"""Load ENTITY_ALIASES.md → returns canonical_id → set(aliases) and reverse map."""

from __future__ import annotations
import re
from pathlib import Path
from . import config as C


_TABLE_ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*(.+?)\s*\|\s*$")
_WORD_RE = re.compile(r"[a-z0-9]+")


def load_aliases(path: Path | None = None) -> tuple[dict[str, list[str]], dict[str, str]]:
    p = path or C.ENTITY_ALIASES
    if not p.exists():
        return {}, {}
    canonical_to_aliases: dict[str, list[str]] = {}
    alias_to_canonical: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        m = _TABLE_ROW_RE.match(line.strip())
        if not m:
            continue
        canonical = m.group(1).strip()
        aliases_field = m.group(2)
        aliases = [a.strip() for a in aliases_field.split(",") if a.strip()]
        if canonical and aliases:
            canonical_to_aliases.setdefault(canonical, []).extend(aliases)
            for a in aliases:
                alias_to_canonical[a.lower()] = canonical
            alias_to_canonical[canonical.lower()] = canonical
    return canonical_to_aliases, alias_to_canonical


_CACHE: tuple[dict[str, list[str]], dict[str, str]] | None = None
_CACHE_PATH: Path | None = None


def get_aliases(path: Path | None = None) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Cached `load_aliases()` (Leg 3, retrieval-honesty 2026-08-06).

    `engine.brain._seed_channels` calls this on every query that has QUERY_ALIAS_EXPANSION
    on; re-parsing ~500 lines of markdown per query is cheap in isolation but needless when
    ENTITY_ALIASES.md changes on Founder-review cadence, not per-request. Tests that repoint
    `C.ENTITY_ALIASES` at a fixture (or edit the live file mid-suite) must call reset_cache()
    between cases — mirrors the vector_seed / rank_signals cache-reset pattern."""
    global _CACHE, _CACHE_PATH
    p = path or C.ENTITY_ALIASES
    if _CACHE is None or _CACHE_PATH != p:
        _CACHE = load_aliases(p)
        _CACHE_PATH = p
    return _CACHE


def reset_cache() -> None:
    global _CACHE, _CACHE_PATH
    _CACHE = None
    _CACHE_PATH = None


def expand_query_terms(topic: str, terms: list[str],
                        canonical_to_aliases: dict[str, list[str]],
                        cap: int | None = None) -> list[str]:
    """Widen a Founder-phrased query's seed terms through registered alias phrases.

    Every paraphrase fixture in the golden set deliberately withholds the corpus's own
    jargon ("Founder-phrased credential question... without the words ArcturionGate,
    broker, or op_client"). `_terms(topic)` tokenizes only what the Founder actually typed,
    so the FTS OR-seed and title-substring channels never see the vocabulary the target
    document is written in — the seeding-stage failure Legs 2/4 traced common-token and
    paraphrase misses to.

    Matching is PHRASE-level and WHOLE-WORD (word-boundary regex against the lowercased
    topic), not per-token and not fuzzy: an alias must be spelled out in the query for its
    canonical entry to fire. On a hit, every OTHER word from that canonical's alias list
    (plus the canonical id's own words, since some canonicals here are corpus-jargon
    entries rather than person/agent names) is added to the term list, deduped against what
    `_terms()` already found, capped at `cap` new terms so one alias entry cannot swamp the
    seed budget.

    Deliberately does NOT touch `RS.term_stats` / the exact-token pin — those must stay tied
    to tokens the Founder literally typed; expansion only widens what gets a SEED slot.

    Per-canonical contributions are bounded so multiple alias groups remain represented.
    """
    ql = (topic or "").strip().lower()
    if not ql or not canonical_to_aliases:
        return terms
    cap = C.QUERY_ALIAS_EXPANSION_MAX_TERMS if cap is None else cap
    per_canonical_cap = max(2, cap // 2)
    seen = set(terms)
    added: list[str] = []
    for canonical, aliases in canonical_to_aliases.items():
        hit = False
        for a in aliases:
            a_low = a.strip().lower()
            if len(a_low) > 2 and re.search(r"\b" + re.escape(a_low) + r"\b", ql):
                hit = True
                break
        if not hit:
            continue
        local_added = 0
        for phrase in aliases:
            for tok in _WORD_RE.findall(phrase.lower()):
                if len(tok) > 2 and tok not in seen:
                    seen.add(tok)
                    added.append(tok)
                    local_added += 1
                    if local_added >= per_canonical_cap:
                        break
            if local_added >= per_canonical_cap:
                break
        # No early `break` on the outer loop — every canonical that fires gets its (capped)
        # turn; only the FINAL slice below bounds the total.
    return terms + added[:cap] if added else terms


def synonymous_edges(canonical_to_aliases: dict[str, list[str]]) -> list[dict]:
    """Emit synonymous_with edges (canonical -> each alias-as-id)."""
    from .util import now_iso
    edges: list[dict] = []
    asserted_at = now_iso()
    for canonical, aliases in canonical_to_aliases.items():
        for alias in aliases:
            edges.append({
                "from": canonical, "to": alias,
                "predicate": "synonymous_with", "family": "semantic",
                "weight": 1.0, "confidence": 1.0,
                "asserted_by": "ENTITY_ALIASES_MD", "asserted_at": asserted_at,
                "evidence_span": "alias-dictionary", "metadata": {},
            })
    return edges
