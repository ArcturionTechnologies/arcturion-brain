"""Brain configuration using an explicit caller-owned work root and policy."""

from __future__ import annotations
import os
import sys
import json
from pathlib import Path

# Candidate requires a caller-owned root; importing does not load host registry code.
_work_root = os.environ.get("ARCTURION_BRAIN_WORK_ROOT")
if not _work_root or not Path(_work_root).is_absolute():
    raise RuntimeError("ARCTURION_BRAIN_WORK_ROOT must be an explicit absolute path")
WORK_ROOT = Path(_work_root)
UNIVERSE_ROOT = WORK_ROOT / "universe"
DESKTOP_ROOT = WORK_ROOT / "desktop"
ATLAS_ROOT = WORK_ROOT / "atlas"
RUNTIME_ROOT = WORK_ROOT / "runtime"
PATH_REGISTRY_ROOT = WORK_ROOT / "registry"
CLAUDE_ROOT = WORK_ROOT / "claude"
OWNER_NODE_ID = os.environ.get("ARCTURION_BRAIN_OWNER_NODE_ID", "")
POLICY_HOOK_ROOT = Path(os.environ.get("ARCTURION_BRAIN_POLICY_HOOK_ROOT", str(WORK_ROOT / "unconfigured-hooks")))
PROVIDER_CLI = Path(os.environ.get("ARCTURION_BRAIN_PROVIDER_CLI", str(WORK_ROOT / "unconfigured-provider")))
REMINDERS_CLI = Path(os.environ.get("ARCTURION_BRAIN_REMINDERS_CLI", str(WORK_ROOT / "unconfigured-reminders")))

def _reg(key: str, fallback: Path) -> Path:
    return fallback

# --- Brain v2 (2026-06-16): lean sibling ~/Desktop/ARCTURION_Brain ---
# The brain is its own top-level prong-adjacent engine (NOT a warehouse): engine code
# in <BRAIN_ROOT>/engine, graph data in index/, taxonomy + aliases in canon/. Content
# across the 4 prongs stays in its home and is only INDEXED here. normpath() collapses
# the registry's "../ARCTURION_Brain" so string comparisons (prong_for_path) work.
import os as _os
BRAIN_ROOT = Path(_os.path.normpath(_reg("BRAIN_ROOT", DESKTOP_ROOT / "ARCTURION_Brain")))
GRAPH_DIR = Path(_os.path.normpath(_reg("BRAIN_INDEX", BRAIN_ROOT / "index")))
_canon_override = os.environ.get("ARCTURION_BRAIN_CANON_DIR")
if _canon_override is not None:
    if not _canon_override or not Path(_canon_override).is_absolute():
        raise RuntimeError("ARCTURION_BRAIN_CANON_DIR must be an absolute directory")
    CANON_DIR = Path(_canon_override)
    if not CANON_DIR.is_dir() or not (CANON_DIR / "0007_ONTOLOGY.yaml").is_file():
        raise RuntimeError("ARCTURION_BRAIN_CANON_DIR must contain the ontology")
else:
    CANON_DIR = Path(__file__).resolve().parent.parent / "canon"
WIKI_ROOT = BRAIN_ROOT  # back-compat alias for any legacy WIKI_ROOT reference
BRAIN_DB = GRAPH_DIR / "brain.sqlite"
# Explicit owner opt-in; disabled until the joint integration/admission review.
# This controls both journal installation on normal init and the HTTP read route.
CHANGE_FEED_ENABLED = os.environ.get("ARCTURION_BRAIN_CHANGE_FEED", "") == "1"
EMBEDDINGS_DIR = GRAPH_DIR / "embeddings.qdrant"
COVERAGE_DASHBOARD = GRAPH_DIR / "coverage_dashboard.md"
RECIPROCITY_REPORT = GRAPH_DIR / "reciprocity_report.md"
DRIFT_LOG = GRAPH_DIR / "drift_log.md"
GRAPH_JSON_EXPORT = GRAPH_DIR / "graph.json"
ENTITY_ALIASES = CANON_DIR / "ENTITY_ALIASES.md"
CHANGE_LEDGER = _reg("CHANGE_LEDGER", UNIVERSE_ROOT / "02_GOVERNANCE" / "0201_COVENANT" / "CHANGE_LEDGER.md")
COVENANT_INBOX = _reg("COVENANT_INBOX", UNIVERSE_ROOT / "02_GOVERNANCE" / "0201_COVENANT" / "inbox.md")
LOCAL_LLM_CLIENT = RUNTIME_ROOT / "local_llm_client.py"
DRIFT_SNAPSHOT = GRAPH_DIR / "drift_snapshot.json"
# Where brain_propose_node/edge/promotion stage tickets for COVENANT review. Lives under the
# brain itself (registry-resolved, normpath'd) — the canonical home of all historical proposals.
# Do NOT hardcode a UNIVERSE_ROOT shelf here: a 2026-06-17 edit pointed staging at
# UNIVERSE_ROOT/06_MEMORY_SYSTEM/..., a compat-symlink that later went dangling and aborted
# every proposal universe-wide with [Errno 17] (a dangling symlink defeats mkdir exist_ok=True).
STAGING_DIR = Path(_os.path.normpath(_reg("BRAIN_STAGING", BRAIN_ROOT / "staging" / "proposals")))

CANONICAL_TRIPLES_JSONL = GRAPH_DIR / "triples.jsonl"
CANONICAL_NODE_INDEX_JSONL = GRAPH_DIR / "node_index.jsonl"

# The constellation agents the walker + live watcher iterate. This is the single
# roster of agents whose memory is INDEXED into the shared Brain. Kept identical to
# ACTIVE_CONSTELLATION_AGENTS below — the phantom ARCTURUS_CEO entry was removed
# 2026-06-19 (no on-disk home; it is a sub-identity, now declared in EXCLUDED_AGENTS).
CONSTELLATION_AGENTS = [
    "ALTAIR", "ARCTURUS", "CASSIOPEIA",
    "COVENANT", "IO", "LILITH", "LYRA",
    "NEMESIS", "ORION", "POLARIS", "SIRIUS", "VEGA",
]

# Brain v2 Phase 9: the 12 ACTIVE constellation agents — the single source of truth for
# gravity/graph-index audits. Excludes the retired CODEX (the "phantom" that kept
# reappearing in audits because gravity.py hardcoded its own list) and the ARCTURUS_CEO
# sub-identity. Each must have a live Desktop home; audits guard on existence.
ACTIVE_CONSTELLATION_AGENTS = (
    "ALTAIR", "ARCTURUS", "CASSIOPEIA", "COVENANT", "IO", "LILITH",
    "LYRA", "NEMESIS", "ORION", "POLARIS", "SIRIUS", "VEGA",
)


def _assert_agents_in_sync(all_agents, active_agents) -> None:
    """Fail loudly at import if CONSTELLATION_AGENTS and ACTIVE_CONSTELLATION_AGENTS
    drift apart (2026-08-04 hardening). Before this, the two lists were kept in sync
    by comment only — a phantom CODEX entry drifted in before it was caught. Any
    future edit to one list without the other now raises at import time instead of
    silently reappearing in an audit months later."""
    a, b = set(all_agents), set(active_agents)
    if a != b:
        raise AssertionError(
            "CONSTELLATION_AGENTS and ACTIVE_CONSTELLATION_AGENTS have drifted — "
            f"only in CONSTELLATION_AGENTS: {sorted(a - b)}; "
            f"only in ACTIVE_CONSTELLATION_AGENTS: {sorted(b - a)}"
        )


_assert_agents_in_sync(CONSTELLATION_AGENTS, ACTIVE_CONSTELLATION_AGENTS)

# Sensitive exclusions are supplied by the deployment, not embedded in source.
_policy_path = os.environ.get("ARCTURION_BRAIN_POLICY_PATH", "")
if not _policy_path or not Path(_policy_path).is_absolute():
    raise RuntimeError("ARCTURION_BRAIN_POLICY_PATH must name an absolute JSON file")
try:
    with open(_policy_path, encoding="utf-8") as _f:
        SENSITIVE_POLICY = json.load(_f)
    if not isinstance(SENSITIVE_POLICY, dict):
        raise ValueError("policy must be an object")
    EXCLUDED_AGENTS = SENSITIVE_POLICY["excluded_agents"]
    _skip_fragments = SENSITIVE_POLICY["skip_path_fragments"]
    if not isinstance(EXCLUDED_AGENTS, dict) or any(not isinstance(k, str) or not k or not isinstance(v, str) for k,v in EXCLUDED_AGENTS.items()):
        raise ValueError("invalid excluded_agents")
    if not isinstance(_skip_fragments, list) or any(not isinstance(v, str) or not v for v in _skip_fragments):
        raise ValueError("invalid skip_path_fragments")
    for _key in ("privacy_topic_pattern", "privacy_person_pattern", "privacy_path_marker", "restricted_recovery_root", "legacy_protected_root"):
        if not isinstance(SENSITIVE_POLICY[_key], str) or not SENSITIVE_POLICY[_key]:
            raise ValueError("invalid " + _key)
    for _key in ("restricted_recovery_root", "legacy_protected_root"):
        if not Path(SENSITIVE_POLICY[_key]).is_absolute():
            raise ValueError("policy paths must be absolute")
except (OSError, ValueError, KeyError, TypeError) as exc:
    raise RuntimeError("Invalid or unreadable sensitive policy") from exc

# Skip path components (matched anywhere in the relative path)
SKIP_DIR_NAMES = {
    "99_ARCHIVE", "node_modules", ".next", ".git", "__pycache__",
    ".venv", "venv", ".env", "site-packages", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", ".turbo",
    ".cache", ".DS_Store", "ARCTURION_EXTERNAL_RUNTIME",
    "12_LOGS", ".obsidian", ".trash",
    # Vendored dependency trees (e.g. a runbook's bundled scipy/numpy under `.deps/`)
    # were polluting the index with thousands of third-party code nodes. 2026-06-19.
    ".deps",
    # Restructure Phase 3 — renamed emoji-domain equivalents of the above skips.
    "99 🗄️ ARCHIVE", "12 📜 LOGS",
}

# Deployment-specific fragments are shared by walkers and reconciliation.
SKIP_PATH_FRAGMENTS = set(_skip_fragments)

# Archive/backup path markers — the SINGLE SOURCE OF TRUTH shared by the walker
# (util.should_skip_path, matched case-INSENSITIVELY so backups never index) and the
# resolver (resolver.is_archive_path, case-sensitive title-collision tie-break, which
# re-exports this list). Two lists drifted before: the 2026-06 reorg created mixed-case
# folders ("99 🗄️ Archive", "Reorg_Backups", "_memory-pre-restructure_*") that the
# case-sensitive SKIP_DIR_NAMES exact-match missed — so backup copies indexed as
# status=active and surfaced as top-1 answers (Phase 8 eval regression). Keeping one
# list here keeps walker and resolver in agreement.
ARCHIVE_MARKERS = (
    "99_ARCHIVE", "0699_ARCHIVE", "99 🗄", "0699 🗄", "staging_snapshots",
    "/before/", "/after/", "Reorg_Backups", "retired_staging", ".bak",
    "Historical_Contracts", "Legacy_", "_staging",
    "_memory-pre-restructure", "_pre-restructure",
)

# Classify historical authority without triggering lossy nightly skiplist GC.
HISTORICAL_SOURCE_MARKERS = ("session_backups", "backups")

# Exclude the engine's own test and evaluation fixtures from its index.
BRAIN_FIXTURE_RELS = (
    "engine/tests/",          # every unit test + the golden-set data file under fixtures/
    "engine/eval.py",         # the retired self-answering scorecard
    "engine/retrieval_eval.py",  # its honest replacement — same trap, so same rule
)


def is_brain_fixture_path(path) -> bool:
    """True if `path` is one of the Brain's own eval/test fixtures (see BRAIN_FIXTURE_RELS).

    Accepts a brain-relative path (what walker.walk_brain has) or a stored universe-relative
    path (what nodes.path holds, prefixed "ARCTURION_Brain/"). Leading "./" and "/" are
    tolerated so callers need not normalize."""
    s = str(path).replace("\\", "/").lstrip("/")
    if s.startswith("./"):
        s = s[2:]
    if s.startswith("ARCTURION_Brain/"):
        s = s[len("ARCTURION_Brain/"):]
    for frag in BRAIN_FIXTURE_RELS:
        if frag.endswith("/"):
            # Directory fragment: the trailing slash IS the boundary, so a prefix test is
            # already safe — "engine/tests/" cannot match "engine/tests_helpers/x.py".
            # The bare directory itself still counts.
            if s == frag.rstrip("/") or s.startswith(frag):
                return True
        elif s == frag or s.startswith(frag + "/"):
            # File fragment: a bare prefix test matched NEIGHBOURS, not just the file —
            # "engine/eval.py" also matched "engine/eval.py2", "engine/eval.pyc" and
            # "engine/eval.py.bak". Wrong in both directions: it silently de-indexed files
            # nobody asked to prune, and any real file named that way would be invisible
            # with no error. Exact match, or a "/" boundary for the pathological case of a
            # directory sharing the name.
            return True
    return False

# File extensions to index, mapped to (family, default_node_type)
EXTENSION_MAP = {
    ".md":   ("document", "Source"),
    ".markdown": ("document", "Source"),
    ".py":   ("code", "Module"),
    ".sh":   ("code", "Script"),
    ".bash": ("code", "Script"),
    ".zsh":  ("code", "Script"),
    ".js":   ("code", "Module"),
    ".ts":   ("code", "Module"),
    ".tsx":  ("code", "Module"),
    ".jsx":  ("code", "Module"),
    ".json": ("code", "Config"),
    ".yaml": ("code", "Config"),
    ".yml":  ("code", "Config"),
    ".toml": ("code", "Config"),
    ".plist": ("automation", "Automation"),
    ".ini":  ("code", "Config"),
    ".env.tpl": ("code", "Config"),
}

# Embedding config
EMBED_MODEL = os.environ.get("ARCTURION_BRAIN_LOCAL_MODEL", "")
EMBED_DIM = 384
EMBED_BATCH_SIZE = 32
SEMANTIC_SIM_THRESHOLD = 0.78
SEMANTIC_TOP_K = 5
MAX_BODY_CHARS = 8000        # truncate long files before embedding
# Keep the full-text body limit separate from the embedding limit.
FTS_BODY_CHARS = 4000
EMBED_SAMPLE_CHARS = 2000    # title+summary+head used for embedding

# Retrieval v2 can be disabled to use the earlier ranking path.
def _envflag(name: str, default: str = "1", *aliases: str) -> bool:
    for key in (name, *aliases):
        v = os.environ.get(key)
        if v is not None:
            return v.strip().lower() not in ("0", "false", "no", "off")
    return default.strip().lower() not in ("0", "false", "no", "off")


RETRIEVAL_V2 = _envflag("ARCTURION_BRAIN_RETRIEVAL_V2", "1", "RETRIEVAL_V2")

# -- 1. Vector seeding (engine/vector_seed.py) --
# Independently switchable so the vector channel can be isolated from the rest of v2 when
# bisecting a ranking change.
VECTOR_SEED_ENABLED = _envflag("ARCTURION_BRAIN_VECTOR_SEED", "0")
# Ids the vector channel contributes to rank fusion. This is a RANKED LIST, not a set of free
# passes: a vector hit still has to out-rank the other channels' hits to reach the pack.
VECTOR_SEED_TOP_K = int(os.environ.get("ARCTURION_BRAIN_VECTOR_SEED_TOP_K", "40"))
# Cosine floor for candidates in the vector seed list.
VECTOR_SEED_MIN_SIM = float(os.environ.get("ARCTURION_BRAIN_VECTOR_SEED_MIN_SIM", "0.35"))
# Fetch additional candidates before deduplication.
VECTOR_SEED_FETCH_K = int(os.environ.get("ARCTURION_BRAIN_VECTOR_SEED_FETCH_K", "400"))
# Seconds an in-process embedding matrix may be reused before refresh.
VECTOR_SEED_CACHE_TTL = float(os.environ.get("ARCTURION_BRAIN_VECTOR_CACHE_TTL", "300"))
# Path fragments never worth a vector seed slot. Raw session transcripts are verbatim echoes
# of the Founder's own prompts, so they embed close to ANY conversational question — their
# cosine measures how the question was PHRASED, not what the node knows. This is the
# vector-scoped twin of the raw-transcript penalty; AUTHORED distillations under
# Sessions/distilled/ are deliberately NOT excluded, matching that penalty's carve-out.
#
# As of Leg 4 (2026-08-07) this is an ADDITIVE list, not the rule. Transcripts are
# identified structurally by `util.is_raw_transcript_node` (node type / `.jsonl` backing),
# because a location list went stale twice and left 3,026 of the graph's 7,823 raw
# transcript nodes uncovered. Keep this for path fragments that are NOT transcripts but
# should still be denied a vector seed; do not re-add transcript shelves here.
VECTOR_SEED_EXCLUDE_PATHS = ("/Sessions/raw/", "logs/sessions")
# Weight of the raw cosine in the final score, for EVERY candidate rather than only the
# vector-seeded ones. Crediting only the nodes the lexical channels missed is asymmetric: on
# an exact-name query the lexical hits are themselves highly similar, so a seed-only bonus
# hands free points to semantically-adjacent nodes while the canonical answer — equally
# similar, merely found by the other channel — gets none.
VECTOR_SIM_WEIGHT = float(os.environ.get("ARCTURION_BRAIN_VECTOR_SIM_WEIGHT", "40.0"))

# -- 2. Reciprocal-rank fusion (engine/rank_signals.rrf) --
# The standard RRF constant. Larger k flattens the head (consensus matters more); smaller k
# lets a single channel's #1 dominate.
RRF_K = int(os.environ.get("ARCTURION_BRAIN_RRF_K", "60"))
# Per-channel weights. `fts` carries bm25's IDF — the signal that was computed and thrown
# away — so it is the heaviest. `exact_id` and `alias` are precise but narrow (they fire on
# one document); `title` is a whole-phrase substring, strong when it fires.
RRF_WEIGHTS = {
    "fts": 1.0,
    "vector": 0.9,
    "title": 0.8,
    "alias": 0.7,
    "exact_id": 1.2,
}

# -- 2d. Durable-source authority -------------------------------------------------
#
# A context pack is an answer surface, not a search-results popularity contest. The live
# graph contains many short-lived echoes of durable knowledge: session distillations,
# cached research payloads, and operational transcripts often repeat a Founder's wording
# more literally than the authored Decision, Preference, Policy, or Knowledge note that
# actually governs the answer. Fusion therefore gives the echo more relevance points even
# though the durable note has the stronger claim to authority.
#
# This is a small QUERY-TIME authority lift, not a filter and not a corpus mutation. It is
# deliberately below the existing active/canonical status boost, cannot override the
# exact-token or temporal-validity tiers, and is reversible with one environment flag. The
# markers name stable semantic shelves rather than fixture IDs or question vocabulary, so
# the rule applies to unseen queries without learning anything from the held-out set.
DURABLE_SOURCE_AUTHORITY = _envflag(
    "ARCTURION_BRAIN_DURABLE_SOURCE_AUTHORITY", "1" if RETRIEVAL_V2 else "0")
DURABLE_SOURCE_BOOST = float(
    os.environ.get("ARCTURION_BRAIN_DURABLE_SOURCE_BOOST", "4.0"))
RECENCY_DURABLE_BOOST = float(
    os.environ.get("ARCTURION_BRAIN_RECENCY_DURABLE_BOOST", "12.0"))
DURABLE_SOURCE_PATH_MARKERS = (
    "/knowledge/",
    "/memory/decisions/",
    "/memory/preferences/",
    "/nodes/policies/",
    "/nodes/constraints/",
)

# Preserve a small number of authoritative candidates through graph expansion. These are
# selected by general source class plus independent retrieval channels, never fixture ids.
DURABLE_HEAD_RESERVE = _envflag(
    "ARCTURION_BRAIN_DURABLE_HEAD_RESERVE", "1" if RETRIEVAL_V2 else "0")
DURABLE_TITLE_RESERVE = int(os.environ.get("ARCTURION_BRAIN_DURABLE_TITLE_RESERVE", "1"))
DURABLE_VECTOR_RESERVE = int(os.environ.get("ARCTURION_BRAIN_DURABLE_VECTOR_RESERVE", "0"))
# RRF scores live in ~[0, 0.077]; the rest of the scorer lives in ~[0, 100]. This maps one
# into the other. At 900 a document ranked #1 by every channel scores ~68 — comparable to a
# title-exact match, and below an exact node-id hit. A document ranked #1 by ONE channel
# scores ~15, which is roughly what three query terms in a title were already worth.
RRF_SCALE = float(os.environ.get("ARCTURION_BRAIN_RRF_SCALE", "900.0"))

# -- 2c. FTS OR-seed rarity filter (Leg 2 follow-up, retrieval-honesty 2026-08-06) --
#
# `_fts_or_seed` builds one `OR` MATCH across every content term, including stopword-grade
# terms `_terms()` didn't catch (common domain words like "agent" or "brain" that are not
# stopwords but sit in tens of thousands of documents). In an OR query those terms don't
# narrow anything — they inflate the bm25 candidate window with matches on the common word
# alone, crowding out documents that hit the query's actually-distinguishing terms.
#
# A SEPARATE flag from `IDF_LEXICAL_WEIGHTING` on purpose: one reshapes what SEEDS the walk,
# the other reshapes how already-seeded nodes are SCORED, and the golden gate needs to be
# able to isolate which one moved a class before they are shipped together. Both default ON
# under RETRIEVAL_V2 for the same reason as the scorer above — this is a quality fix to an
# existing v2 channel, not an independent experiment.
#
# GUARANTEED FALLBACK: if every term in a query prices below the threshold (e.g. a query that
# is entirely common words, or `term_stats` degraded to empty — the v1 path, where `stats` is
# always `{}`), filtering is skipped and the FULL unfiltered term list seeds the OR query,
# exactly as before this flag existed. A seed channel must never go from "returns something
# common" to "returns nothing" because of a ranking refinement.
FTS_SEED_IDF_FILTER = _envflag(
    "ARCTURION_BRAIN_FTS_SEED_IDF_FILTER", "1" if RETRIEVAL_V2 else "0")
# Terms with idf() below this are dropped from the OR-seed (subject to the fallback above).
# 0.5 sits below RARE_TOKEN's band by design — this filter only needs to catch the truly
# common, near-stopword tokens crowding the bm25 window, not to identify RARE ones; it must
# never be raised to where it starts dropping ordinary content words, which would starve the
# FTS channel the same way the pre-Leg-2 implicit-AND used to.
FTS_SEED_MIN_IDF = float(os.environ.get("ARCTURION_BRAIN_FTS_SEED_MIN_IDF", "0.5"))
# How much of the old hand-written lexical score survives alongside fusion. Fusion only
# scores SEEDS (a node has no rank in a channel that never returned it), while the graph walk
# discovers nodes no channel ranked — so the lexical term stays, damped, as the only thing
# that can rank an expanded node at all.
LEXICAL_WEIGHT = float(os.environ.get("ARCTURION_BRAIN_LEXICAL_WEIGHT", "0.25"))

# -- 2b. IDF-weighted lexical scoring (Leg 2 follow-up, retrieval-honesty 2026-08-06) --
#
# `_lexical_score` (brain.py) is the ONLY signal that ranks a node the graph WALK
# discovered rather than a channel SEEDED — fusion has no rank for those, so this is all
# they get — and its own docstring admits every term weighs the same: a token in 40,000
# nodes counts identically to a token in three. That is the measured cause of common-token
# recall@5 collapsing (Lessons:
# common-token-queries-collapse-recall-no-idf-term-rarity-weighting-in-the-ranker).
#
# `_lexical_score_idf` is the fix: same phrase-hit and field-weight shape, but each
# per-term contribution is scaled by idf(t)/max_idf(query) — the rarest query term keeps
# its full weight (6.0 title / 2.0 summary / 0.5 body, identical to `_lexical_score`'s
# per-term constants), and a common term is scaled toward zero. Normalizing by the QUERY's
# own max IDF (not a corpus-wide constant) keeps `C.LEXICAL_WEIGHT`'s calibrated magnitude:
# a query that is entirely rare tokens scores the same as before; only a query mixing rare
# and common tokens redistributes weight away from the common ones.
#
# `term_stats` is computed ONCE per query in `_query_impl` and threaded into the scorer —
# this reuses it, at zero new DB queries per node. Defaults ON exactly when RETRIEVAL_V2 is
# on: this is a straight quality improvement to the v2 lexical term, not an independent
# experiment, so it inherits v2's own A/B switch rather than adding a second one someone
# has to remember to flip together. `RETRIEVAL_V2=0` never reaches this flag at all — the
# v1 path calls `_lexical_score`, never `_lexical_score_idf` — so the A/B restore holds
# regardless of this flag's value.
IDF_LEXICAL_WEIGHTING = _envflag(
    "ARCTURION_BRAIN_IDF_LEXICAL_WEIGHTING", "1" if RETRIEVAL_V2 else "0")

# -- 3. Exact-token primacy --
# Rare tokens can receive a stronger exact-match ranking signal.
RARE_TOKEN_MAX_DF = int(os.environ.get("ARCTURION_BRAIN_RARE_TOKEN_MAX_DF", "400"))
RARE_TOKEN_MIN_LEN = int(os.environ.get("ARCTURION_BRAIN_RARE_TOKEN_MIN_LEN", "4"))
# Query tokens to price. Bounds the per-query DF cost (~1-2 ms each, cached).
RARE_TOKEN_MAX_TERMS = int(os.environ.get("ARCTURION_BRAIN_RARE_TOKEN_MAX_TERMS", "12"))
# A TRUE pin: a literally-matched rare token puts the document in a HIGHER SORT TIER than
# every unpinned document, so no fusion or similarity score can outrank it. This is what the
# card asks for — "pins that hit to the top regardless of fusion score".
#
# It was a boost first, and the boost was NOT what the card said. At 45 points against
# RRF_SCALE x max_rrf (~69) plus VECTOR_SIM_WEIGHT (40), "regardless" was simply false: any
# additive term is a number competing with other numbers, so there is always a combination
# that beats it, and raising the number only moves where the failure is. A lexicographic tier
# is the only shape that means "regardless". See brain.rank_key_v2.
EXACT_TOKEN_TRUE_PIN = _envflag("ARCTURION_BRAIN_EXACT_TOKEN_TRUE_PIN", "1")
# The additive fallback, used only when the true pin is switched off. Kept switchable so the
# two can be compared on the golden gate rather than argued about.
EXACT_TOKEN_PIN = float(os.environ.get("ARCTURION_BRAIN_EXACT_TOKEN_PIN", "45.0"))
EXACT_TOKEN_PIN_MAX = float(os.environ.get("ARCTURION_BRAIN_EXACT_TOKEN_PIN_MAX", "180.0"))

# -- 3b. Temporal validity tier (Leg 4, retrieval-honesty 2026-08-06) --
#
# Same argument as EXACT_TOKEN_TRUE_PIN above, aimed the other direction: `_authority_score`'s
# existing `-12` if node.get("superseded_by") is an ADDITIVE penalty, and an additive penalty
# is just a number competing with other numbers — there is always a fusion+recency+similarity
# combination that outscores it, so a stale document can still win a recency question. "Stale
# facts lose" needs the same lexicographic-tier shape the exact-token pin uses: a node whose
# `valid_until` (engine.maintenance.validity_pass, stamped ONLY from an explicit supersession
# or resolved-contradiction record — never inferred) has passed sorts in a tier BELOW every
# live node, regardless of score. It is a TIEBREAK-of-last-resort, not a filter: an expired
# node is never removed from the candidate set, so it remains the answer when it is the ONLY
# node that has one (see rec_02-shaped questions where the "stale" side is still an accepted
# alternate). `rank_key_v2` becomes `(pin_tier, validity_tier, score)`.
#
# Defaults ON exactly when RETRIEVAL_V2 is: this is a ranking-quality fix to an existing v2
# tier mechanism, not an independent experiment, so it inherits v2's own A/B switch. When OFF
# (or when RETRIEVAL_V2=0, which never reaches rank_key_v2 at all — v1 calls the old `score`
# closure, byte-identical, unconditionally), `rank_key_v2` returns the old 2-tuple
# `(pin_tier, score)` exactly as before this leg, and the pre-existing `-12` additive
# `superseded_by` penalty path in `_authority_score` is untouched either way.
TEMPORAL_VALIDITY_TIER = _envflag(
    "ARCTURION_BRAIN_TEMPORAL_VALIDITY_TIER", "1" if RETRIEVAL_V2 else "0")

# -- 3c. Contextual embedding prefix + query alias expansion (Leg 3, retrieval-honesty
#    2026-08-06) --
#
# Legs 2 and 4 converged on a stage the ranker never gets a chance to fix: SEEDING. Common-
# token recall@5 (0.182) and paraphrase recall@5 (~0.273) both fail because the expected node
# never enters the fused candidate pool in the first place — no amount of downstream rank
# fusion, IDF weighting, or temporal tiering can recover a document no channel ever returned.
#
# Two independent, cheap (no LLM, no new deps) fixes to the SEED side:
#
#   1. CONTEXTUAL_EMBED_PREFIX — Anthropic contextual-retrieval's cheap half. A deterministic
#      header (title, shelf/prong, owner agent, doc kind) is prepended to the text that gets
#      EMBEDDED (engine.embedder._text_for_embedding), never to the stored body and never to
#      the FTS-indexed text. This gives the vector channel context a bare title/summary/body
#      excerpt doesn't have — e.g. distinguishing a "Stop hook feedback" session-log twin from
#      a "Stop Gate Implementation Pattern" knowledge doc, which is exactly the common-token
#      collision ct-01's note describes. Flag OFF restores the pre-Leg-3 embedding input
#      byte-for-byte (see test_contextual_prefix.py).
#   2. QUERY_ALIAS_EXPANSION — Founder-phrased queries deliberately avoid corpus jargon (every
#      paraphrase fixture's note names the words it withholds). `engine.aliases.
#      expand_query_terms` matches a REGISTERED alias phrase (canon/ENTITY_ALIASES.md) against
#      the raw topic string and, on a hit, adds that entry's other alias words to the term list
#      `_seed_channels` feeds the FTS OR-seed and title-substring channels — never to
#      `RS.term_stats`/the exact-token pin, which must stay tied to what the Founder actually
#      typed.
#
# MEASURED, NOT ASSUMED, 2026-08-06 — the two mechanisms do NOT get the same default:
#
#   QUERY_ALIAS_EXPANSION defaults ON exactly when RETRIEVAL_V2 is (a seeding-quality fix to
#   the existing v2 pipeline, inheriting v2's own A/B switch). Traced end to end: it DOES put
#   previously-unseeded paraphrase targets into the fused candidate pool (verified directly —
#   `mem_thermal_and_rate_gates_d6117f` was absent from `_seed_channels`'s `fts` channel with
#   the flag off, present at bm25 rank 18/480 with it on), and a same-session paired gate run
#   (flag on vs off, embeddings and DB otherwise held constant) showed the SAME pass/fail set
#   on every class — no measured regression.
#
#   CONTEXTUAL_EMBED_PREFIX defaults OFF UNCONDITIONALLY, overriding the RETRIEVAL_V2-inherits
#   pattern every other v2 flag in this file uses. A full re-embed of the live 58,065-node
#   graph with the header on (118 s) moved paraphrase/common-token recall@5 by ZERO on the
#   golden gate — seeding-pool entry alone does not fix these classes; the fused pool already
#   contained most of the missed targets even with the header off, and the classes fail
#   further down the pipeline (final ranking, not seeding) — while measurably REGRESSING two
#   classes the header was never meant to touch: recency recall@5 0.545 -> 0.455 (a duplicate
#   'twin' node with a different id-churn suffix started outranking the golden-pinned copy
#   once every node's embedding shifted) and exact-token recall@5 0.909 -> 0.818 for the same
#   id-churn-twin reason on a different fixture. Both regressions exceed this leg's own 0.05
#   tolerance. Per the Leg-2 precedent ("if a target is not reachable, instrument, trace, and
#   report the mechanism honestly rather than force it"): the mechanism, tests, and re-embed
#   tooling all ship — `_context_header`/`_text_for_embedding` are exercised and pinned by
#   `test_contextual_prefix.py`, and `embedder.embed_all(force=True)` / `embed --force` exist
#   for whoever revisits this — but it does not go live by default until a future pass either
#   lightens the header (this leg's locked design is title+shelf+owner+kind; a lighter
#   composition was NOT tried) or pairs it with an id-churn dedup fix so a header-driven
#   embedding shift cannot resurface a duplicate over its golden-pinned twin.
#
#   RE-TESTED 2026-08-07 (retrieval-precision Leg 2, post-Leg-1-dedup): the id-churn dedup
#   fix landed (commit 7b1db5b — duplicate rate 10.22% -> 0.0%) and this leg re-ran the exact
#   same experiment (`embed --force` with the header on, full 59,600-node re-embed, 124 s) on
#   the clean corpus. Dedup did NOT unlock the flag. paraphrase and common-token pass rates
#   were UNCHANGED (0.364 and 0.182, byte-identical to the header-off baseline — zero targets
#   moved), and recency STILL regressed past tolerance (pass 0.545 -> 0.455, i.e. -0.09 against
#   a 0.05 budget), this time without an id-churn twin to blame (dedup removed those) — direct
#   inspection of para-01 and ct-01's top-1 after the re-embed shows session-log / meta nodes
#   with high raw lexical overlap on the query's own words (a node titled literally
#   "paraphrased_questions_return_nothing_and_hook_noise_session_files_crowd_the_pack" for
#   para-01; a session log opening "when I'm working in session with an agent I hat[e]..." for
#   ct-01, both out-scoring the correct doc) still outrank the golden node, and the golden node
#   does not even reach the top 8. This confirms the mechanism this comment already named:
#   these two classes fail in FINAL RANKING, not candidate-pool seeding — the header changes
#   what gets embedded, not how competing high-overlap nodes get scored against it, so it was
#   never going to touch this failure mode. Flag stays OFF; embeddings were restored to the
#   un-prefixed form and the golden gate re-confirmed byte-identical to pre-leg (pass 0.500,
#   guards 0.462, all six per-class pass rates matched). The next lever to try is downstream of
#   seeding (rank fusion / lexical-overlap discounting for session-log-shaped nodes), not the
#   embedding input.
CONTEXTUAL_EMBED_PREFIX = _envflag("ARCTURION_BRAIN_CONTEXTUAL_EMBED_PREFIX", "0")
QUERY_ALIAS_EXPANSION = _envflag(
    "ARCTURION_BRAIN_QUERY_ALIAS_EXPANSION", "1" if RETRIEVAL_V2 else "0")
# Cap on NEW terms one query-side alias expansion pass may add. `_terms()` itself caps at 12;
# this bounds how much a single alias hit can grow that list before the OR-seed's cost scales
# with it. Calibrated generously loose — every alias entry authored for this leg is 3-8 words.
QUERY_ALIAS_EXPANSION_MAX_TERMS = int(
    os.environ.get("ARCTURION_BRAIN_QUERY_ALIAS_EXPANSION_MAX_TERMS", "8"))

# -- 4. Real age decay --
#
# THE BOUND THAT MATTERS: `AGE_DECAY_WEIGHT` is the ENTIRE contribution of recency — the age
# term is `WEIGHT x 2^(-age/half-life)`, so it lives in [0, WEIGHT]. Recency can therefore
# never move a node by more than WEIGHT points, and keeping WEIGHT below the status signal is
# what makes "a TIEBREAK, never a filter" true in the code rather than only in this comment.
#
# Against brain._STATUS_BOOST / _STATUS_PENALTY (canonical +10, active/verified +8,
# draft -6, deprecated -14, retired -16, superseded/archived -18, archive path -20):
#   * max age contribution 8.0 < canonical +10  →  an infinitely old canonical document still
#     outranks a brand-new one on status alone.
#   * 8.0 is less than half of every stale-status penalty  →  nothing superseded, archived,
#     deprecated or retired can be freshened back into the pack by being new.
#   Worked example, the one that failed review: a fresh draft scores -6 + 8.0 = +2.0; a
#   two-year-old canonical scores +10 + ~0.002 = +10.0. Status wins by 8 points.
#
# This was 16.0 and the comment above it claimed status dominated "by an order of magnitude".
# It did not: 16.0 EXCEEDED the canonical boost of 10, so a fresh draft levelled with an old
# canonical and recency could outrank status outright. Corrected 2026-08-04 in fix round 1 by
# re-measuring, not by nudging — see the half-life note below for how the discrimination was
# preserved while the bound was cut in half.
AGE_DECAY_WEIGHT = float(os.environ.get("ARCTURION_BRAIN_AGE_DECAY_WEIGHT", "8.0"))
# Half-life in days. This governs DISCRIMINATION (how sharply two dates separate); the weight
# above governs AUTHORITY (how much recency can ever be worth). They are independent, which is
# what let the weight drop from 16 to 8 with no loss: halving the half-life from 180 d to 60 d
# doubles the separation between two documents a few months apart, recovering exactly what the
# smaller weight gave up.
#
# 60 d is measured, not borrowed. 180 d is the general-purpose IR default; THIS corpus reverses
# itself far faster — the golden set's own recency pairs sit 28 days apart (the June "GnuCash is
# canonical" ADR vs the July "GnuCash is dead" migration). Swept across {45, 60, 90, 120, 180}
# at weight 8: 45 d and 60 d both hold recency top1 at 0.250 and pooled top1 at 0.211; 90 d and
# above lose rec-02's top-1 to an older generic note. 60 d is the looser of the two that work.
AGE_HALFLIFE_DAYS = float(os.environ.get("ARCTURION_BRAIN_AGE_HALFLIFE_DAYS", "60"))

# -- 5. Relevance floor --
# Below this absolute confidence the pack comes back EMPTY with a low-confidence warning
# instead of confident noise. Confidence is max(best cosine, IDF-weighted lexical coverage)
# over the head of the ranked pack — see rank_signals.pack_confidence.
#
# MEASURED 2026-08-04 across all 26 golden questions, both components dumped separately
# (numbers reproduced in the Leg 2 report):
#
#            | cosine (head)  | IDF coverage   | max(cos, cov)
#   positive | 0.392 - 0.718  | 0.368 - 1.000  | 0.473 - 1.000   (n=19)
#   negative | 0.000 - 0.452  | 0.000 - 0.532  | 0.000 - 0.532   (n=5)
#
# NEITHER component separates the classes on its own, and neither does their max — the
# distributions overlap. So this floor is chosen for MARGIN, not for maximum separation.
# 0.42 sits 0.053 below the weakest true positive (para-04, 0.473) and above three of the five
# negative controls. Raising it to 0.47 catches no additional negative; raising it to 0.48
# catches one more (neg-04, 0.477) at the cost of flooring a true positive — buying a guard by
# losing recall, which is the wrong trade.
#
# The two negatives that survive are an HONEST LIMIT, not a tuning failure: neg-03 ("goats")
# and neg-04 ("amsterdam") each contain a genuinely rare token that genuinely occurs somewhere
# in the corpus, so both signals legitimately fire. Separating those needs an answerability
# judgement this leg does not have — not a different threshold.
#
# Because this is calibrated on the set that grades it, drift in EITHER direction is a signal
# to re-measure, never to nudge the number.
RELEVANCE_FLOOR = float(os.environ.get("ARCTURION_BRAIN_RELEVANCE_FLOOR", "0.42"))
# How deep into the ranked pack the floor looks for one confident node.
RELEVANCE_FLOOR_DEPTH = int(os.environ.get("ARCTURION_BRAIN_RELEVANCE_FLOOR_DEPTH", "5"))
# A literal rare-token match does not bypass the relevance floor by default.
# A token being present is not evidence that the corpus answers the question.
# Keep the optional switch available for explicitly configured evaluations.
RELEVANCE_FLOOR_RARE_TOKEN_BYPASS = _envflag(
    "ARCTURION_BRAIN_FLOOR_RARE_TOKEN_BYPASS", "0")

# -- 5b. Two-floor confidence (Leg 5, retrieval-honesty 2026-08-06) --
# `RELEVANCE_FLOOR` is ONE number serving two incommensurable scales: a raw MiniLM cosine
# and an IDF-weighted lexical-coverage fraction, combined with `max()`. Measured 2026-08-06:
# a well-formed conversational question clears 0.42 on cosine ALONE regardless of whether the
# corpus answers it, so guard questions about things absent from the corpus (e.g. "how many
# goats does the founder own") still returned confident packs. Golden guard pass rate at that
# baseline: 0.462 (11 negatives + 2 contamination).
#
# TWO_FLOOR_CONFIDENCE splits the single floor into two independently-sufficient floors plus a
# narrow corroboration band between them, and is the ONLY thing `rank_signals.pack_passes_floor`
# reads when it is on:
#   - `lexical_coverage >= LEXICAL_FLOOR`  — sufficient ALONE. A pasted error string or exact
#     flag name legitimately has mediocre cosine; this is `RELEVANCE_FLOOR`'s old value,
#     UNCHANGED, because it was already correctly calibrated for that shape of evidence.
#   - `sim >= SEMANTIC_FLOOR` — sufficient ALONE. A true paraphrase legitimately has near-zero
#     lexical evidence (different words, same meaning); this is the floor that did not exist
#     before — the old code let ANY sim >= 0.42 through unconditionally, which is exactly what
#     let "how many goats does the founder own" (sim 0.452 against the Founder-identity notes)
#     read as confident.
#   - CORROBORATION BAND `[CORROBORATION_SIM, SEMANTIC_FLOOR)`: a sim too weak to trust alone
#     passes ONLY when there is real, non-trivial lexical corroboration
#     (`lexical_coverage >= CORROBORATION_LEX_MIN`). This is what lets a genuine paraphrase
#     with middling sim and modest-but-real lexical overlap (para-04, sim 0.473 / lex 0.364)
#     through, while refusing a negative whose only lexical overlap is a couple of common
#     words riding along with an unrelated topic (neg-03 "goats", sim 0.452 / lex 0.093).
#
# `RELEVANCE_FLOOR` keeps acting as an outer sanity ceiling even in two-floor mode
# (`max(sim, lex) >= RELEVANCE_FLOOR` gates before the two-floor decision runs at all) — this is
# what keeps `test_retrieval_v2`'s existing floor mechanics (RELEVANCE_FLOOR=0.9 must empty
# every pack, RELEVANCE_FLOOR=0.0 must never floor) working unmodified. In production
# RELEVANCE_FLOOR (0.42) is the lowest of the four numbers here, so the ceiling is inert — it
# only bites when a caller deliberately raises or lowers RELEVANCE_FLOOR, exactly as before.
#
# TUNED 2026-08-06 against the 11 golden negatives + 2 contamination + all currently-passing
# positive classes (para-03/04/08, ct-03/09, all exact-token, all passing recency, both
# contamination) — see the Leg 5 report for the full trade curve. SEMANTIC_FLOOR=0.60 and
# CORROBORATION_SIM=0.45/CORROBORATION_LEX_MIN=0.30 are the widest (most conservative) point
# that both fixes neg-03 and does not touch any currently-passing positive; several negatives
# (neg-04 "amsterdam", neg-07 "evacuation", neg-08 "whale", neg-09 "board meeting", neg-10
# "dress code", neg-11 "garage") remain HONEST LIMITS of this design, not a tuning miss: each
# already clears `LEXICAL_FLOOR` on its own — sufficient alone, per this leg's own contract —
# via either a literal rare-token hit in the wrong context (evacuation, garage) or accumulated
# common-word coverage across several mid-frequency terms (board/meeting, dress/code). Fixing
# those needs an answerability judgement neither signal here has, not a different number.
TWO_FLOOR_CONFIDENCE = _envflag(
    "ARCTURION_BRAIN_TWO_FLOOR_CONFIDENCE", "1" if RETRIEVAL_V2 else "0")
LEXICAL_FLOOR = float(os.environ.get("ARCTURION_BRAIN_LEXICAL_FLOOR", "0.42"))
SEMANTIC_FLOOR = float(os.environ.get("ARCTURION_BRAIN_SEMANTIC_FLOOR", "0.60"))
CORROBORATION_SIM = float(os.environ.get("ARCTURION_BRAIN_CORROBORATION_SIM", "0.45"))
CORROBORATION_LEX_MIN = float(
    os.environ.get("ARCTURION_BRAIN_CORROBORATION_LEX_MIN", "0.30"))

# -- 5c. Guard floor refinement: what makes lexical evidence SUFFICIENT ALONE
#    (Leg 3 / refinement 5b, retrieval-precision 2026-08-07) --
#
# Leg 5 (above) closed the semantic side and left a documented residual on the lexical side:
# six of the eleven golden negatives still returned confident packs, every one of them by
# clearing `LEXICAL_FLOOR` — "sufficient ALONE, per this leg's own contract". Leg 5 called
# those an honest limit needing "an answerability judgement neither signal here has". Measured
# 2026-08-07, that is half right: two DIFFERENT mechanisms are hiding under one number, and
# both are answerable from signals already computed per query.
#
# MECHANISM 1 — coverage is a FRACTION OF TOTAL IDF MASS, so a document can clear it while
# matching NONE of the terms that make the question specific. `lexical_coverage` is already
# IDF-weighted, but it is normalized by the query's WHOLE mass, and the low-IDF remainder of a
# short question is a large enough share of that mass to clear 0.42 on its own:
#
#   neg-03 "how many goats does the founder own" — matched {many 4.12, founder 1.58, own 2.38}
#          = 0.472 coverage, while `goats` (idf 9.05, 53% of the query's entire mass — the one
#          word that makes it a question) is absent from the document.
#   Same shape: neg-04 (matched wifi+password, missed `amsterdam`), neg-08 (missed `watching`,
#          `trip`), neg-10 (missed `employee`), neg-11 (missed `validation`, `policy`).
#
# `FLOOR_RARE_COVERAGE_MIN` closes it by asking a second, scale-free question of the SAME IDF
# numbers: of the query's RARE mass specifically — terms at or above `FLOOR_RARE_TERM_MIN_IDF` —
# how much did this document actually account for? Lexical evidence is sufficient alone only
# when that fraction is essentially all of it. The rationale is a property of answerhood, not
# of these fixtures: a document that ANSWERS a question contains the question's distinguishing
# vocabulary, and a document that merely COLLIDES with it contains a subset. Measured on the
# golden set that is not a trend but a separation — every currently-passing positive that
# relies on the lexical-alone branch (ct-03, ex-06, ex-08, ex-10, rec-03, rec-11) sits at rare
# coverage 1.000, while the negatives clearing it sit at 0.31 / 0.56 / 0.57 / 0.71 / 0.78.
# 0.90 is the midpoint of that gap; the whole band [0.79, 1.00] scores identically.
#
# MECHANISM 2 — a document can contain ALL of the query's rare vocabulary and still be about
# something else entirely. Rare-mass coverage cannot see this and neither could Leg 5:
#
#   neg-07 "where do we keep the fire evacuation route for the office" — a 2026 governance
#          restructure document matches `evacuation`, `route`, `office`, `keep` (coverage
#          0.899, rare coverage 1.000) at cosine 0.090. It is not a near-miss answer; the words
#          co-occur in a document about an entirely unrelated subject.
#
# `FLOOR_LEX_ALONE_MIN_SIM` is the symmetric counterpart of Leg 5's corroboration band. Leg 5
# required lexical corroboration before trusting a WEAK semantic; this requires a minimum
# semantic sanity check before trusting lexical evidence ALONE. It is deliberately far below
# `CORROBORATION_SIM` (0.45) — this is not "the document must be about the question", which
# would destroy the pasted-error-string class whose mediocre cosine is the entire reason the
# lexical-alone branch exists. It is only "the document is not semantically unrelated". The
# lowest cosine among currently-passing positives that need the lexical-alone branch is ex-10
# at 0.316; the highest among negatives it refuses is neg-07 at 0.096. 0.20 is the midpoint;
# the whole band [0.10, 0.30] scores identically, and 0.32 is where ex-10 starts to break.
#
# BOTH GATES ONLY EVER TIGHTEN THE `lex >= LEXICAL_FLOOR` BRANCH. The semantic floor and the
# corroboration band are untouched, and neither gate can make a pack pass that failed before —
# so this can only convert a confident wrong answer into an honest refusal, never the reverse.
#
# TWO FLAGS, not one, for the same reason `FTS_SEED_IDF_FILTER` is separate from
# `IDF_LEXICAL_WEIGHTING`: they are independent mechanisms closing disjoint negative sets
# (rare-mass alone fixes neg-03/04/08/10/11; sanity alone fixes neg-07/08/10), and the gate
# must be able to attribute a movement to one of them before they ship together.
#
# DEGRADED-SIGNAL SAFETY (the reason the sanity gate is per-node rather than a bare compare):
# `vector_seed.similarity` returns None outright when the encoder is still loading, and returns
# 0.0 for any node that has no embedding. Reading either as "semantically unrelated" would turn
# a missing signal into a blanket refusal of every query — the exact inversion of this module's
# contract. The gate therefore applies only to a node with a REAL cosine; see
# `rank_signals.pack_passes_floor`.
LEXICAL_FLOOR_RARE_MASS = _envflag(
    "ARCTURION_BRAIN_LEXICAL_FLOOR_RARE_MASS", "1" if RETRIEVAL_V2 else "0")
# What counts as a "rare" (distinguishing) query term for the floor's purposes. Sits well above
# FTS_SEED_MIN_IDF (0.5, the near-stopword line) and below the RARE_TOKEN pin band
# (idf(df=400) ~ 4.93): idf 3.0 is a term in fewer than ~2,800 of 55K documents, ~5% of the
# corpus. NOTE the golden set cannot discriminate this number — every value in [0.0, 3.4]
# scores 12/13 — so it is chosen on that structural reading rather than on the score, and
# deliberately NOT at the plateau edge. At 0.0 the rule silently degenerates into "plain
# coverage >= FLOOR_RARE_COVERAGE_MIN", a far blunter test that would score the same here and
# generalize much worse.
FLOOR_RARE_TERM_MIN_IDF = float(
    os.environ.get("ARCTURION_BRAIN_FLOOR_RARE_TERM_MIN_IDF", "3.0"))
FLOOR_RARE_COVERAGE_MIN = float(
    os.environ.get("ARCTURION_BRAIN_FLOOR_RARE_COVERAGE_MIN", "0.90"))
# GUARANTEED FALLBACK, same contract as FTS_SEED_IDF_FILTER's: when NO query term prices at or
# above FLOOR_RARE_TERM_MIN_IDF (a question made entirely of common words, or `stats` degraded
# to empty on the v1 path), there is no rare mass to measure coverage of, and the gate falls
# back to plain `lexical_coverage` exactly as before this flag existed. A floor must never go
# from "sometimes passes" to "never passes" because a refinement had nothing to measure.
LEXICAL_FLOOR_SIM_SANITY = _envflag(
    "ARCTURION_BRAIN_LEXICAL_FLOOR_SIM_SANITY", "1" if RETRIEVAL_V2 else "0")
FLOOR_LEX_ALONE_MIN_SIM = float(
    os.environ.get("ARCTURION_BRAIN_FLOOR_LEX_ALONE_MIN_SIM", "0.20"))

# The marker the empty pack carries. The RAG hook keys on this prefix to inject NOTHING, rather
# than falling over to /search and re-injecting the noise the floor just removed.
#
# ⚠️ MIRRORED OUTSIDE THIS REPO, across a boundary git cannot see:
#     ~/.claude/hooks/brain-rag-inject.py  →  LOW_CONFIDENCE_MARKER
# `~/.claude` is not a git repository, so nothing links these two strings and no test spans
# them. If they drift, the failure is NOT a quiet fall-back to /search — the refusal SENTENCE
# ITSELF stops being recognised as a sentinel and gets injected into every agent's context as
# though it were retrieved knowledge, which is the exact opposite of what the floor is for.
#
# Two cheap mitigations, both already in place: the hook matches the prefix WITHOUT the
# trailing colon, so punctuation and message-body edits here are safe; and the leading `[`
# keeps it inside the "bracketed control sentence" family the hook already screens (`[no `).
# Changing the PHRASE "low-confidence" is the one edit that breaks the contract — do that only
# together with the hook.
LOW_CONFIDENCE_MARKER = "[low-confidence:"

# Query API defaults
# 2000 (was 4000): brain_query is called every turn per the MCP mandate, so the
# returned pack compounds in-context across a session. Halving the default caps
# that per-turn cost; deep one-off lookups can still pass a larger budget.
DEFAULT_TOKEN_BUDGET = 2000
DEFAULT_RADIUS = 2
APPROX_CHARS_PER_TOKEN = 4

# --- /query latency tuning (Brain v2 perf, 2026-06-20) ---
# The query index is a ~3 GB WAL DB. SQLite's default ~2 MB page cache + a pread()
# per page meant every /query re-read cold pages from disk under memory pressure
# (≥10 long-lived Claude processes resident) — the 1.5-3 s tail. mmap maps the DB so
# the hot working set (indexes + frequently-touched edge/node pages) stays resident
# across the long-lived brain-http / MCP processes and reads become page-faults, not
# syscalls. cache_size is per-connection (connections are short-lived, one per request),
# so it stays modest and mmap does the cross-request caching at the OS level. Both are
# read-path only and change NO query results. Tunable via env for low-RAM machines.
#
# _int_env (2026-08-04 hardening): a bare int(os.environ.get(...)) crashes every
# consumer at IMPORT time if the operator sets a malformed value (e.g. a typo'd
# "3GB" instead of the byte count) — the whole engine becomes unimportable from a
# single bad env var. Falls back to the default and warns to stderr instead.
def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        print(f"[config] {name}={raw!r} is not a valid int; using default {default}",
              file=sys.stderr)
        return default


SQLITE_MMAP_BYTES = _int_env("ARCTURION_BRAIN_MMAP_BYTES", 3 * 1024**3)   # 3 GiB
SQLITE_CACHE_KIB = _int_env("ARCTURION_BRAIN_CACHE_KIB", 65536)           # 64 MiB/conn

# --- pack CONTENT (Leg 5) ----------------------------------------------------------
# Until Leg 5 a pack emitted title + id/path/owner + summary and nothing else: ~91 chars
# per node measured over the golden set. The body_excerpt was SCORED during retrieval and
# then thrown away, so a caller that asked a question got a list of POINTERS to documents
# it would have to go and read. Twenty pointers is not an answer; one substantive
# paragraph usually is.
#
# Graded, not uniform. Giving every node a body would spend the whole budget on the tail
# and the pack would stop being a graph view. The top PACK_BODY_WINNERS nodes get up to
# PACK_BODY_CHARS of body; everything below stays summary-only and the pack keeps its
# breadth.
#
# A winner NEVER costs a slot it could have had summary-only: if the body-bearing chunk
# does not fit the remaining budget, `_node_lines` is re-rendered WITHOUT the body and
# only THEN may the node be dropped. So this can shorten a pack's tail; it can never make
# a pack shorter in NODES than it was before. Without that fallback a single 600-char
# winner at a small budget returns an EMPTY pack where the old code returned four
# pointers — strictly worse.
PACK_BODY_WINNERS = _int_env("ARCTURION_BRAIN_PACK_BODY_WINNERS", 3)
PACK_BODY_CHARS = _int_env("ARCTURION_BRAIN_PACK_BODY_CHARS", 600)

# --- pack DIVERSITY (Leg 5) --------------------------------------------------------
# Exact-twin dedup (path / title / content-fingerprint, `brain._query_impl`) already
# collapses restatements of ONE document. It does nothing about near-SIBLINGS: a dozen
# distinct, genuinely different notes that all live in one folder can still take every
# slot, and when they do the pack answers from a single vantage point and calls it the
# Brain's knowledge.
#
# The cap is a DEMOTION, not a deletion: nodes beyond the cap for their source move to
# the END of the ranking rather than being dropped. The honest failure mode of a
# diversity rule is punishing a corpus for being well organised — demoting rather than
# dropping makes the worst case "the right answer is at rank 6 instead of 4" instead of
# "the right answer is gone". Set to 0 to disable.
PACK_PER_SOURCE_CAP = _int_env("ARCTURION_BRAIN_PACK_PER_SOURCE_CAP", 3)

# Applies to the configured graph and source inventory.
EXPAND_MAX_EDGES_PER_HOP = _int_env("ARCTURION_BRAIN_MAX_EDGES_PER_HOP", 12000)
EXPAND_MAX_REACHED = _int_env("ARCTURION_BRAIN_MAX_REACHED", 3000)

# Applies to the configured graph and source inventory.
MAX_FILES_PER_PASS = 400_000

# Hash + ID
ID_PREFIX = "mem"

# --- Four-Prong Canon (Commandment 019, ratified 2026-06-09) ---
# Runtime MAP allow-list: the only files the brain indexes from the External
# Runtime prong. The runtime's CONTENTS (venvs/weights/caches/build/service source)
# stay excluded — we index "what runs where", not the bytes that run.
RUNTIME_MAP_FILES = [
    "RUNTIME_BOUNDARY_MANIFEST.md",
    "README.md",
    "SERVICE_CATALOG.md",
]


def prong_for_path(path) -> str:
    """Return the prong a path belongs to: hq | atlas | constellation | runtime.

    Checked most-specific first (BRAIN/ATLAS/RUNTIME/HQ are all under DESKTOP_ROOT).
    The Brain (BRAIN_ROOT) is HQ-stewarded shared infra → tagged 'hq', keeping the
    prong set at 4 values (the brain is connective infra, not a 5th prong).
    'universe' was renamed to 'hq' 2026-06-16 (ARCTURION_UNIVERSE→ARCTURION_HQ).
    Used to tag every brain node with its prong (graph_builder._extract_one).

    Normalization is cwd-INDEPENDENT (2026-08-04 hardening): a relative input is
    resolved against DESKTOP_ROOT, never Path.resolve()'s implicit process cwd, so
    classification is identical whether the caller runs interactively (cwd is
    wherever the shell happens to be) or under launchd (cwd="/"). This also
    preserves the documented behavior for already-relative universe paths like
    "ARCTURION_EXTERNAL_RUNTIME/README.md" — the intended base was always
    DESKTOP_ROOT, cwd just happened to match it in the interactive case."""
    s = str(path)
    if not s.startswith("/"):
        s = str(DESKTOP_ROOT / s)
    s = _os.path.normpath(s)
    _runtime = _os.path.normpath(str(RUNTIME_ROOT))
    _atlas = _os.path.normpath(str(ATLAS_ROOT))
    _brain = _os.path.normpath(str(BRAIN_ROOT))
    _hq = _os.path.normpath(str(UNIVERSE_ROOT))
    _desktop = _os.path.normpath(str(DESKTOP_ROOT))
    _claude = _os.path.normpath(str(CLAUDE_ROOT))
    if s.startswith(_runtime):
        return "runtime"
    if s.startswith(_atlas):
        return "atlas"
    if s.startswith(_brain):
        return "hq"
    if s.startswith(_claude):
        return "hq"  # ~/.claude engineering plans + hooks — HQ-stewarded runtime infra
    if s.startswith(_hq):
        return "hq"
    if s.startswith(_desktop):
        return "constellation"
    return "unknown"


def prong_for_relpath(relpath) -> str:
    """Map a STORED (universe-relative) node path to its prong: hq|atlas|constellation|runtime.

    Nodes store paths relative to their source root, so the first path component
    identifies the prong: an agent name → constellation, ATLAS/ → atlas,
    ARCTURION_EXTERNAL_RUNTIME/ → runtime, ARCTURION_Brain/ → hq (brain infra),
    everything else (HQ shelves: 00_…/01_…/'00 🧭 DOCTRINE'/… ) → hq.
    Used by maintenance/prong_backfill.py to fill NULL prongs without reconstructing
    absolute paths (which are ambiguous post-relocation)."""
    s = str(relpath).lstrip("/")
    first = s.split("/", 1)[0]
    if first == "ATLAS":
        return "atlas"
    if first == "ARCTURION_EXTERNAL_RUNTIME":
        return "runtime"
    if first in ("ARCTURION_Brain", "ARCTURION_HQ", ".claude"):
        return "hq"
    if first in CONSTELLATION_AGENTS:
        return "constellation"
    # ORION's DeepSearch research notes live outside the ORION agent home but are ORION
    # output → constellation, matching prong_for_path's Desktop fallback (resolver-align
    # 2026-06-16, ALTAIR review follow-up; keeps backfill from flipping these to hq).
    if first == "Orion DeepSearch":
        return "constellation"
    return "hq"


# --- live-brain 2026-11-07: recency, supersession and journals -------------------------------
# Ranking-only historical-copy class (rank_signals.is_historical_copy); ARCHIVE_MARKERS is left
# alone because the walker's skip rules share it.
HISTORICAL_COPY_RANKING = _envflag("ARCTURION_BRAIN_HISTORICAL_COPY_RANKING", "1")
HISTORICAL_COPY_PENALTY = float(os.environ.get("ARCTURION_BRAIN_HISTORICAL_COPY_PENALTY", "20.0"))
# "status / state / progress ..." ask about the present just like "current" does.
CURRENT_INTENT_STATUS_TERMS = _envflag("ARCTURION_BRAIN_CURRENT_INTENT_STATUS_TERMS", "1")
# Judge-verified supersession from journals (journal --supersedes--> older node).
JOURNAL_SUPERSESSION = _envflag("ARCTURION_BRAIN_JOURNAL_SUPERSESSION", "1")
# Maintained project-status notes, pointers and verified journals are authored current records,
# the same class as Decisions/Preferences above.
CURRENT_RECORD_SHELVES = _envflag("ARCTURION_BRAIN_CURRENT_RECORD_SHELVES", "1")
if CURRENT_RECORD_SHELVES:
    DURABLE_SOURCE_PATH_MARKERS = DURABLE_SOURCE_PATH_MARKERS + (
        "/memory/projects/", "/memory/pointers/", "arcturion_brain/journals/")
    # COVENANT's governance shelves are the canonical doctrine every agent is bound by.
    DURABLE_SOURCE_PATH_MARKERS = DURABLE_SOURCE_PATH_MARKERS + (
        "covenant/00 🛡 governance/laws/", "covenant/00 🛡 governance/commandments/")
# Session distillations are machine summaries ABOUT one past session: never maintained after the
# fact. They stay retrievable on their merits, but a maintained record on the same subject wins.
SESSION_DISTILLATION_PENALTY = float(os.environ.get("ARCTURION_BRAIN_SESSION_DISTILLATION_PENALTY", "6.0"))
SESSION_DISTILLATION_MARKERS = ("/sessions/distilled/", "/0608_session_distillations/")
