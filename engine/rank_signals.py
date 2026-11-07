"""Ranking signals for retrieval v2 (Leg 2, retrieval-honesty 2026-08-04).

Four capabilities the Brain already had the raw material for, and read at query time
nowhere. Each one closes a class that Leg 1's honest golden gate measured as broken:

  * **Reciprocal-rank fusion** (`rrf`) — the ranker used to UNION every seed channel and then
    re-score the union with a hand-written linear function that has no term-rarity component.
    bm25 was computed to pick candidates and then thrown away. Fusion scores a document by
    where it RANKS in each channel, so consensus across channels beats one loud signal and
    bm25's IDF finally reaches the final ordering. Closes `common-token` (baseline 0.000).
  * **Document frequency / rare tokens** (`document_frequency`, `rare_tokens`, `idf`) — the
    corpus-rarity numbers the ranker never had. Used for the exact-token pin and for the
    relevance floor's lexical half.
  * **Exponential age decay** (`age_decay`) — replaces a binary 0.3 "has a timestamp" flag
    that file mtime always satisfies, so a 2024 note and a 2026 note scored identically.
    Closes `recency` (baseline 0.250). A TIEBREAK weight, never a filter.
  * **Answer confidence** (`node_confidence`, `pack_confidence`) — the absolute measure that
    lets the Brain say "I don't know". Closes `negative` (baseline 0.200: four of five
    questions with no answer in the corpus were answered anyway, 11-16 nodes each).

Everything here is pure or read-only, and nothing raises on a degraded input: a query runs
behind a live HTTP service, and a signal that cannot be computed must cost its own
contribution, never the request.
"""

from __future__ import annotations

import math
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone

from . import config as C

def _aware(dt):
    """A datetime guaranteed comparable with any other `_aware` result, or None.

    A naive datetime is read as UTC. That choice is arbitrary but it must be MADE: mixing
    naive and aware operands raises TypeError, and the alternative reading (local time) would
    make the same stored timestamp decay differently depending on the machine's zone."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


_PARSE_TS = None


def _brain_parse_ts():
    """`brain._parse_ts`, resolved once. Lazy because `brain` imports this module's siblings
    and a module-level import would close the cycle; cached because `age_decay` runs per
    candidate and a `sys.modules` round-trip per node is not free at 3,000 nodes."""
    global _PARSE_TS
    if _PARSE_TS is None:
        from .brain import _parse_ts
        _PARSE_TS = _parse_ts
    return _PARSE_TS

# ---------------------------------------------------------------- reciprocal-rank fusion


def rrf(lists: dict[str, list[str]], weights: dict[str, float] | None = None,
        k: int | None = None) -> dict[str, float]:
    """Reciprocal-rank fusion: score(d) = Σ_channels w_c / (k + rank_c(d)), rank 1-based.

    `lists` maps a channel name to that channel's ids in ITS OWN rank order. A document
    missing from a channel simply contributes nothing for it — no imputation, no penalty.

    Why rank and not score: the channels are not commensurable. bm25 is a negative
    log-odds-ish score, cosine is [-1, 1], "the title contains the query" is a boolean. Any
    attempt to add them needs per-channel calibration that drifts the moment the corpus does.
    Rank is the one thing every channel agrees on the meaning of, and `k` (60, the standard)
    flattens the head so the #1 of a weak channel cannot outvote the #1-#3 of a strong one.
    """
    k = C.RRF_K if k is None else k
    weights = weights if weights is not None else C.RRF_WEIGHTS
    out: dict[str, float] = {}
    for channel, ids in lists.items():
        w = weights.get(channel, 1.0)
        if not w:
            continue
        seen: set[str] = set()
        rank = 0
        for nid in ids:
            if nid in seen:      # a channel that repeats an id must not double-vote for it
                continue
            seen.add(nid)
            rank += 1
            out[nid] = out.get(nid, 0.0) + w / (k + rank)
    return out


# ---------------------------------------------------------------- corpus term rarity

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# token -> (document_frequency, computed_at). One MATCH-count per token is ~1-2 ms on the
# live index, and query vocabulary repeats heavily across turns, so a small process-local
# cache keeps the whole signal well inside the latency budget. TTL'd rather than permanent
# because the watcher grows the corpus every ~20 s.
# LRU, not a plain dict: the previous version cleared the WHOLE cache on reaching the cap,
# which turned a steady stream of new vocabulary into periodic cliffs where every token in
# the next query went back to the DB at once. Oldest-first eviction keeps the hot terms
# ("agent", "brain", "session") resident and evicts only the tail.
_DF_CACHE: "OrderedDict[str, tuple[int, float]]" = OrderedDict()
_DF_LOCK = threading.RLock()
_DF_TTL = 900.0
_DF_CACHE_MAX = 4096

# Total indexed documents, cached the same way — the denominator of every IDF.
_NDOCS: tuple[int, float, str] | None = None


_WARNED: set[str] = set()


def _warn_once(key: str, msg: str) -> None:
    """One line to stderr per distinct problem per process. A degraded signal must be quiet
    enough not to spam a per-turn service and loud enough not to die unnoticed."""
    if key not in _WARNED:
        _WARNED.add(key)
        import sys
        print(f"[brain] rank-signal degraded: {msg}", file=sys.stderr)


def _now() -> float:
    return time.time()


def reset_cache() -> None:
    """Drop the DF/N caches. Tests point `C.BRAIN_DB` at a temp fixture between cases, so a
    document frequency measured against the live graph must not leak into one of them."""
    global _NDOCS
    with _DF_LOCK:
        _DF_CACHE.clear()
        _NDOCS = None


def _scope(conn=None) -> str:
    """Which index a cached frequency was measured against.

    Include the index and access scope so cached frequencies from one corpus do not
    change rarity calculations for another."""
    from .capacity_admission import scope_key
    return str(C.BRAIN_DB) + (":" + scope_key(conn) if conn is not None else "")


def corpus_size(conn: sqlite3.Connection) -> int:
    """Indexed document count (the IDF denominator). 0 if unavailable."""
    global _NDOCS
    scope = _scope(conn)
    with _DF_LOCK:
        if _NDOCS is not None and _NDOCS[2] == scope and (_now() - _NDOCS[1]) < _DF_TTL:
            return _NDOCS[0]
    try:
        n = int(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
    except sqlite3.Error:
        return 0
    with _DF_LOCK:
        _NDOCS = (n, _now(), scope)
    return n


def document_frequency(conn: sqlite3.Connection, token: str) -> int:
    """How many indexed documents contain `token`, via the FTS index.

    Deliberately a `MATCH` count rather than an `fts5vocab` table: fts5vocab must live in the
    same schema as its source table, so using it would mean a DDL write against a 1.19 GB live
    index to read a number. The count is ~1-2 ms and cached.

    THE TOKEN IS NOT TRUSTED. It is interpolated into an FTS5 MATCH expression, which is its
    own query language: a `"` closes the phrase and everything after it is parsed as FTS5
    syntax (`OR`, `NEAR`, `*`, column filters). Nothing here is a database write, so the worst
    case is a wrong number or an exception rather than data loss — but a ranking signal that
    can be steered by the text of a question is a ranking signal that cannot be trusted, and
    `_TOKEN_RE` was declared for exactly this and then never enforced. Tokens are now reduced
    to their `[a-z0-9]+` runs before the phrase is built, so no FTS5 metacharacter can survive;
    the embedded quote is doubled as well, which is redundant after that and is kept because
    defence in depth costs one line.

    Note that only the FREQUENCY lookup is sanitized. `exact_token_hits` still matches the
    caller's original token literally against node text — that is a plain Python substring
    test with no expression language behind it, so `local_llm_client.py` and
    `X-Payment-Authorization-Token` keep working as pins. In the live path the two are
    identical anyway: real query terms arrive from `brain._terms`, which is itself
    `[a-z0-9]+`.

    Returns -1 when the frequency cannot be determined (no FTS5, unusable token). -1 is not 0:
    an unknown frequency must never be read as "this token is unique in the corpus", which is
    what the exact-token pin fires on.
    """
    raw = (token or "").strip().lower()
    if not raw:
        return -1
    # Reduce to the alphanumeric runs FTS5 would have tokenized anyway. A token that is
    # nothing BUT metacharacters yields no runs and is unusable, not "rare".
    parts = _TOKEN_RE.findall(raw)
    if not parts:
        return -1
    safe = " ".join(parts).replace('"', '""')

    key = f"{_scope(conn)}\x00{safe}"
    # The lock is held ACROSS the DB call, not merely around the cache reads. Releasing it
    # first meant every thread that missed on a cold cache issued its own identical query —
    # a thundering herd on exactly the token everyone is asking about. The critical section
    # is one ~1-2 ms indexed count, and only on a miss; it is re-entrant so `_scope`/`_now`
    # helpers stay callable inside.
    with _DF_LOCK:
        hit = _DF_CACHE.get(key)
        if hit is not None and (_now() - hit[1]) < _DF_TTL:
            _DF_CACHE.move_to_end(key)      # LRU: touching a key makes it recent
            return hit[0]
        try:
            df = int(conn.execute(
                "SELECT COUNT(*) FROM fts_nodes JOIN nodes n ON n.id=fts_nodes.node_id WHERE fts_nodes MATCH ?", (f'"{safe}"',)
            ).fetchone()[0])
        except sqlite3.OperationalError as exc:
            # Usually "no such table: fts_nodes" (a fixture graph without FTS) or a malformed
            # MATCH. Both mean "no frequency information", but silence here is how a signal
            # dies unnoticed — the whole point of this programme.
            _warn_once(f"df:{type(exc).__name__}",
                       f"document frequency unavailable ({exc}); term-rarity weighting and "
                       f"the exact-token pin are inert for this query")
            return -1
        except sqlite3.Error as exc:
            _warn_once(f"df:{type(exc).__name__}", f"document frequency failed: {exc}")
            return -1
        while len(_DF_CACHE) >= _DF_CACHE_MAX:
            _DF_CACHE.popitem(last=False)   # evict oldest, never clear the whole cache
        _DF_CACHE[key] = (df, _now())
    return df


def idf(df: int, n_docs: int) -> float:
    """Smoothed inverse document frequency, floored at 0.

    A token the corpus has never seen and a token in every document must not both look
    informative, and an unknown df (-1) is treated as "no information" rather than "maximally
    rare" — see `document_frequency`."""
    if df < 0 or n_docs <= 0:
        return 0.0
    return max(0.0, math.log((n_docs + 1) / (df + 1)))


def term_stats(conn: sqlite3.Connection, terms: list[str]) -> dict[str, tuple[int, float]]:
    """{term: (document_frequency, idf)} for the query's content terms."""
    n = corpus_size(conn)
    out: dict[str, tuple[int, float]] = {}
    for t in terms[:C.RARE_TOKEN_MAX_TERMS]:
        df = document_frequency(conn, t)
        out[t] = (df, idf(df, n))
    return out


def rare_tokens(stats: dict[str, tuple[int, float]]) -> list[str]:
    """The query tokens rare enough that a LITERAL match is near-proof of relevance.

    Bounded on both sides. `df < 0` (unknown) never qualifies; `df == 0` never qualifies
    either — a token absent from the whole corpus cannot pin anything, and treating it as
    maximally rare is exactly how a nonsense query talks itself into confidence. Short tokens
    are excluded because a 3-character rare string is usually a tokenizer artefact.
    """
    return [t for t, (df, _i) in stats.items()
            if len(t) >= C.RARE_TOKEN_MIN_LEN and 0 < df <= C.RARE_TOKEN_MAX_DF]


_HAYSTACK_FIELDS = ("id", "title", "summary", "body_excerpt", "path")


def _haystack(node: dict) -> str:
    """Lowercased id+title+summary+body+path.

    DOES NOT MUTATE `node`. It used to memoise the result back into the caller's dict under
    a private `_rs_haystack` key, which meant a shared, caller-owned structure grew a private field: any
    path that serialized a node wholesale — an MCP response, a JSON dump, a future consumer
    that does not know to strip it — would have shipped the engine's scratch, and the only
    thing preventing that was one `pop` in one function remembering to run.

    Building it twice for a handful of nodes is the cheaper mistake. The measured cost of
    dropping the memo is bounded and small: the exact-token pin builds it once per candidate
    either way, and the only second read is `lexical_coverage` over the relevance floor's
    five-node head — so this is five extra string joins per query, not 3,000."""
    return " ".join(str(node.get(f) or "") for f in _HAYSTACK_FIELDS).lower()


def exact_token_hits(node: dict, rares: list[str]) -> list[str]:
    """Which rare query tokens this node matches LITERALLY (substring, case-folded).

    Substring rather than word-boundary on purpose: the shapes this is for — pasted error
    strings, flag names, file names, header names, IDs — routinely sit inside a larger token
    (`local_llm_client.py` inside a path, `X-Payment-Authorization-Token` inside a quoted
    traceback). The rarity threshold is what keeps that from being noisy."""
    if not rares:
        return []
    hay = _haystack(node)
    return [t for t in rares if t in hay]


# ---------------------------------------------------------------- age decay


def age_decay(last_modified, now=None, halflife_days: float | None = None) -> float:
    """Exponential recency weight in [0, 1]: 1.0 for something modified now, 0.5 at one
    half-life, asymptotically 0.

    Replaces a binary 0.3 "has a timestamp" flag. File mtime always supplies a timestamp, so
    that flag was constant across the whole corpus and a June note and a July note that
    contradict each other ranked identically — the `recency` class's entire failure.

    Undated nodes return 0.0 (no recency evidence), NOT 1.0. Future timestamps are clamped to
    1.0 rather than rewarded: a clock skew or a bad frontmatter date must not buy rank.

    Pass `now` when scoring a whole pack. This runs once per candidate — up to 3,000 per
    query — and a fresh `datetime.now()` per node is both wasted work and a moving reference
    that makes the ordering depend on where in the sort a node happened to fall.

    NAIVE/AWARE MISMATCH CANNOT RAISE HERE. `brain._parse_ts` attaches local tz to a naive
    string, but `now` is a caller-supplied argument and a naive one made `ref - dt` a
    TypeError — inside a loop that runs up to 3,000 times per query, behind a live HTTP
    service, in a module whose contract is that a signal which cannot be computed costs its
    own contribution and never the request. Both operands are normalized to aware (naive is
    read as UTC), and the arithmetic is belt-and-braced with a catch that degrades to 0.0.
    """
    dt = _aware(_brain_parse_ts()(last_modified))
    if dt is None:
        return 0.0
    hl = C.AGE_HALFLIFE_DAYS if halflife_days is None else halflife_days
    if hl <= 0:
        return 1.0
    ref = _aware(now) or datetime.now(timezone.utc)
    try:
        age_days = (ref - dt).total_seconds() / 86400.0
    except (TypeError, OverflowError, ValueError):
        # Unreachable via _aware, but this is the hot loop of a live service: a recency
        # tiebreak is never worth a 500.
        return 0.0
    if age_days <= 0:
        return 1.0
    return float(2.0 ** (-age_days / hl))


# ---------------------------------------------------------------- answer confidence


def lexical_coverage(node: dict, stats: dict[str, tuple[int, float]]) -> float:
    """Fraction of the query's IDF MASS this node accounts for, in [0, 1].

    IDF-weighted, not term-counted, and that is the whole point. "how many goats does the
    Founder own" is four common words and one rare one; a document matching only `founder`
    and `own` has matched almost none of what makes the question specific, and a plain
    term-count would score it 0.5. Weighting by rarity scores it near zero — which is the
    honest reading, and the difference between an answer and a coincidence.
    """
    total = sum(i for _df, i in stats.values())
    if total <= 0:
        return 0.0
    hay = _haystack(node)
    got = sum(i for t, (_df, i) in stats.items() if t in hay)
    return min(1.0, got / total)


def rare_mass_coverage(node: dict, stats: dict[str, tuple[int, float]],
                       min_idf: float | None = None) -> float:
    """Fraction of the query's RARE IDF mass this node accounts for, in [0, 1].

    `lexical_coverage` above normalizes by the query's WHOLE mass, which is what lets a
    document clear the floor while missing every term that makes the question specific: "how
    many goats does the Founder own" scores 0.472 on {many, founder, own} alone, with `goats`
    — 53% of the query's mass, and the only word that makes it a question — absent. Restricting
    both halves of the ratio to terms at or above `min_idf` asks the scale-free version of the
    same question: of what actually DISTINGUISHES this query, how much is here? The goats
    document scores 0.313, which is the honest reading.

    GUARANTEED FALLBACK: a query with no term at or above `min_idf` (all common words, or
    `stats` empty on the v1 path) has no rare mass to measure, and falls back to
    `lexical_coverage`. Returning 0.0 there would make the lexical floor unreachable for a
    whole class of legitimate questions — a refinement that had nothing to measure must not
    read as evidence of absence. See `config.FLOOR_RARE_TERM_MIN_IDF`.
    """
    min_idf = C.FLOOR_RARE_TERM_MIN_IDF if min_idf is None else min_idf
    rare_total = sum(i for _df, i in stats.values() if i >= min_idf)
    if rare_total <= 0:
        return lexical_coverage(node, stats)
    hay = _haystack(node)
    got = sum(i for t, (_df, i) in stats.items() if i >= min_idf and t in hay)
    return min(1.0, got / rare_total)


def node_confidence(node: dict, stats: dict[str, tuple[int, float]], sim: float) -> float:
    """How much this node looks like an ANSWER to the query, in [0, 1], on an absolute scale.

    Two independent kinds of evidence, and the stronger one wins:
      - `sim`: the query and the document mean the same thing (catches paraphrase, where the
        lexical evidence is legitimately near zero).
      - `lexical_coverage`: the document contains the rare, load-bearing words of the query
        (catches pasted error strings, where the cosine of a 4-word fragment is legitimately
        mediocre).

    A maximum, not a sum, because either alone is sufficient evidence and requiring both would
    fail exactly the two classes this leg exists to fix.
    """
    return max(float(sim or 0.0), lexical_coverage(node, stats))


def pack_confidence(nodes: list[dict], stats: dict[str, tuple[int, float]],
                    sims=None, depth: int | None = None) -> float:
    """The best `node_confidence` in the head of the ranked pack.

    Scored over the head rather than the single top node so one unlucky #1 does not throw
    away a pack whose #2 is a clean answer, and over a bounded head rather than everything so
    a 3,000-node BFS expansion cannot manufacture confidence from its tail.
    """
    depth = C.RELEVANCE_FLOOR_DEPTH if depth is None else depth
    best = 0.0
    for n in nodes[:depth]:
        sim = sims.get(n["id"], 0.0) if sims is not None else 0.0
        c = node_confidence(n, stats, sim)
        if c > best:
            best = c
    return best


def node_passes_two_floor(node: dict, stats: dict[str, tuple[int, float]], sim: float,
                          sim_trusted: bool = True) -> bool:
    """Leg 5's two-floor decision for ONE node: does this node, on its own, look enough like
    an answer to keep the pack? See `config.TWO_FLOOR_CONFIDENCE` for the full rationale.

    Three independent sufficient conditions, each closing a class `node_confidence`'s single
    `max(sim, lex)` floor could not separate from a negative:
      - lexical alone (pasted error strings / exact identifiers — mediocre cosine, real coverage)
      - semantic alone (true paraphrase — near-zero lexical evidence, real meaning-match)
      - corroboration band (a sim too weak to trust alone, backed by real — not incidental —
        lexical overlap)
    A `sim` too weak even for the corroboration band, with lexical coverage below
    `CORROBORATION_LEX_MIN`, is exactly the shape of "a couple of common query words happened
    to occur near an unrelated topic" — the failure mode this leg exists to close.

    Refinement 5b (2026-08-07) narrows what makes the LEXICAL branch sufficient ALONE, and
    touches nothing else. Coverage clearing `LEXICAL_FLOOR` is now sufficient alone only when
    it is also:
      - carried by the query's DISTINGUISHING terms (`FLOOR_RARE_COVERAGE_MIN` of the query's
        rare IDF mass), not assembled from the low-IDF remainder of a short question; and
      - not semantically contradicted (`FLOOR_LEX_ALONE_MIN_SIM`) — a document holding every
        rare word of the query at cosine 0.09 is a vocabulary collision, not an answer.
    Both gates can only REMOVE a pass, never add one, so neither can turn a refusal back into
    a confident wrong answer. See `config.LEXICAL_FLOOR_RARE_MASS` for the measurement.

    `sim_trusted` is False for a node with no real cosine — the vector channel is degraded, or
    this node has no embedding. The sanity gate is then skipped rather than failed: a missing
    signal must cost its own contribution, never the request.
    """
    lex = lexical_coverage(node, stats)
    sim = float(sim or 0.0)
    if lex >= C.LEXICAL_FLOOR and _lexical_alone_is_sufficient(node, stats, sim, sim_trusted):
        return True
    if sim >= C.SEMANTIC_FLOOR:
        return True
    return sim >= C.CORROBORATION_SIM and lex >= C.CORROBORATION_LEX_MIN


def _lexical_alone_is_sufficient(node: dict, stats: dict[str, tuple[int, float]],
                                 sim: float, sim_trusted: bool) -> bool:
    """The refinement-5b gates on the lexical-alone branch. Both default ON under
    RETRIEVAL_V2; with both off this is unconditionally True and `node_passes_two_floor`
    reduces to the Leg 5 decision exactly."""
    if C.LEXICAL_FLOOR_RARE_MASS:
        if rare_mass_coverage(node, stats) < C.FLOOR_RARE_COVERAGE_MIN:
            return False
    if C.LEXICAL_FLOOR_SIM_SANITY and sim_trusted:
        if sim < C.FLOOR_LEX_ALONE_MIN_SIM:
            return False
    return True


def pack_passes_floor(nodes: list[dict], stats: dict[str, tuple[int, float]],
                      sims=None, depth: int | None = None) -> tuple[bool, float]:
    """Whether the head of the ranked pack has one node that survives the relevance floor,
    plus the best absolute confidence in that head (for the refusal message / warnings).

    `RELEVANCE_FLOOR` still acts as an outer sanity ceiling here — `max(sim, lex)` for a node
    must clear it before the (finer-grained) two-floor test is even asked. In production
    RELEVANCE_FLOOR (0.42) is the lowest of the four calibrated numbers this leg introduces,
    so the ceiling is inert; it only bites when a caller deliberately overrides
    RELEVANCE_FLOOR, which is what keeps the pre-existing floor mechanics (RELEVANCE_FLOOR=0.9
    must empty every pack, RELEVANCE_FLOOR=0.0 must never floor) working unmodified under
    TWO_FLOOR_CONFIDENCE.

    Whichever path is used (`TWO_FLOOR_CONFIDENCE` on or off), the confidence number returned
    is always `node_confidence`'s `max(sim, lex)` — the refusal message reports one honest
    absolute number regardless of which mechanism made the pass/fail call.

    A node's cosine is TRUSTED only when one really exists for it. `sims` is None outright
    while the encoder loads, and `QuerySimilarity.get` returns its default for any node with no
    embedding — which the graph walk reaches constantly. Refinement 5b's semantic sanity gate
    reads a low cosine as evidence AGAINST a node, so it must be able to tell "measured, and
    unrelated" from "never measured"; passing an explicit `None` default is how that question
    is asked (see `vector_seed.QuerySimilarity.get`, whose `default` parameter exists for
    exactly this kind of call-site-visible contract). Untrusted nodes skip the gate.
    """
    depth = C.RELEVANCE_FLOOR_DEPTH if depth is None else depth
    best_conf = 0.0
    passed = False
    for n in nodes[:depth]:
        raw = sims.get(n["id"], None) if sims is not None else None
        sim_trusted = raw is not None
        sim = float(raw or 0.0)
        c = node_confidence(n, stats, sim)
        if c > best_conf:
            best_conf = c
        if c < C.RELEVANCE_FLOOR:
            continue
        if node_passes_two_floor(n, stats, sim, sim_trusted=sim_trusted):
            passed = True
    return passed, best_conf


# ---------------------------------------------------------------- historical copies (live-brain 2026-11-07)
# Ranking-only. `config.ARCHIVE_MARKERS` also drives the walker's skip rules (util.should_skip_path),
# so widening it would make the nightly GC delete every node under an `Archive/` folder. This
# classifier only orders results: a copy kept in an archive or backup folder is still indexed and
# still returned, it just stops outranking the live record for current questions.
_HISTORICAL_SEGMENTS = frozenset({
    "archive", "archives", "_archive", "archived", "_archived", "old", "_old", "deprecated",
    "_deprecated", "superseded", "_superseded", "backup", "backups", "_backup", "_backups",
    "config_backups", "_config_backups", "session_backups", "trash", ".trash",
})
_HISTORICAL_WORDS = frozenset({"archive", "archives", "archived", "backup", "backups", "deprecated",
                               "superseded", "old", "config", "trash", "bak"})
_HIST_CORE = frozenset({"archive", "archives", "archived", "backup", "backups", "deprecated", "superseded"})


def is_historical_copy(path) -> bool:
    """True when a FOLDER on the path is an archive/backup shelf. A segment qualifies when it is
    one of the named shelves, or when every word in it is a date/number or a historical word
    ("99 🗄 Archive", "2026-05-31_backup") -- so "backup-policy-review" or "archive-tools" do not."""
    if not path:
        return False
    for seg in str(path).replace("\\", "/").split("/")[:-1]:
        s = seg.strip().lower()
        if s in _HISTORICAL_SEGMENTS:
            return True
        words = [w for w in re.split(r"[\s_\-.]+", re.sub(r"[^\w\s\-.]", " ", s)) if w]
        if words and any(w in _HIST_CORE for w in words) and all(w.isdigit() or w in _HISTORICAL_WORDS for w in words):
            return True
    return False


# ---------------------------------------------------------------- journals (live-brain 2026-11-07)
_SUPERSEDED_CACHE: dict = {}


def journal_superseded(conn) -> frozenset:
    """Ids a verified journal marks as superseded (journal --supersedes--> node). Cached; the set
    only changes when journals are rebuilt. Any failure costs only this signal."""
    now = _now()
    hit = _SUPERSEDED_CACHE.get("v")
    if hit and now - hit[0] < _DF_TTL:
        return hit[1]
    try:
        rows = conn.execute(
            "SELECT to_id FROM edges WHERE predicate='supersedes' AND from_id LIKE 'journal:%'").fetchall()
        ids = frozenset(r[0] for r in rows)
    except Exception as exc:  # pragma: no cover - degraded signal, never a failed query
        _warn_once("journal_superseded", f"journal supersession unavailable: {exc}")
        ids = frozenset()
    _SUPERSEDED_CACHE["v"] = (now, ids)
    return ids


def journal_source_path(node: dict):
    """The source path a journal node was derived from, or None for ordinary nodes."""
    if not str(node.get("id") or "").startswith("journal:"):
        return None
    meta = node.get("metadata")
    if isinstance(meta, str):
        try:
            import json as _json
            meta = _json.loads(meta)
        except ValueError:
            return None
    j = (meta or {}).get("journal") if isinstance(meta, dict) else None
    return j.get("source_path") if isinstance(j, dict) else None
