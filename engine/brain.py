"""L7 — Agent Query API. Returns token-budgeted context packs."""

from __future__ import annotations
import re
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Iterable
from . import config as C
from . import db as DB
from . import util as _U
from .resolver import is_archive_path
from . import rank_signals as _RS
# Retrieval uses the Cmd 044 domain-neutral default; strict scope stays in capacity_admission.
from .capacity_admission import (retrieval_connection as scoped_connection,
                                 retrieval_coverage_warning as coverage_warning)

# Brain v2 Phase 6 — retrieval v2. Conversational prompts must seed nodes, archive
# copies must never outrank live ones, and the 1.26M semantically_similar_to noise
# edges must not drag duplicates into the pack.

# Stopwords dropped before FTS so a 4-word NL query doesn't require every filler token.
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are", "was",
    "were", "be", "been", "with", "about", "into", "from", "by", "at", "as", "it",
    "this", "that", "these", "those", "what", "which", "who", "whom", "whose", "how",
    "why", "when", "where", "do", "does", "did", "can", "could", "would", "should",
    "i", "we", "you", "me", "my", "our", "us", "your", "they", "them", "their",
    "has", "have", "had", "get", "got", "any", "all", "some", "there", "here",
})

# Predicates excluded from BFS expansion by default (caller can re-request them via
# `predicates=`). semantically_similar_to/co_occurs_with are the embedding-noise edges
# that pull archive + duplicate nodes into results; synonymous_with is alias plumbing.
_EXPAND_EXCLUDE = ("semantically_similar_to", "co_occurs_with", "synonymous_with")

# Status signal for ranking.
_STATUS_BOOST = {"active": 8.0, "canonical": 10.0, "verified": 8.0, "scheduled": 2.0,
                 "completed": 2.0, "open": 2.0}
_STATUS_PENALTY = {"superseded": -18.0, "archived": -18.0, "deprecated": -14.0,
                   "draft": -6.0, "retired": -16.0}

# Session-transcript penalty (2026-06-28). Raw session logs under wiki/logs/sessions/
# are verbatim echoes of the user's prompt ("Nemesis, in this session you are my ELITE
# CREDIT REPAIR ATTORNEY"), so they pack EVERY query term into their title and win the
# term-overlap signal in _relevance_score — burying the authored knowledge (Lessons,
# Patterns, Decisions, FDCPA doctrine) that actually answers the query. 2,982 of these
# near-duplicate restatements exist; a single topic like "NEMESIS FDCPA credit dispute"
# returned a pack of 6 transcript twins and ZERO doctrine before this penalty. It does
# NOT touch authored "Session …" distillations in an agent's own Memory (those live in
# Memory/Sessions/distilled/, which is deliberately excluded from the markers below), so
# cross-agent recall of real lessons is preserved.
# Tunable via env; reversible by setting it to 0.
import os as _os
_SESSION_LOG_PENALTY = float(_os.environ.get("ARCTURION_BRAIN_SESSION_LOG_PENALTY", "-14.0"))
# Applies to the configured graph and source inventory.
_is_raw_transcript = _U.is_raw_transcript_node

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_WS_RE = re.compile(r"\s+")
_RECENCY_TERMS = frozenset({
    "current", "currently", "latest", "newest", "now", "still", "today", "present",
} | ({"status", "state", "progress", "pending", "blocked", "outstanding", "remaining"}
     if C.CURRENT_INTENT_STATUS_TERMS else set()))



def _norm_title(t: str | None) -> str:
    """Normalized title key for twin-collapse — lowercased, whitespace-folded. Empty for
    untitled nodes (which are never collapsed on title)."""
    return _WS_RE.sub(" ", (t or "").strip().lower())


# Date prefix on session-log titles ("Session 2026-05-26 — …") — stripped before
# fingerprinting so otherwise-identical restatements on different days collapse.
_SESSION_DATE_RE = re.compile(r"^\s*session\s+\d{4}-\d{2}-\d{2}\s*(?:\[[^\]]*\]\s*)?[—–-]+\s*", re.I)
_FP_LEN = 180


def _content_fingerprint(node: dict) -> str:
    """A near-duplicate fingerprint from summary (preferred) or title: lowercased,
    whitespace-folded, date-prefix-stripped, capped at _FP_LEN chars. Two nodes that
    restate the same fact in different casing / under different dated paths (the 2,982
    session-log twins, backup copies, id-churn) share a fingerprint and collapse to the
    canonical (highest-scored) copy. Empty fingerprint ⇒ never collapsed (we never drop a
    node we can't confidently call a duplicate)."""
    pre = (node.get("summary") or "").strip() or (node.get("title") or "").strip()
    raw = _SESSION_DATE_RE.sub("", pre)
    raw = _WS_RE.sub(" ", raw.lower()).strip()
    if not raw:
        # Title WAS only the date prefix — exactly the session-log twins this
        # dedup targets; fall back to the pre-strip text so they still collapse
        # instead of getting a free pass (review 2026-07-20).
        raw = _WS_RE.sub(" ", pre.lower()).strip()
    return raw[:_FP_LEN]


def _terms(topic: str, cap: int = 12) -> list[str]:
    """Content tokens from an NL topic: lowercased, stopworded, length>1, deduped."""
    out: list[str] = []
    seen: set[str] = set()
    for t in _TOKEN_RE.findall((topic or "").lower()):
        if len(t) > 1 and t not in _STOPWORDS and t not in seen:
            out.append(t)
            seen.add(t)
        if len(out) >= cap:
            break
    return out


_REL_RE = re.compile(r"^-?\s*(\d+)\s*(h|hours?|d|days?|w|weeks?|mo|months?|y|years?)\s*(?:ago)?$", re.I)
_BARE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def resolve_time(s, end: bool = False) -> str | None:
    """Resolve a since/before bound to an ISO string comparable against last_modified.

    Accepts ISO dates ('2026-06-01'), ISO datetimes, or relative spans ('7d', '30 days',
    '2w', '3mo', '1y', '24h'). A bare upper-bound date is extended to end-of-day so the
    whole day is inclusive. Returns None for empty/unparseable input."""
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    m = _REL_RE.match(s)
    if m:
        n = int(m.group(1))
        u = m.group(2).lower()
        if u.startswith("h"):
            delta = timedelta(hours=n)
        elif u.startswith("w"):
            delta = timedelta(weeks=n)
        elif u.startswith("mo"):
            delta = timedelta(days=30 * n)
        elif u.startswith("y"):
            delta = timedelta(days=365 * n)
        else:  # d / day / days
            delta = timedelta(days=n)
        # Naive LOCAL time — the dominant last_modified format in the live DB is
        # naive local; an offset suffix made lexicographic SQL compares misorder
        # (review 2026-07-20). The python-side _in_window parses properly anyway.
        return (datetime.now() - delta).isoformat(timespec="seconds")
    if end and _BARE_DATE_RE.match(s):
        return s + "T23:59:59"
    return s


def _parse_ts(s: str | None) -> datetime | None:
    """Parse any last_modified/bound format in the wild (naive local, offset,
    space-separated) to an AWARE datetime; naive strings are assumed local.
    Returns None for empty/unparseable input."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # attach local tz
    return dt


@dataclass
class ContextPack:
    topic: str
    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    text_bundle: str = ""
    citations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False
    used_tokens_est: int = 0

    def to_dict(self) -> dict:
        return {
            "schema": "brain.context-pack.v2",
            "sources": [_source_descriptor(n) for n in self.nodes],
            "topic": self.topic,
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "truncated": self.truncated,
            "used_tokens_est": self.used_tokens_est,
            "citations": self.citations,
            "warnings": self.warnings,
            "text_bundle": self.text_bundle,
        }


def _source_descriptor(node: dict) -> dict:
    metadata = node.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    out = {**{k: node.get(k) for k in ("id", "path", "content_hash", "owner")},
           **{k: metadata.get(k) for k in ("subject", "ownership_source", "ownership_conflict",
                                          "ownership_invalid")}}
    j = metadata.get("journal")
    if isinstance(j, dict) and str(node.get("id") or "").startswith("journal:"):
        out["journal_source_path"] = j.get("source_path")
        out["journal_source_sha256"] = j.get("source_sha256")
    return out


def capture_receipt(path: str, capacity: str = "business", project_id: str | None = None) -> dict:
    """Exact indexed-version visibility; does not read source files or imply authority."""
    from .extractors.markdown import _rel_to_universe
    from .source_integrity import filter_retrieval
    p = Path(path)
    if not p.is_absolute() or ".." in p.parts:
        raise ValueError("receipt path must be absolute and normalized")
    stored = _rel_to_universe(p)
    conn = scoped_connection(capacity, project_id, include_telemetry=True)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM nodes WHERE path IN (?,?)", (stored, str(p)))]
        rows = filter_retrieval(conn, rows)
        result = []
        for node in rows:
            lexical = bool(conn.execute(
                "SELECT 1 FROM fts_nodes WHERE node_id=? LIMIT 1", (node["id"],)).fetchone())
            semantic = bool(conn.execute(
                "SELECT 1 FROM embeddings WHERE node_id=? LIMIT 1", (node["id"],)).fetchone())
            result.append({**_source_descriptor(node), "absolute_path": str(p),
                           "lexical_indexed": lexical, "semantic_indexed": semantic})
        return {"schema": "brain.capture-receipt.v1",
                "status": "hit" if result else "empty", "sources": result,
                "coverage": coverage_warning(capacity, project_id)}
    finally:
        conn.close()


_OWNER_BOOST = 30.0


def _lexical_score(node: dict, topic: str) -> float:
    'The purely LEXICAL half of the old `_relevance_score`: whole-phrase hits plus'
    q = topic.strip().lower()
    title = (node.get("title") or "").lower()
    summary = (node.get("summary") or "").lower()
    body = (node.get("body_excerpt") or "").lower()
    node_id = (node.get("id") or "").lower()
    terms = _terms(topic)

    score = 0.0
    # Whole-phrase hits (only fire for short/exact topics).
    if node_id == q:
        score += 100.0
    if title == q:
        score += 90.0
    elif title.startswith(q):
        score += 85.0
    elif q and q in title:
        score += 80.0
    if q and q in summary:
        score += 50.0
    if q and q in body:
        score += 25.0
    # Per-term overlap — the load-bearing signal for conversational queries.
    score += sum(6.0 for term in terms if term in title)
    score += sum(2.0 for term in terms if term in summary)
    score += sum(0.5 for term in terms if term in body)
    return score


def _lexical_score_idf(node: dict, topic: str,
                        stats: dict[str, tuple[int, float]]) -> float:
    """`_lexical_score`'s IDF-weighted twin (Leg 2 follow-up, retrieval-honesty 2026-08-06).

    Identical phrase-hit tier (whole-topic hits on id/title/summary/body score exactly as
    `_lexical_score` does — those already fire on the WHOLE query, so rarity-weighting a
    single boolean has nothing to distribute). The per-term overlap tier is where the two
    diverge: `_lexical_score` credits every matched term the same 6.0/2.0/0.5 regardless of
    how many other documents also contain it, which is the defect this exists to fix.

    Each term's contribution is scaled by `idf(t) / max_idf(query)`, where `max_idf` is the
    highest IDF among THIS query's own terms (not a corpus-wide constant). That keeps the
    ceiling identical to `_lexical_score`: the query's rarest term still earns the full
    6.0/2.0/0.5, so `C.LEXICAL_WEIGHT` keeps the magnitude it was calibrated at, and only
    weight AMONG a query's terms shifts — from common toward rare — rather than the overall
    scale of the signal shifting.

    `stats` is `_query_impl`'s `term_stats(conn, _terms(topic))`, computed once per query and
    passed in here; this function issues no DB call of its own; A TERM MISSING FROM `stats`
    (should not happen — same `_terms()` call builds both, but `RARE_TOKEN_MAX_TERMS` caps
    `term_stats`'s own iteration) contributes nothing rather than guessing.

    Degrades to `_lexical_score`'s equal-weighting behaviour when `stats` carries no rarity
    information at all (empty, or every term's IDF is 0 — an unavailable-FTS fixture graph,
    same shape `have_signal` in `_query_impl` already treats as "no signal"): a query this
    function cannot discriminate must not be scored as if every term were worthless.
    """
    q = topic.strip().lower()
    title = (node.get("title") or "").lower()
    summary = (node.get("summary") or "").lower()
    body = (node.get("body_excerpt") or "").lower()
    node_id = (node.get("id") or "").lower()
    terms = _terms(topic)

    score = 0.0
    # Whole-phrase hits — unchanged from `_lexical_score`, nothing to weight.
    if node_id == q:
        score += 100.0
    if title == q:
        score += 90.0
    elif title.startswith(q):
        score += 85.0
    elif q and q in title:
        score += 80.0
    if q and q in summary:
        score += 50.0
    if q and q in body:
        score += 25.0

    max_idf = max((i for _df, i in stats.values()), default=0.0)
    if max_idf <= 0.0:
        # No rarity information to weight by (empty stats, or every term priced at 0 IDF) —
        # degrade to equal weighting rather than silently zeroing the per-term signal.
        score += sum(6.0 for term in terms if term in title)
        score += sum(2.0 for term in terms if term in summary)
        score += sum(0.5 for term in terms if term in body)
        return score

    for term in terms:
        _df, i = stats.get(term, (-1, 0.0))
        w = i / max_idf if i > 0.0 else 0.0
        if w <= 0.0:
            continue
        if term in title:
            score += 6.0 * w
        if term in summary:
            score += 2.0 * w
        if term in body:
            score += 0.5 * w
    return score


def _authority_score(node: dict, owner: str | None = None) -> float:
    """The half of the old `_relevance_score` that has nothing to do with the query: is this
    node LIVE, CANONICAL, and the real copy? Applies identically in v1 and v2."""
    score = 0.0
    # 6b: truth outranks noise. Boost live/canonical status, penalize stale status,
    # and hard-penalize archive/backup/staging copies so a canonical node always wins
    # over its archived twin.
    status = (node.get("status") or "").lower()
    score += _STATUS_BOOST.get(status, 0.0)
    score += _STATUS_PENALTY.get(status, 0.0)
    if is_archive_path(node.get("path")):
        score -= 20.0
    elif C.HISTORICAL_COPY_RANKING and _RS.is_historical_copy(node.get("path")):
        score -= C.HISTORICAL_COPY_PENALTY
    if C.SESSION_DISTILLATION_PENALTY and any(
            m in (node.get("path") or "").lower() for m in C.SESSION_DISTILLATION_MARKERS):
        score -= C.SESSION_DISTILLATION_PENALTY
    if node.get("superseded_by"):
        score -= 12.0
    # Demote raw session transcripts so authored knowledge outranks prompt-echoes.
    if _is_raw_transcript(node):
        score += _SESSION_LOG_PENALTY
    # Agent-scoped recall (owner param): float the requesting agent's own material
    # to the top without excluding cross-agent nodes — owner coverage is ~33% of
    # the graph, so this is a boost, not a filter, and never yields an empty pack.
    if owner and (node.get("owner") or "").strip().upper() == owner.strip().upper():
        score += _OWNER_BOOST
    score += _durable_source_score(node)
    return score


def _durable_source_score(node: dict) -> float:
    """Small authority lift for authored durable knowledge shelves.

    This is intentionally path-class based and query-independent. It cannot know an eval
    question, expected node, or held-out label; it only expresses the general rule that a
    maintained Decision/Preference/Policy/Knowledge note outranks an equally relevant echo.
    """
    if not C.DURABLE_SOURCE_AUTHORITY:
        return 0.0
    path = (node.get("path") or "").replace("\\", "/").lower()
    if any(marker in path for marker in C.DURABLE_SOURCE_PATH_MARKERS):
        return C.DURABLE_SOURCE_BOOST
    return 0.0


def _recency_durable_source_score(node: dict, topic: str) -> float:
    """Extra durable-source preference only when the query explicitly asks for now.

    Historical queries keep the ordinary +4 authority signal. Current-state questions get
    enough separation to keep legacy feedback echoes below maintained doctrine, without
    changing the global ranker or inventing supersession facts that the graph does not have.
    """
    if not C.DURABLE_SOURCE_AUTHORITY:
        return 0.0
    if not (_RECENCY_TERMS & set(_TOKEN_RE.findall((topic or "").lower()))):
        return 0.0
    return C.RECENCY_DURABLE_BOOST if _durable_source_score(node) else 0.0



def _validity_tier(node: dict, now: datetime) -> int:
    """1 (live) or 0 (expired) — the temporal-validity sort tier (Leg 4, see
    `config.TEMPORAL_VALIDITY_TIER` and `rank_key_v2`'s docstring for the full argument).

    `valid_until` is written ONLY by `engine.maintenance.validity_pass`, from an explicit
    supersession or resolved-contradiction record naming this node — never inferred, never
    set by ingest. A node with no `valid_until`, or an unparseable one, is treated as live:
    the absence of a stamp is not evidence of staleness, and a parse failure must fail open
    exactly like `_in_window`'s `_parse_ts` callers elsewhere in this module."""
    if is_archive_path(node.get("path")) or (node.get("status") or "").lower() in {"archived", "superseded", "retired"}:
        return 0
    vu = _parse_ts(node.get("valid_until"))
    if vu is None:
        return 1
    return 0 if vu <= now else 1


def _relevance_score(node: dict, topic: str, owner: str | None = None) -> float:
    """Lexical + authority — the v1 ranking function, byte-for-byte in behaviour.

    Retained as the sole scorer on the `RETRIEVAL_V2=0` path and as the seed-dedup ordering
    in `_seed_nodes`, so the A/B switch restores the old ranking rather than an approximation
    of it."""
    return _lexical_score(node, topic) + _authority_score(node, owner=owner)


def _semantically_untrustworthy(node: dict) -> bool:
    """True for nodes whose cosine similarity is systematically misleading.

    Raw session transcripts are verbatim echoes of the Founder's own prompts, so they embed
    close to ANY conversational question regardless of what they contain — their similarity
    measures how the question was PHRASED, not what the node knows. Verified 2026-08-04: for
    'how do the agents talk to each other', raw transcripts sit at cosine 0.63, above the
    actual Inter-Agent Communications Protocol at 0.58.

    Withheld here is ONLY the new semantic term. Every lexical, status, archive and centrality
    signal still applies to these nodes exactly as before, so this changes no ranking that
    existed before hybrid retrieval. Same carve-out as `util.is_raw_transcript_node`:
    AUTHORED distillations under Sessions/distilled/ are deliberately not excluded.

    Leg 4 (2026-08-07) widened WHICH nodes this reaches without widening what it does. It
    asked `config.VECTOR_SEED_EXCLUDE_PATHS` — a location list — and so covered 4,797 of
    the graph's 7,823 raw-transcript nodes, leaving the 3,026 on the Brain's 0608 catch-all
    shelf embedding freely. `config.VECTOR_SEED_EXCLUDE_PATHS` is retained as a
    documented, env-free way to withhold the semantic term from ADDITIONAL paths that are
    not transcripts; it is a union with the structural rule, never a replacement for it."""
    if _is_raw_transcript(node):
        return True
    path = node.get("path") or ""
    return any(frag in path for frag in C.VECTOR_SEED_EXCLUDE_PATHS)


def _fts_or_seed(conn: sqlite3.Connection, terms: list[str], limit: int,
                  stats: dict[str, tuple[int, float]] | None = None) -> list[dict]:
    """FTS5 OR-query over content terms, bm25-ranked. The fix for conversational
    prompts: a node matching ANY term seeds the pack (was: implicit-AND of every
    token incl. stopwords -> empty). Returns [] if FTS5 is unavailable.

    `stats`, when given and `C.FTS_SEED_IDF_FILTER` is on, drops terms whose IDF prices
    below `C.FTS_SEED_MIN_IDF` before building the OR query — stopword-grade terms
    `_terms()` doesn't catch (common domain words, not stopwords, but common enough that an
    OR-match on them alone crowds the bm25 window rather than narrowing it). GUARANTEED
    FALLBACK: if filtering would leave zero terms, the unfiltered list is used instead — a
    seed channel must never go from "returns something" to "returns nothing" because every
    term in the query happened to be common. `stats` is `None`/`{}` on the v1 path (`_query_
    impl` never computes it there), so this is a no-op there regardless of the flag."""
    if not terms:
        return []
    seed_terms = terms
    if C.FTS_SEED_IDF_FILTER and stats:
        rare_enough = [t for t in terms
                       if stats.get(t, (-1, 0.0))[1] >= C.FTS_SEED_MIN_IDF]
        if rare_enough:
            seed_terms = rare_enough
        # else: every term priced below the threshold — fall back to the full list rather
        # than seed nothing.
    match = " OR ".join(f'"{t}"' for t in seed_terms)
    try:
        rows = conn.execute(
            """SELECT n.*, bm25(fts_nodes) AS _bm25
               FROM fts_nodes f JOIN nodes n ON n.id = f.node_id
               WHERE fts_nodes MATCH ?
               ORDER BY _bm25
               LIMIT ?""",
            (match, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def _title_terms_channel(conn: sqlite3.Connection, terms: list[str], limit: int) -> list[dict]:
    """BM25-ranked title-only matches for answer-bearing authored document titles."""
    if not terms:
        return []
    match = "title : (" + " OR ".join(f'"{t}"' for t in terms) + ")"
    try:
        return [dict(r) for r in conn.execute(
            """SELECT n.*, bm25(fts_nodes) AS _bm25
                 FROM fts_nodes f JOIN nodes n ON n.id = f.node_id
                WHERE fts_nodes MATCH ? ORDER BY _bm25 LIMIT ?""",
            (match, limit),
        ).fetchall()]
    except sqlite3.OperationalError:
        return []


def _vector_channel(conn: sqlite3.Connection, sims, node_cache: dict[str, dict]) -> list[str]:
    """The vector channel's ranked ids: nearest neighbours of the query, worst noise dropped.

    Over-fetch then filter, because near-identical files score ~1.0 cosine against each other
    (worktree copies, `_config_backups`), so a raw top-K is routinely one document repeated.
    Archive copies and raw session transcripts are dropped outright — both already carry hard
    rank penalties (-20 / -14 in `_authority_score`), so seeding them can only widen the BFS
    frontier with junk and can never win a slot."""
    if sims is None or not C.VECTOR_SEED_ENABLED or C.VECTOR_SEED_TOP_K <= 0:
        return []
    hits = sims.top(C.VECTOR_SEED_FETCH_K)
    if not hits:
        return []

    # ONE batched read for the whole candidate window. Fetching these row-by-row was ~400
    # separate statements per query and the single largest cost this leg added to warm p50.
    # Chunked because SQLite caps host parameters at 999.
    missing = [nid for nid, _s in hits if nid not in node_cache]
    for i in range(0, len(missing), 400):
        chunk = missing[i:i + 400]
        for row in conn.execute(
                f"SELECT * FROM nodes WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            node = dict(row)
            node_cache.setdefault(node["id"], node)

    out: list[str] = []
    seen_paths: set[str] = set()
    seen_titles: set[str] = set()
    seen_fps: set[str] = set()
    for nid, _sim in hits:
        if len(out) >= C.VECTOR_SEED_TOP_K:
            break
        node = node_cache.get(nid)
        if node is None:      # embedded row whose node was GC'd since the matrix was cached
            continue
        path = node.get("path") or ""
        if is_archive_path(path) or _is_raw_transcript(node):
            continue
        if _semantically_untrustworthy(node):
            continue
        # Collapse near-identical copies WITHIN the channel. Duplicate files score ~1.0
        # cosine against each other, so an un-deduped top-K is routinely one document
        # repeated — and because this is a RANKED list feeding fusion, a repeat does not
        # merely waste a slot, it pushes every genuine neighbour down a rank.
        t = _norm_title(node.get("title"))
        fp = _content_fingerprint(node)
        if (path and path in seen_paths) or (t and t in seen_titles) or (fp and fp in seen_fps):
            continue
        if path:
            seen_paths.add(path)
        if t:
            seen_titles.add(t)
        if fp:
            seen_fps.add(fp)
        out.append(nid)
    return out


def _seed_channels(conn: sqlite3.Connection, topic: str, limit: int,
                   sims=None,
                   stats: dict[str, tuple[int, float]] | None = None,
                   ) -> tuple[dict[str, list[str]], dict[str, dict]]:
    """Run every seed channel and return (channel -> ranked ids, id -> node row).

    Each channel keeps ITS OWN order — bm25 for FTS, cosine for vectors, centrality for the
    title scan — because rank fusion consumes rank, not score. The v1 path flattened all of
    these into one insertion-ordered union and then threw the orders away; that discarded
    bm25's IDF, which is the measured cause of `common-token` recall@5 0.000."""
    node_cache: dict[str, dict] = {}
    channels: dict[str, list[str]] = {}

    def collect(rows) -> list[str]:
        ids: list[str] = []
        for row in rows:
            node = dict(row)
            node_cache.setdefault(node["id"], node)
            ids.append(node["id"])
        return ids

    terms = _terms(topic)
    if C.QUERY_ALIAS_EXPANSION:
        # Leg 3 (retrieval-honesty 2026-08-06): widen the seed-side term list through
        # canon/ENTITY_ALIASES.md so a Founder-phrased query reaches corpus jargon it never
        # typed. Isolated to seeding — `_query_impl`'s own `stats`/rares/exact-token pin
        # keep using the unexpanded `_terms(topic)`; only THIS function's local `terms` (and
        # therefore `channels["fts"]`/the alias/title channels) see the expansion.
        from . import aliases as A
        canonical_to_aliases, _ = A.get_aliases()
        expanded = A.expand_query_terms(topic, terms, canonical_to_aliases)
        added = [t for t in expanded if t not in terms]
        if added and stats:
            # `_fts_or_seed`'s FTS_SEED_IDF_FILTER drops any term missing from `stats`
            # (default idf 0.0, below FTS_SEED_MIN_IDF) — without this, every alias-added
            # term would be silently filtered back out and the expansion would do nothing.
            # A local dict, not a mutation of the caller's `stats` — `_query_impl` reuses
            # that same object afterwards for the exact-token pin and `_lexical_score_idf`,
            # which must stay scoped to what the Founder actually typed.
            from . import rank_signals as RS
            stats = {**stats, **RS.term_stats(conn, added)}
        terms = expanded
    ql = topic.strip().lower()

    # 1. exact node id.
    channels["exact_id"] = collect(
        conn.execute("SELECT * FROM nodes WHERE lower(id) = ? LIMIT 1", (ql,)).fetchall())

    # 2. cheap alias/agent seeding: an agent token or an alias-table hit per term.
    alias_ids: list[str] = []
    for t in terms:
        alias_ids += collect(
            conn.execute("SELECT * FROM nodes WHERE id = ? LIMIT 1", (f"agent_{t}",)).fetchall())
        arow = conn.execute(
            "SELECT canonical_id FROM aliases WHERE lower(alias) = ? LIMIT 1", (t,)).fetchone()
        if arow:
            alias_ids += collect(conn.execute(
                "SELECT * FROM nodes WHERE id = ? LIMIT 1", (arow["canonical_id"],)).fetchall())
    channels["alias"] = alias_ids

    # 3. whole-phrase title substring (cheap, helps short exact topics). instr() avoids
    #    a wildcard scan literal and is case-folded on both sides.
    channels["title"] = collect(conn.execute(
        """SELECT * FROM nodes WHERE instr(lower(title), ?) > 0
           ORDER BY centrality DESC, last_modified DESC LIMIT ?""",
        (ql, limit)).fetchall())

    # 4. FTS5 OR-query (primary NL path), in bm25 order. Pull a WIDE window: the live graph
    #    still carries un-GC'd id-churn twins (same path, many ids) that crowd the bm25 top,
    #    so a narrow window can push the canonical copy of a hit out of view.
    channels["fts"] = collect(
        _fts_or_seed(conn, terms, min(600, max(120, limit * 40)), stats=stats))

    # A field-scoped lane keeps an authored title from being buried by numerous body-only
    # mentions. It remains one ordinary fusion vote; the bounded reserve below additionally
    # protects only durable-source hits from being displaced during graph expansion.
    channels["title_terms"] = collect(
        _title_terms_channel(conn, terms, min(120, max(40, limit * 10))))

    # 5. Optional vector channel.
    channels["vector"] = _vector_channel(conn, sims, node_cache)

    # 6. never-empty fallback: per-term title/summary substring if NO channel matched.
    if not any(channels.values()) and terms:
        ranked = sorted(terms, key=len, reverse=True)
        clause = " OR ".join(
            ["instr(lower(title), ?) > 0", "instr(lower(summary), ?) > 0"] * len(ranked))
        params: list = []
        for t in ranked:
            params += [t, t]
        channels["fts"] = collect(conn.execute(
            f"SELECT * FROM nodes WHERE {clause} ORDER BY centrality DESC LIMIT ?",
            params + [limit]).fetchall())

    return channels, node_cache


_L016_CANONICAL_TITLE = "Law L016 — Task Outcomes & Verification"
_L016_CANONICAL_PATH = "COVENANT/00 🛡 Governance/Laws/L016_definition-of-done.md"
_L016_IDENTIFIER_RE = re.compile(r"(?<![a-z0-9])l016(?![a-z0-9])", re.I)
_L016_DOCTRINE_INTENT_RE = re.compile(
    r"\b(?:law|doctrine|task\s+outcomes|definition\s+of\s+done)\b", re.I)
_L016_HISTORICAL_INTENT_RE = re.compile(
    r"\b(?:historical|history|previous|formerly|obsolete|archive|archived|"
    r"superseded|audit)\b", re.I)


def _explicit_current_l016_node(conn, topic, *, historical=False, enabled=False):
    """Return the unique active canonical L016 source only for explicit law queries."""
    query = (topic or "").strip()
    if (not enabled or historical or not _L016_IDENTIFIER_RE.search(query)
            or not _L016_DOCTRINE_INTENT_RE.search(query)
            or _L016_HISTORICAL_INTENT_RE.search(query)):
        return None
    # Existing node identifiers remain address lookups and retain their established path.
    if conn.execute("SELECT 1 FROM nodes WHERE lower(id)=lower(?) LIMIT 1",
                    (query,)).fetchone():
        return None
    rows = conn.execute(
        """SELECT * FROM nodes
           WHERE title=? AND path=? AND type='Source' AND family='document'
             AND owner='COVENANT' AND status='active' AND governance_tier=0
             AND valid_until IS NULL""",
        (_L016_CANONICAL_TITLE, _L016_CANONICAL_PATH)).fetchall()
    # ID-churn twins or any other ambiguity must fall back to ordinary ranking.
    if len(rows) != 1:
        return None
    from .source_integrity import filter_retrieval
    allowed = filter_retrieval(conn, [dict(rows[0])], historical=False)
    return allowed[0] if allowed else None


def _prioritize_current_l016(nodes, canonical_node, limit):
    if limit <= 0:
        return []
    if canonical_node is None:
        return nodes[:limit]
    canonical_id = canonical_node["id"]
    return [canonical_node] + [
        node for node in nodes if node.get("id") != canonical_id
    ][:limit - 1]


def _seed_nodes(conn: sqlite3.Connection, topic: str, limit: int = 12,
                 owner: str | None = None, owner_strict: bool = False,
                 sims=None, out_fusion: dict[str, float] | None = None,
                 stats: dict[str, tuple[int, float]] | None = None,
                 out_reserve: dict[str, int] | None = None,
                 historical: bool = False,
                 current_law_priority: dict | None = None) -> list[dict]:
    """Choose the nodes the graph walk starts from.

    v1 (`RETRIEVAL_V2=0`): union every channel in insertion order, re-score the union with
    `_relevance_score`, collapse id-churn twins by path, keep the top `limit`.

    v2: fuse the channels by reciprocal rank (`rank_signals.rrf`), so a document several
    channels agree on outranks one that a single channel shouted about — and so bm25's term
    rarity, computed all along to pick the FTS window and then discarded, finally reaches the
    ordering. `out_fusion`, when given, is filled with the fused score per id so the final
    scorer in `_query_impl` can reuse it without recomputing anything."""
    channels, node_cache = _seed_channels(conn, topic, limit, sims=sims, stats=stats)
    stats = stats or {}
    from .source_integrity import filter_retrieval
    permitted = {n['id'] for n in filter_retrieval(conn, list(node_cache.values()), historical=historical)}
    channels = {name: [nid for nid in ids if nid in permitted] for name, ids in channels.items()}
    node_cache = {nid: n for nid, n in node_cache.items() if nid in permitted}
    # An existing identifier is an address lookup, not an inferred semantic answer.
    exact = channels.get("exact_id", [])
    if exact:
        return [node_cache[nid] for nid in exact]

    if not C.RETRIEVAL_V2:
        candidates: list[dict] = []
        seen: set[str] = set()
        for name in ("exact_id", "alias", "title", "fts"):   # v1 never had a vector channel
            for nid in channels.get(name, []):
                if nid not in seen:
                    seen.add(nid)
                    candidates.append(node_cache[nid])
        # owner_strict: hard-restrict to this agent's own nodes among the topic-matched
        # candidates. Opt-in only — ~33% owner coverage means filtering before topic
        # matching would silently starve most topics, so this filters AFTER matching.
        if owner and owner_strict:
            candidates = [n for n in candidates
                          if (n.get("owner") or "").strip().upper() == owner.strip().upper()]
        # Collapse id-churn twins at seed time: keep the best-scoring node per path so the
        # canonical copy (live path > archive path) wins its slot and duplicates don't
        # consume the top-`limit`. Pathless nodes are all kept.
        scored = [(n, _relevance_score(n, topic, owner=owner)) for n in candidates]
        best_by_path: dict[str, tuple[dict, float]] = {}
        pathless: list[tuple[dict, float]] = []
        for n, s in scored:
            p = n.get("path")
            if not p:
                pathless.append((n, s))
            elif p not in best_by_path or s > best_by_path[p][1]:
                best_by_path[p] = (n, s)
        merged = list(best_by_path.values()) + pathless
        merged.sort(key=lambda ns: (ns[1], float(ns[0].get("centrality") or 0.0)), reverse=True)
        return _prioritize_current_l016(
            [n for n, _ in merged[:limit]], current_law_priority, limit)

    from . import rank_signals as RS
    # Title candidates are a discovery-only lane. They must not perturb ordinary RRF;
    # only the explicit, qualified support-slot policy below may use them.
    title_candidates = channels.pop("title_terms", [])
    fused = RS.rrf(channels)
    if out_fusion is not None:
        out_fusion.update(fused)

    candidates = [node_cache[nid] for nid in fused if nid in node_cache]
    if owner and owner_strict:
        candidates = [n for n in candidates
                      if (n.get("owner") or "").strip().upper() == owner.strip().upper()]

    # Order the seed slots by fused rank, broken by authority (so a live copy beats its
    # archived twin) — then collapse duplicates on the SAME three keys `_query_impl` uses.
    # Path-only dedup let one document eat most of the seed budget whenever it existed under
    # several paths, which in this graph it usually does; the pack looked fine afterwards
    # because `_query_impl` dedupes again before rendering, but by then the wasted slots had
    # already narrowed the BFS frontier to a handful of distinct documents.
    def _seed_key(n: dict) -> tuple[float, float]:
        return (C.RRF_SCALE * fused.get(n["id"], 0.0) + _authority_score(n, owner=owner),
                float(n.get("centrality") or 0.0))

    candidates.sort(key=_seed_key, reverse=True)
    reserve: dict[str, int] = {}
    if C.DURABLE_HEAD_RESERVE:
        if C.DURABLE_VECTOR_RESERVE > 0:
            for nid in channels.get("vector", []):
                node = node_cache.get(nid)
                if node and _durable_source_score(node):
                    reserve[nid] = max(reserve.get(nid, 0), 1)
                    if sum(1 for tier in reserve.values() if tier == 1) >= C.DURABLE_VECTOR_RESERVE:
                        break
        title_count = 0
        for nid in title_candidates:
            node = node_cache.get(nid)
            query_terms = set(_terms(topic))
            title_terms = set(_TOKEN_RE.findall((node.get("title") or "").lower())) if node else set()
            title_hits = len(query_terms & title_terms)
            title_coverage = title_hits / len(query_terms) if query_terms else 0.0
            max_idf = max((idf for _df, idf in stats.values()), default=0.0)
            if (node and _durable_source_score(node)
                    and len(query_terms) >= 5 and 4.0 <= max_idf < 6.0
                    and not re.search(r"[_=]", topic)
                    and title_hits >= 2 and title_coverage >= 0.25):
                reserve[nid] = 2
                title_count += 1
                if title_count >= C.DURABLE_TITLE_RESERVE:
                    break
    if out_reserve is not None:
        out_reserve.update(reserve)
    if reserve:
        candidates.sort(key=lambda n: (reserve.get(n["id"], 0), _seed_key(n)), reverse=True)
    out: list[dict] = []
    seen_paths: set[str] = set()
    seen_titles: set[str] = set()
    seen_fps: set[str] = set()
    for n in candidates:
        if len(out) >= limit:
            break
        p = n.get("path")
        t = _norm_title(n.get("title"))
        fp = _content_fingerprint(n)
        # Check every guard before mutating ANY seen-set — adding early would let a node
        # rejected by a later guard poison the sets and silently drop the only surviving copy
        # of a different document (the same bug fixed in `_query_impl`, review 2026-07-20).
        if (p and p in seen_paths) or (t and t in seen_titles) or (fp and fp in seen_fps):
            continue
        if p:
            seen_paths.add(p)
        if t:
            seen_titles.add(t)
        if fp:
            seen_fps.add(fp)
        out.append(n)
    return _prioritize_current_l016(out, current_law_priority, limit)


def _expand(conn: sqlite3.Connection, seed_ids: set[str], radius: int,
            predicates: list[str] | None, historical: bool = False) -> tuple[set[str], list[dict]]:
    """BFS expand from seeds up to `radius` hops; return reachable_ids + traversed edges."""
    from .source_integrity import filter_retrieval
    def allowed_ids(ids):
        if not ids:
            return set()
        rows = conn.execute(f"SELECT * FROM nodes WHERE id IN ({','.join('?'*len(ids))})", list(ids)).fetchall()
        return {n['id'] for n in filter_retrieval(conn, [dict(r) for r in rows], historical=historical)}
    reached = allowed_ids(seed_ids)
    edges_out: list[dict] = []
    frontier = set(reached)
    pred_clause = ""
    pred_params: list = []
    if predicates:
        pred_clause = f" AND predicate IN ({','.join('?'*len(predicates))})"
        pred_params = list(predicates)
    else:
        # 6c: by default, don't traverse the embedding-noise edges
        # (semantically_similar_to/co_occurs_with/synonymous_with). They pull archive +
        # duplicate nodes into the pack and bury canonical truth. Caller can re-request
        # them explicitly via `predicates=`.
        pred_clause = f" AND predicate NOT IN ({','.join('?'*len(_EXPAND_EXCLUDE))})"
        pred_params = list(_EXPAND_EXCLUDE)

    # Applies to the configured graph and source inventory.
    edge_limit = C.EXPAND_MAX_EDGES_PER_HOP
    limit_clause = f" LIMIT {edge_limit}" if edge_limit and edge_limit > 0 else ""
    max_reached = C.EXPAND_MAX_REACHED
    for _ in range(max(0, radius)):
        if not frontier:
            break
        placeholders = ",".join("?" * len(frontier))
        params = list(frontier) + list(frontier) + pred_params
        rows = conn.execute(
            f"""SELECT from_id, to_id, predicate, family, weight, confidence, asserted_by
                FROM edges
                WHERE (from_id IN ({placeholders}) OR to_id IN ({placeholders}))
                  AND EXISTS(SELECT 1 FROM nodes WHERE id = edges.from_id)
                  AND EXISTS(SELECT 1 FROM nodes WHERE id = edges.to_id)
                {pred_clause}{limit_clause}""",
            params,
        ).fetchall()
        permitted = allowed_ids({r[key] for r in rows for key in ('from_id', 'to_id')})
        new_frontier: set[str] = set()
        for r in rows:
            d = dict(r)
            if d['from_id'] not in permitted or d['to_id'] not in permitted:
                continue
            edges_out.append(d)
            if d["from_id"] not in reached:
                new_frontier.add(d["from_id"])
            if d["to_id"] not in reached:
                new_frontier.add(d["to_id"])
        reached |= new_frontier
        # Stop discovering once we already hold far more candidates than any budget
        # will keep — further hops only add rank-losers and disk reads.
        if max_reached and len(reached) >= max_reached:
            break
        frontier = new_frontier
    return reached, edges_out


def _fully_dangling_requested(conn: sqlite3.Connection,
                              predicates: list[str] | None) -> list[str]:
    """Return requested relationship classes that exist but have no walkable edge.

    This is deliberately scoped to explicit predicate filters. The normal query path
    already excludes every dangling edge in `_expand`; an explicit structural request
    additionally needs a visible refusal so callers cannot mistake seed documents for
    an answer produced by the requested relationship.
    """
    requested = sorted(set(predicates or []))
    if not requested:
        return []
    placeholders = ",".join("?" * len(requested))
    rows = conn.execute(
        f"""SELECT e.predicate,
                   SUM(CASE WHEN src.id IS NOT NULL AND dst.id IS NOT NULL
                            THEN 1 ELSE 0 END) AS walkable
              FROM edges e
              LEFT JOIN nodes src ON src.id = e.from_id
              LEFT JOIN nodes dst ON dst.id = e.to_id
             WHERE e.predicate IN ({placeholders})
             GROUP BY e.predicate""",
        requested,
    ).fetchall()
    return sorted(r["predicate"] for r in rows if int(r["walkable"] or 0) == 0)


def _pack_source(node: dict) -> str:
    """The 'source' a node counts against for `PACK_PER_SOURCE_CAP`: its containing
    folder. That is the level at which crowding actually happens — one shelf, one project
    directory, one agent's Memory room. Coarser (the agent root) would cap a whole
    agent's contribution, which is a different and wrong policy; finer (the file) is what
    path-dedup already does."""
    p = node.get("path") or ""
    return p.rsplit("/", 1)[0] if "/" in p else ""


def _body_snippet(node: dict, limit: int) -> str:
    """Up to `limit` chars of a node's body, with the summary's own text removed.

    The summary is very often the body's opening sentence, so emitting both verbatim
    spends budget restating what the caller read one line above. Trimmed at a word
    boundary when it has to cut, and marked with an ellipsis so a truncated paragraph is
    never mistaken for a complete one — a silently-cut body is a quotation that ends
    mid-argument."""
    body = (node.get("body_excerpt") or "").strip()
    if not body or limit <= 0:
        return ""
    summary = (node.get("summary") or "").strip()
    if summary and body.startswith(summary):
        body = body[len(summary):].lstrip()
    if not body:
        return ""
    if len(body) <= limit:
        return body
    cut = body[:limit]
    space = cut.rfind(" ")
    if space > limit // 2:          # never trim back past half the allowance
        cut = cut[:space]
    return cut.rstrip() + " \u2026"


def _node_lines(node: dict, body_chars: int = 0) -> str:
    head = f"### {node.get('title') or node.get('id')}  [{node.get('type')}|{node.get('family')}]"
    meta = f"`id: {node.get('id')}  path: {node.get('path')}  owner: {node.get('owner')}`"
    summary = node.get("summary") or ""
    body = _body_snippet(node, body_chars)
    return f"{head}\n{meta}\n{summary}\n" + (f"{body}\n" if body else "")


def query(
    topic: str,
    radius: int = C.DEFAULT_RADIUS,
    predicates: Iterable[str] | None = None,
    families: Iterable[str] | None = None,
    governance_tier_min: int = 0,
    token_budget: int = C.DEFAULT_TOKEN_BUDGET,
    recency_boost: float = 0.3,
    exclude_orphans: bool = False,
    since: str | None = None,
    before: str | None = None,
    order_by: str = "relevance",
    source: str = "py",
    owner: str | None = None,
    owner_strict: bool = False,
    capacity: str = "business",
    project_id: str | None = None,
    desk: str | None = None,
    desk_hops: int | None = None,
) -> ContextPack:
    if desk is not None:
        if desk != "VEGA":
            raise ValueError("unknown desk")
        from .vega_index import enabled
        if not enabled():
            raise ValueError("VEGA desk is disabled")
        if (radius != C.DEFAULT_RADIUS or families is not None
                or governance_tier_min != 0 or recency_boost != 0.3 or exclude_orphans
                or since is not None or before is not None or order_by != "relevance"
                or owner is not None or owner_strict):
            raise ValueError("VEGA desk supports topic, budget, capacity and project_id only")
        from .vega_query import query_desk
        return query_desk(topic, token_budget=token_budget, capacity=capacity,
                          project_id=project_id, predicates=predicates, desk_hops=desk_hops)
    if desk_hops is not None:
        raise ValueError("desk_hops requires desk='VEGA'")
    # Item 13: metrics. Lazy import to avoid cycles.
    try:
        from . import metrics as _M
    except Exception:
        _M = None
    # Real with-semantics via ExitStack (conditional CM): the old manual
    # __enter__/__exit__(None,None,None) told the metrics CM every query
    # succeeded — failures were logged as successful zero-node queries, and an
    # __enter__ raise leaked the CM (review 2026-07-20).
    from contextlib import ExitStack
    with ExitStack() as _stack:
        _ctx = _stack.enter_context(_M.log_query(topic, source=source)) if _M else {}
        pack = _query_impl(
            topic=topic, radius=radius, predicates=predicates, families=families,
            governance_tier_min=governance_tier_min, token_budget=token_budget,
            recency_boost=recency_boost, exclude_orphans=exclude_orphans,
            since=since, before=before, order_by=order_by,
            owner=owner, owner_strict=owner_strict, capacity=capacity, project_id=project_id,
        )
        coverage=coverage_warning(capacity,project_id)
        if coverage not in pack.warnings:
            pack.warnings.insert(0,coverage)
        pack.text_bundle = "> Scope: " + coverage + "\n\n" + pack.text_bundle
        if _M:
            _ctx["nodes"] = len(pack.nodes)
            _ctx["tokens"] = pack.used_tokens_est
            _ctx["truncated"] = pack.truncated
        return pack


def _recency_seed(conn: sqlite3.Connection, since: str | None, before: str | None,
                  limit: int = 24, owner: str | None = None,
                  owner_strict: bool = False) -> list[dict]:
    """Pure temporal seeding for an empty topic — newest nodes in the time window.
    Lets an agent ask 'what changed last week' without a keyword.

    AUDIT FIX (Leg 2 fix round 4, pre-existing defect). This used to push the window and the
    ordering into SQL as `last_modified >= ?` / `<= ?` / `ORDER BY last_modified DESC` —
    STRING comparisons against a column the rest of this file documents as holding mixed
    formats: naive-local, offset-suffixed, space-separated, and `Z`. `_query_impl` was fixed
    for exactly this on 2026-07-20 (`_parse_ts` + `_in_window`, "lexicographic string
    comparison misordered windows"), and `resolve_time` still carries the comment explaining
    that it deliberately emits naive-local because an offset suffix "made lexicographic SQL
    compares misorder". This function was the one place that never got the fix, and it is the
    ONLY seeding path for a bare "what changed recently" — so the query whose entire content
    is a date range was the one ranking dates by string.

    Concretely: `2026-08-04 09:00:00` sorts BEFORE `2026-08-04T08:00:00` (space < `T`), and
    `2026-08-04T08:00:00-04:00` compares as later than `2026-08-04T09:00:00Z` despite being
    four hours earlier. Both mis-filter at the window edge and mis-order inside it.

    Now: pull the candidate keys, parse every timestamp with `_parse_ts` (the same parser
    `_in_window` uses), filter and sort in Python, then fetch full rows for the survivors.
    The id+timestamp scan is two small columns and runs only on this branch — empty topic
    plus a window — which is rare compared with the keyword path.
    """
    since_dt = _parse_ts(since)
    before_dt = _parse_ts(before)

    keys = conn.execute(
        "SELECT id, last_modified FROM nodes "
        "WHERE last_modified IS NOT NULL AND last_modified <> ''"
    ).fetchall()

    dated: list[tuple[datetime, str]] = []
    for row in keys:
        lm = _parse_ts(row["last_modified"])
        if lm is None:                       # unparseable — cannot be placed in a window
            continue
        if since_dt and lm < since_dt:
            continue
        if before_dt and lm > before_dt:
            continue
        dated.append((lm, row["id"]))
    if not dated:
        return []

    # Newest first, by real instant. `limit` is applied AFTER the true sort, so a node cannot
    # miss the pack because its timestamp format sorted badly as text.
    dated.sort(key=lambda p: p[0], reverse=True)

    # owner_strict widens the scan rather than filtering after truncation: taking the newest
    # 24 nodes and then keeping this agent's would usually yield nothing, since one agent owns
    # a minority of the graph. Fetch a generous multiple, filter, then truncate.
    scan = dated[:limit * 40] if (owner and owner_strict) else dated[:limit]
    order = {nid: i for i, (_dt, nid) in enumerate(scan)}

    out: list[dict] = []
    ids = [nid for _dt, nid in scan]
    for i in range(0, len(ids), 400):        # chunked: SQLite caps host parameters
        chunk = ids[i:i + 400]
        for row in conn.execute(
                f"SELECT * FROM nodes WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            out.append(dict(row))

    # owner_strict: hard-restrict to this agent's own nodes, matching `_seed_nodes` exactly.
    # A non-strict `owner` is deliberately NOT filtered here — it is a rank boost applied
    # downstream in `_authority_score`, and filtering on it would silently turn a preference
    # into an exclusion. (Fix round 4: this branch dropped both parameters entirely, so an
    # owner-scoped "what changed recently" returned every agent's nodes.)
    if owner and owner_strict:
        out = [n for n in out
               if (n.get("owner") or "").strip().upper() == owner.strip().upper()]

    out.sort(key=lambda n: order.get(n["id"], 1 << 30))
    return out[:limit]


def _query_impl(
    topic: str,
    radius: int,
    predicates: Iterable[str] | None,
    families: Iterable[str] | None,
    governance_tier_min: int,
    token_budget: int,
    recency_boost: float,
    exclude_orphans: bool,
    since: str | None = None,
    before: str | None = None,
    order_by: str = "relevance",
    owner: str | None = None,
    owner_strict: bool = False,
    capacity: str = "business",
    project_id: str | None = None,
) -> ContextPack:
    since = resolve_time(since)
    before = resolve_time(before, end=True)
    predicates = list(predicates) if predicates else None
    conn = scoped_connection(capacity, project_id, include_telemetry=bool(families and "event" in families))
    try:
        from .memory_policy import load_policy
        load_policy()  # Configured policy errors never become a successful empty pack.
        blocked_predicates = _fully_dangling_requested(conn, predicates)
        effective_predicates = (
            [p for p in predicates if p not in blocked_predicates]
            if predicates is not None else None
        )
        warnings = [coverage_warning(capacity, project_id)] + [f"fully_dangling_predicate:{p}" for p in blocked_predicates]
        historical = bool(before) or bool({"historical", "history", "previous", "formerly", "obsolete"} & set(_TOKEN_RE.findall((topic or "").lower())))
        priority_allowed = (
            families is None and governance_tier_min == 0 and not exclude_orphans
            and since is None and before is None and order_by == "relevance"
            and owner is None and not owner_strict and predicates is None
        )
        current_law_priority = _explicit_current_l016_node(
            conn, topic, historical=historical, enabled=priority_allowed)

        # --- retrieval v2 signals, computed ONCE per query and reused everywhere below ---
        # `sims`  : one MiniLM encode + one 55K-row dot product — the vector seed channel AND
        #           the per-node semantic term AND the semantic half of the relevance floor.
        # `stats` : document frequency + IDF for the query's content terms — the exact-token
        #           pin AND the lexical half of the floor.
        # None/{} when v2 is off or vectors are unavailable, which is the degrade-to-lexical
        # path; nothing below may assume either exists.
        sims = None
        stats: dict[str, tuple[int, float]] = {}
        rares: list[str] = []
        fusion: dict[str, float] = {}
        reserve: dict[str, int] = {}
        v2 = bool(C.RETRIEVAL_V2) and bool((topic or "").strip())
        if v2:
            from . import rank_signals as RS
            if C.VECTOR_SEED_ENABLED:
                from . import vector_seed as VS
                sims = VS.similarity(conn, topic)
            stats = RS.term_stats(conn, _terms(topic))
            rares = RS.rare_tokens(stats)

        # Empty topic + a time window (or recency order) → flat recency listing.
        if not (topic or "").strip() and (since or before or order_by == "recency"):
            # owner/owner_strict are threaded through here too. They used to be dropped on
            # this branch alone, so an owner-scoped "what changed recently" quietly answered
            # with every agent's nodes — a scoping request that failed open (fix round 4).
            seeds = _recency_seed(conn, since, before,
                                  owner=owner, owner_strict=owner_strict)
        else:
            seeds = _seed_nodes(conn, topic, limit=12, owner=owner, owner_strict=owner_strict,
                                sims=sims, out_fusion=fusion, stats=stats,
                                out_reserve=reserve, historical=historical,
                                current_law_priority=current_law_priority)
        from .source_integrity import filter_retrieval
        seeds = filter_retrieval(conn, seeds, historical=historical)
        if not seeds:
            scope = topic or f"[{since or '…'} .. {before or 'now'}]"
            return ContextPack(topic=topic, text_bundle=f"[no nodes matched '{scope}']", warnings=warnings)

        seed_ids = {s["id"] for s in seeds}
        if predicates is not None and not effective_predicates:
            reached, edges = set(seed_ids), []
        else:
            reached, edges = _expand(conn, seed_ids, radius=radius,
                                     predicates=effective_predicates, historical=historical)

        # Pull full node rows for everything reached
        placeholders = ",".join("?" * len(reached))
        rows = conn.execute(
            f"SELECT * FROM nodes WHERE id IN ({placeholders})",
            list(reached),
        ).fetchall()
        nodes = [dict(r) for r in rows]

        from .source_integrity import filter_retrieval
        historical = bool(before) or bool({"historical", "history", "previous", "formerly", "obsolete"} & set(_TOKEN_RE.findall((topic or "").lower())))
        nodes = filter_retrieval(conn, nodes, historical=historical)

        # Filter by family / governance / orphan
        if families:
            fams = set(families)
            nodes = [n for n in nodes if n.get("family") in fams]
        if governance_tier_min > 0:
            nodes = [n for n in nodes if (n.get("governance_tier") or 0) >= governance_tier_min]
        if exclude_orphans:
            edge_endpoint = set()
            for e in edges:
                edge_endpoint.add(e["from_id"])
                edge_endpoint.add(e["to_id"])
            nodes = [n for n in nodes if n["id"] in edge_endpoint]

        # Temporal window filter (last_modified). Active only when since/before given.
        # Nodes without a timestamp are dropped while filtering — they can't be placed.
        if since or before:
            # Datetime-parse both sides — the DB mixes naive-local, offset, and
            # space-separated formats, so lexicographic string comparison
            # misordered windows (review 2026-07-20).
            since_dt = _parse_ts(since)
            before_dt = _parse_ts(before)

            def _in_window(n: dict) -> bool:
                lm = _parse_ts(n.get("last_modified"))
                if lm is None:
                    return False
                if since_dt and lm < since_dt:
                    return False
                if before_dt and lm > before_dt:
                    return False
                return True
            nodes = [n for n in nodes if _in_window(n)]
            if not nodes:
                scope = topic or f"[{since or '…'} .. {before or 'now'}]"
                return ContextPack(topic=topic,
                                   text_bundle=f"[no nodes in time window for '{scope}']", warnings=warnings)

        # Rank by centrality * recency_boost + seed bonus
        def score(n: dict) -> float:
            c = float(n.get("centrality") or 0.0)
            recency = 1.0 if n.get("last_modified") else 0.5
            seed_bonus = 0.5 if n["id"] in seed_ids else 0.0
            return _relevance_score(n, topic, owner=owner) + c + recency * recency_boost + seed_bonus

        superseded_ids: frozenset = frozenset()
        if v2:
            from . import rank_signals as RS
            _now_ref = datetime.now(timezone.utc).astimezone()
            if C.JOURNAL_SUPERSESSION and not historical:
                superseded_ids = RS.journal_superseded(conn)

        def score_v2(n: dict) -> float:
            'Fusion + authority + damped lexical + semantic + real age decay + exact-token pin.'
            s = C.RRF_SCALE * fusion.get(n["id"], 0.0)
            s += _authority_score(n, owner=owner)
            s += _recency_durable_source_score(n, topic)
            if C.IDF_LEXICAL_WEIGHTING:
                s += C.LEXICAL_WEIGHT * _lexical_score_idf(n, topic, stats)
            else:
                s += C.LEXICAL_WEIGHT * _lexical_score(n, topic)
            s += float(n.get("centrality") or 0.0)
            s += C.AGE_DECAY_WEIGHT * RS.age_decay(n.get("last_modified"), now=_now_ref)
            if sims is not None and not _semantically_untrustworthy(n):
                # Explicit default, matching `fusion.get(id, 0.0)` above. Most candidates
                # here have NO vector rank — the graph walk reaches far more nodes than the
                # vector channel ever ranked — so this is the common path.
                s += C.VECTOR_SIM_WEIGHT * sims.get(n["id"], 0.0)
            if rares and not C.EXACT_TOKEN_TRUE_PIN:
                hits = RS.exact_token_hits(n, rares)
                if hits:
                    s += min(C.EXACT_TOKEN_PIN * len(hits), C.EXACT_TOKEN_PIN_MAX)
            if n["id"] in seed_ids:
                s += 0.5
            return s

        def rank_key_v2(n: dict):
            """(pin_tier, score) — or (pin_tier, validity_tier, score) when
            `C.TEMPORAL_VALIDITY_TIER` is on. A TRUE exact-token pin, not a large boost.

            The card is unambiguous — a corpus-rare token matched literally "pins that hit to
            the top REGARDLESS of fusion score" (Cerebras: "no amount of semantic similarity
            should outrank" a pasted error match). An additive boost cannot deliver that at any
            magnitude: it is always a number competing with other numbers, so there is always a
            fusion+similarity combination that beats it. A lexicographic tier is the only shape
            that means "regardless".

            The pin tier is the COUNT of distinct rare query tokens matched, so a document
            holding both halves of a two-token error string outranks one holding either half,
            and every pinned document outranks every unpinned one. Score orders within a tier.

            The validity tier (Leg 4, retrieval-honesty 2026-08-06) is the same argument
            applied to staleness: 1 for a live node (no `valid_until`, or one still in the
            future), 0 for a node whose `valid_until` has passed. It sits BELOW the pin tier
            (an exact-token pin still wins — a pasted error string from an expired doc is
            still the literal match the user is holding) and ABOVE score, so an expired node
            can never outrank a live one on fused score alone, yet is never excluded from the
            pack — it is still the answer when it is the ONLY node with one.
            """
            pin = (len(RS.exact_token_hits(n, rares))
                   if rares and C.EXACT_TOKEN_TRUE_PIN else 0)
            if C.TEMPORAL_VALIDITY_TIER:
                archival = (is_archive_path(n.get("path"))
                            or (n.get('status') or '').lower() in {'archived','superseded','retired'}
                            or (C.HISTORICAL_COPY_RANKING and _RS.is_historical_copy(n.get("path")))
                            or n["id"] in superseded_ids)
                archive_tier = 1 if historical or not archival else 0
                if _RECENCY_TERMS & set(_TOKEN_RE.findall((topic or "").lower())):
                    return (archive_tier, _validity_tier(n, _now_ref), pin, score_v2(n))
                return (archive_tier, pin, _validity_tier(n, _now_ref), score_v2(n))
            return (pin, score_v2(n))

        nodes.sort(key=(rank_key_v2 if v2 else score), reverse=True)
        if current_law_priority is not None:
            priority_id = current_law_priority["id"]
            # Stable: only the explicit canonical current-law result moves; every
            # other node keeps the order selected by the existing ranking policy.
            nodes.sort(key=lambda node: node["id"] != priority_id)

        if v2 and len(nodes) > 5:
            # Keep the established top four intact, while guaranteeing one supporting slot
            # for the best durable source found through title concepts. This prevents a
            # maintained answer from being displaced by graph expansion without letting a
            # title-shaped near miss seize top-1.
            title_reserve = next((n for n in nodes if reserve.get(n["id"]) == 2), None)
            if title_reserve is not None:
                old_rank = nodes.index(title_reserve)
                if old_rank >= 5:
                    nodes = [*nodes[:4], title_reserve,
                             *nodes[4:old_rank], *nodes[old_rank + 1:]]

        # order_by=recency → newest first (relevance still chose WHAT matched; this
        # chooses the ORDER). Applied after the relevance sort so it fully governs order.
        if order_by == "recency":
            nodes.sort(key=lambda n: (n.get("last_modified") or ""), reverse=True)

        # 6b: collapse duplicates at retrieval time. Nodes are already score-sorted, so
        # the first occurrence is the canonical/highest-scored copy.
        #   - by PATH: id-churn twins (same path, many ids) Phase 7's GC will purge.
        #   - by TITLE: backup/migrated copies of one memory sit under DISTINCT paths
        #     (HISTORICAL_AI_BRAIN_EVIDENCE, session_backups, AI_BRAIN/memory_root,
        #     INCUBATING project copies) that ARCHIVE_MARKERS doesn't catch, so path-dedup
        #     keeps them all and a single memory shows up many times — burying diverse
        #     context. Untitled nodes (rare) are never collapsed on title.
        deduped: list[dict] = []
        seen_paths: set[str] = set()
        seen_titles: set[str] = set()
        seen_fps: set[str] = set()
        for n in nodes:
            # Check ALL three guards before mutating ANY seen-set — adding early
            # let a node rejected by a later guard poison the sets, silently
            # dropping every subsequent node sharing its path/title even when
            # that one would have been the only surviving copy (review 2026-07-20).
            p = _RS.journal_source_path(n) or n.get("path")
            if p and p in seen_paths:
                continue
            t = _norm_title(n.get("title"))
            if t and t in seen_titles:
                continue
            # Content-fingerprint dedup: collapses near-identical restatements whose
            # TITLES differ (date prefix, casing, -compact suffix, 10_/11_ path churn)
            # so title-dedup misses them. Nodes are score-sorted, so the first occurrence
            # is the canonical/highest-scored copy and the rest are padding.
            fp = _content_fingerprint(n)
            if fp and fp in seen_fps:
                continue
            if p:
                seen_paths.add(p)
            if t:
                seen_titles.add(t)
            if fp:
                seen_fps.add(fp)
            deduped.append(n)
        nodes = deduped

        # --- per-source cap (Leg 5) -------------------------------------------------
        # Dedup above collapses copies of ONE document. This bounds how many DISTINCT
        # documents from one folder may hold the head of the pack, so a single shelf
        # cannot answer every question by itself.
        #
        # DEMOTION, not deletion: over-cap nodes go to the tail in their original
        # relative order, so the only thing this can cost is RANK — never a node, never a
        # citation. Measured against the golden set before shipping; see the Leg 5 report.
        if C.PACK_PER_SOURCE_CAP > 0 and len(nodes) > C.PACK_PER_SOURCE_CAP:
            per_source: dict[str, int] = {}
            head_nodes: list[dict] = []
            demoted: list[dict] = []
            for n in nodes:
                src = _pack_source(n)
                seen_src = per_source.get(src, 0)
                # A node with no path has no source to crowd; never demote it.
                if src and seen_src >= C.PACK_PER_SOURCE_CAP:
                    demoted.append(n)
                    continue
                per_source[src] = seen_src + 1
                head_nodes.append(n)
            if demoted:
                warnings = warnings + [f"per_source_cap_demoted:{len(demoted)}"]
            nodes = head_nodes + demoted

        # --- relevance floor (v2) ---------------------------------------------------------
        # "Always answers" is a defect. At the Leg 1 baseline, four of five golden questions
        # with NO answer anywhere in the corpus were answered anyway, 11-16 nodes each — and
        # this pack is auto-injected into every agent's context on every turn by the RAG hook,
        # so a confident-looking pack of near-misses is not a harmless non-answer, it is
        # continuous low-grade context pollution.
        #
        # Confidence is ABSOLUTE (see rank_signals.pack_confidence): the best of
        # max(cosine, IDF-weighted lexical coverage) over the head of the pack. Absolute
        # matters — a relative floor ("keep anything within X of the top") can never say
        # "nothing here", because there is always a top.
        #
        # The floor only fires when there is something to judge WITH. If the vectors are
        # unavailable AND every query term prices out at zero IDF (the shape a hermetic
        # fixture graph with no FTS index has), confidence is 0.0 for structural reasons, and
        # flooring on that would convert a degraded signal into a silent no-answer. A missing
        # signal must cost its own contribution, never the whole request.
        #
        # The rare-token bypass is OFF by default — measured, it fired on four of the five
        # negative controls. See `config.RELEVANCE_FLOOR_RARE_TOKEN_BYPASS`.
        if v2 and nodes:
            from . import rank_signals as RS
            have_signal = sims is not None or any(i > 0 for _df, i in stats.values())
            head = nodes[:C.RELEVANCE_FLOOR_DEPTH]
            exact_id_lookup = any(str(n['id']).casefold() == topic.strip().casefold() for n in nodes)
            bypass = (not have_signal) or (
                bool(C.RELEVANCE_FLOOR_RARE_TOKEN_BYPASS and rares)
                and any(RS.exact_token_hits(n, rares) for n in head))
            bypass = bypass or exact_id_lookup
            if not bypass:
                # Leg 5 (2026-08-06): TWO_FLOOR_CONFIDENCE replaces the single
                # max(sim, lex) >= RELEVANCE_FLOOR test with two independently-sufficient
                # floors plus a narrow corroboration band — see config.TWO_FLOOR_CONFIDENCE.
                # RELEVANCE_FLOOR=0 (off) always takes the v1 single-floor branch below.
                if C.TWO_FLOOR_CONFIDENCE:
                    ok, conf = RS.pack_passes_floor(head, stats, sims=sims)
                    if priority_allowed:
                        # Verified-journal answer resolution (v11 hook, real journals): a cached
                        # judge YES leads with that journal; no verdict = unchanged behaviour.
                        from . import evidence_fields
                        nodes, ok = evidence_fields.resolve(topic, nodes, conn, ok)
                else:
                    conf = RS.pack_confidence(head, stats, sims=sims)
                    ok = conf >= C.RELEVANCE_FLOOR
                if not ok:
                    if C.TWO_FLOOR_CONFIDENCE:
                        failure = (
                            f"best aggregate confidence {conf:.2f}; no candidate satisfied "
                            "the two-floor gate ("
                            f"lexical >= {C.LEXICAL_FLOOR:.2f} with rare-mass/similarity "
                            f"sanity, semantic >= {C.SEMANTIC_FLOOR:.2f}, or "
                            f"semantic >= {C.CORROBORATION_SIM:.2f} with lexical >= "
                            f"{C.CORROBORATION_LEX_MIN:.2f})"
                        )
                    else:
                        failure = (
                            f"best confidence {conf:.2f} < floor "
                            f"{C.RELEVANCE_FLOOR:.2f}"
                        )
                    return ContextPack(
                        topic=topic,
                        text_bundle=(f"{C.LOW_CONFIDENCE_MARKER} nothing in the Brain answers "
                                     f"'{topic}' — {failure}; returning no context rather "
                                     f"than near-misses]"),
                        warnings=warnings + [f"low_confidence_empty:{conf:.3f}"],
                    )

        # Token-budget the bundle
        budget_chars = token_budget * C.APPROX_CHARS_PER_TOKEN
        used_chars = 0
        kept_nodes: list[dict] = []
        bundle_parts: list[str] = [f"# Context Pack — {topic}\n"]
        if blocked_predicates:
            bundle_parts.append(
                "\n> STRUCTURAL RELATION UNAVAILABLE: fully dangling predicate(s) "
                + ", ".join(blocked_predicates)
                + ". No edge from these classes was used.\n\n"
            )
        # Graded emission (Leg 5): the top `PACK_BODY_WINNERS` nodes carry body text,
        # the rest stay summary-only. A winner that does not fit is DEMOTED to
        # summary-only before it may be dropped — see config.PACK_BODY_WINNERS for why
        # that fallback is the whole safety property.
        for rank, n in enumerate(nodes):
            want_body = rank < C.PACK_BODY_WINNERS
            chunk = _node_lines(n, C.PACK_BODY_CHARS if want_body else 0)
            if used_chars + len(chunk) > budget_chars:
                if not want_body:
                    break
                chunk = _node_lines(n, 0)
                if used_chars + len(chunk) > budget_chars:
                    break
            kept_nodes.append(n)
            bundle_parts.append(chunk)
            used_chars += len(chunk)
        truncated = len(kept_nodes) < len(nodes)

        # Edges among the kept nodes — queried directly so the graph-pack is COMPLETE
        # regardless of the BFS cap. The cap only bounds which NODES we discover; the
        # connections between the final (token-budgeted, ~20) nodes are always fully
        # shown. Bounded by the kept-set size, so this stays cheap. Mirrors _expand's
        # predicate policy (explicit `predicates=` else exclude the embedding-noise edges).
        kept_ids = {n["id"] for n in kept_nodes}
        kept_edges: list[dict] = []
        if len(kept_ids) > 1:
            kp = ",".join("?" * len(kept_ids))
            if effective_predicates:
                pclause = f" AND predicate IN ({','.join('?' * len(effective_predicates))})"
                pparams = list(effective_predicates)
            else:
                pclause = f" AND predicate NOT IN ({','.join('?' * len(_EXPAND_EXCLUDE))})"
                pparams = list(_EXPAND_EXCLUDE)
            erows = conn.execute(
                f"""SELECT from_id, to_id, predicate, family, weight, confidence, asserted_by
                    FROM edges
                    WHERE from_id IN ({kp}) AND to_id IN ({kp}){pclause}""",
                (*kept_ids, *kept_ids, *pparams),
            ).fetchall()
            kept_edges = [dict(r) for r in erows]

        if kept_edges:
            bundle_parts.append("\n## Edges\n")
            for e in kept_edges[:200]:
                bundle_parts.append(
                    f"- `{e['from_id']}` —[{e['predicate']}]→ `{e['to_id']}`  "
                    f"(w={(e['weight'] or 0.0):.2f}, conf={(e['confidence'] or 0.0):.2f}, by={e['asserted_by']})\n"
                )

        citations = sorted({n["path"] for n in kept_nodes if n.get("path")})
        bundle = "".join(bundle_parts)

        return ContextPack(
            topic=topic, nodes=kept_nodes, edges=kept_edges,
            text_bundle=bundle, citations=citations,
            warnings=warnings,
            truncated=truncated, used_tokens_est=used_chars // C.APPROX_CHARS_PER_TOKEN,
        )
    finally:
        conn.close()


def status() -> dict:
    conn = DB.connect()
    try:
        s = DB.stats(conn)
        from .capacity_admission import deployment_status
        s["capacity_admission"] = deployment_status()
        # `edges` above is the RAW total, and on its own it lies — it counts edges whose
        # endpoints no longer exist. Ship the walkable census beside it so the number a
        # human reads is the number that means something (see db.edge_health).
        s["edge_health"] = DB.edge_health(conn)
        from .source_integrity import health as source_health
        s["source_integrity"] = source_health(conn)
        s["node_families"] = DB.family_counts(conn, "nodes")
        s["top_predicates"] = dict(list(DB.predicate_counts(conn).items())[:15])
        s["orphan_count"] = len(DB.orphan_ids(conn))
        # Which ranking path is actually serving, and whether the embeddings are actually
        # being READ. They were built for months and used only at index time; without this,
        # "retrieval v2 is live" and "the model silently failed to load and every query quietly
        # fell back to lexical seeding" are indistinguishable from outside the process.
        s["retrieval_v2"] = bool(C.RETRIEVAL_V2)
        try:
            from . import vector_seed as VS
            s["vector_seed"] = VS.available(conn)
        except Exception as exc:  # noqa: BLE001 — /status must never fail on a sub-probe
            s["vector_seed"] = {"error": f"{exc.__class__.__name__}: {exc}"}
        return s
    finally:
        conn.close()


# `status()` is deliberately exhaustive — /status is the documented "full DB.stats +
# edge_health census" surface (http_server.py) and the landmine-canonical place to read
# edge_health from (never /health, which caches a lie under load — see edge_health()'s
# docstring). HTTP callers and the nightly assertion depend on getting everything back.
#
# MCP returns a bounded summary with totals and capped per-predicate maps.
# An omitted count makes truncation visible to the caller.
STATUS_SUMMARY_TOP_N = 10


def status_summary(top_n: int = STATUS_SUMMARY_TOP_N) -> dict:
    """Bounded view of status() safe to hand an LLM agent — see module note above."""
    full = status()
    out = dict(full)
    eh = dict(full.get("edge_health") or {})

    dbp = eh.pop("dangling_by_predicate", {}) or {}
    fdp = eh.pop("fully_dangling_predicates", []) or []

    # dangling_by_predicate arrives pre-sorted worst-first (db.edge_health sorts by
    # -count before returning it) — taking the head is taking the head of the census
    # that actually matters, not an arbitrary slice.
    top_dbp = dict(list(dbp.items())[:top_n])
    top_fdp = sorted(fdp)[:top_n]

    eh["dangling_by_predicate_top"] = top_dbp
    eh["dangling_by_predicate_omitted"] = max(0, len(dbp) - len(top_dbp))
    eh["fully_dangling_predicates_top"] = top_fdp
    eh["fully_dangling_predicates_omitted"] = max(0, len(fdp) - len(top_fdp))

    out["edge_health"] = eh
    out["_summary"] = True
    out["_note"] = (
        "Bounded summary — per-predicate dangling census capped to top "
        f"{top_n} of each. Full census: brain.status() (CLI `brain status`, "
        "HTTP GET /status)."
    )
    return out


def _cli():
    import argparse, json
    ap = argparse.ArgumentParser(prog="brain")
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("query")
    q.add_argument("topic")
    q.add_argument("--radius", type=int, default=C.DEFAULT_RADIUS)
    q.add_argument("--budget", type=int, default=C.DEFAULT_TOKEN_BUDGET)
    q.add_argument("--families", nargs="*", default=None)
    q.add_argument("--predicates", nargs="*", default=None)
    q.add_argument("--tier", type=int, default=0)
    q.add_argument("--since", default=None, help="ISO date/datetime or relative (7d, 2w, 3mo, 1y)")
    q.add_argument("--before", default=None, help="ISO date/datetime or relative")
    q.add_argument("--order-by", dest="order_by", default="relevance",
                   choices=["relevance", "recency"])
    q.add_argument("--capacity", choices=["business", "personal"], default="business")
    q.add_argument("--project-id", default=None)
    q.add_argument("--json", action="store_true")
    sub.add_parser("status")
    args = ap.parse_args()
    if args.cmd == "status":
        print(json.dumps(status(), indent=2))
        return
    pack = query(args.topic, radius=args.radius, token_budget=args.budget,
                 families=args.families, predicates=args.predicates,
                 governance_tier_min=args.tier, since=args.since, before=args.before,
                 order_by=args.order_by, capacity=args.capacity, project_id=args.project_id)
    if args.json:
        print(json.dumps(pack.to_dict(), indent=2))
    else:
        print(pack.text_bundle)


if __name__ == "__main__":
    _cli()
