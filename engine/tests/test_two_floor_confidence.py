"""Hermetic unit tests for two-floor confidence (Leg 5, retrieval-honesty 2026-08-06).

`RELEVANCE_FLOOR` was one number serving two incommensurable scales (raw cosine and
IDF-weighted lexical coverage) via `max()`. A well-formed conversational question clears
0.42 on cosine ALONE regardless of whether the corpus answers it — golden guard pass rate at
that baseline: 0.462 (11 negatives + 2 contamination).

TWO_FLOOR_CONFIDENCE (`rank_signals.node_passes_two_floor` / `pack_passes_floor`) splits the
single floor into:
  - `lexical_coverage >= LEXICAL_FLOOR`         — sufficient ALONE
  - `sim >= SEMANTIC_FLOOR`                     — sufficient ALONE
  - `CORROBORATION_SIM <= sim < SEMANTIC_FLOOR` — passes only with real lexical corroboration
    (`lexical_coverage >= CORROBORATION_LEX_MIN`), not a bare non-zero.

Covers:
  1. each floor sufficient alone (lexical / semantic)
  2. corroboration band passes with real coverage, refuses on a weak/incidental one
  3. RELEVANCE_FLOOR_RARE_TOKEN_BYPASS still short-circuits both mechanisms
  4. RETRIEVAL_V2=0 / TWO_FLOOR_CONFIDENCE=0 is byte-identical to the pre-Leg-5 single floor
  5. LOW_CONFIDENCE_MARKER still fires end-to-end through brain.query() under two-floor mode

Run:  python3 -m engine.tests.test_two_floor_confidence
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from .. import config as C
from .. import db as DB


def _n(nid, title, path, summary, status="active", last_modified="2026-06-16T00:00:00Z",
       body=None):
    return {"id": nid, "type": "Source", "family": "document", "title": title,
            "path": path, "owner": "ALTAIR", "status": status, "governance_tier": 0,
            "content_hash": None, "summary": summary, "body_excerpt": body,
            "centrality": 0.0, "created": None, "last_modified": last_modified,
            "last_reviewed": None, "metadata": {}}


# ---------------------------------------------------------- 1/2. floor + corroboration logic

def test_lexical_floor_sufficient_alone() -> None:
    from .. import rank_signals as RS

    orig = (C.LEXICAL_FLOOR, C.SEMANTIC_FLOOR, C.CORROBORATION_SIM, C.CORROBORATION_LEX_MIN)
    C.LEXICAL_FLOOR, C.SEMANTIC_FLOOR = 0.42, 0.60
    C.CORROBORATION_SIM, C.CORROBORATION_LEX_MIN = 0.45, 0.30
    try:
        node = _n("err", "pasted error string", "x.md", "")
        stats = {"errno": (3, 9.0), "enoent": (2, 9.5)}
        # A pasted-error-string shape: high lexical coverage, mediocre-to-nothing cosine.
        assert RS.lexical_coverage(node, stats) == 0.0  # haystack doesn't literally hold it
        # Simulate coverage directly via a stats/haystack combo that DOES match: reuse the
        # exact-token machinery is overkill here — assert the boundary on the pure function.
        assert RS.node_passes_two_floor(node, {}, sim=0.0) is False  # no signal at all -> False
        print("  ok  1a. lexical floor — node_passes_two_floor is False with zero signal")
    finally:
        (C.LEXICAL_FLOOR, C.SEMANTIC_FLOOR,
         C.CORROBORATION_SIM, C.CORROBORATION_LEX_MIN) = orig


def test_semantic_floor_sufficient_alone() -> None:
    from .. import rank_signals as RS

    orig = (C.LEXICAL_FLOOR, C.SEMANTIC_FLOOR, C.CORROBORATION_SIM, C.CORROBORATION_LEX_MIN)
    C.LEXICAL_FLOOR, C.SEMANTIC_FLOOR = 0.42, 0.60
    C.CORROBORATION_SIM, C.CORROBORATION_LEX_MIN = 0.45, 0.30
    try:
        node = _n("para", "true paraphrase, zero shared vocabulary", "x.md", "")
        stats: dict = {}  # no query terms match this doc at all -> lexical_coverage == 0.0
        assert RS.lexical_coverage(node, stats) == 0.0

        # High sim, zero lexical evidence: semantic floor must pass this ALONE.
        assert RS.node_passes_two_floor(node, stats, sim=0.61) is True
        # Just under the semantic floor, with no corroboration available (stats empty ->
        # lexical_coverage is always 0.0): must fail.
        assert RS.node_passes_two_floor(node, stats, sim=0.59) is False
        print("  ok  1b. semantic floor — sufficient alone at >= SEMANTIC_FLOOR, "
              "fails just below it with no corroboration")
    finally:
        (C.LEXICAL_FLOOR, C.SEMANTIC_FLOOR,
         C.CORROBORATION_SIM, C.CORROBORATION_LEX_MIN) = orig


def test_lexical_floor_pure_boundary() -> None:
    from .. import rank_signals as RS

    orig = (C.LEXICAL_FLOOR, C.SEMANTIC_FLOOR, C.CORROBORATION_SIM, C.CORROBORATION_LEX_MIN)
    C.LEXICAL_FLOOR, C.SEMANTIC_FLOOR = 0.42, 0.60
    C.CORROBORATION_SIM, C.CORROBORATION_LEX_MIN = 0.45, 0.30
    try:
        # A node whose EVERY query term is present (lexical_coverage == 1.0) and whose sim
        # is 0.0 must still pass — the pasted-error-string shape this floor exists for.
        #
        # This assertion originally read sim=0.0 as "an unrelated embedding, OR embeddings
        # unavailable". Refinement 5b (Leg 3, retrieval-precision 2026-08-07) separated those
        # two, because they are not the same evidence and the golden negatives turned on the
        # difference: a document measured at cosine 0.09 that happens to hold every rare query
        # term is a vocabulary collision (neg-07), while a document with NO cosine at all is
        # simply unmeasured, and refusing it would convert a degraded vector channel into a
        # blanket refusal. Both halves are asserted here; see
        # `test_lexical_floor_rare_mass.test_degraded_vector_signal_skips_the_sanity_gate`.
        node = _n("exact", "ENOENT no such file or directory", "x.md", "")
        stats = {"enoent": (2, 9.5)}
        assert RS.lexical_coverage(node, stats) == 1.0
        # No cosine available for this node — full coverage still passes alone.
        assert RS.node_passes_two_floor(node, stats, sim=0.0, sim_trusted=False) is True
        assert RS.pack_passes_floor([node], stats, sims=None)[0] is True
        # The Leg 5 contract itself, unchanged, with the refinement's flags off.
        orig_flags = (C.LEXICAL_FLOOR_RARE_MASS, C.LEXICAL_FLOOR_SIM_SANITY)
        C.LEXICAL_FLOOR_RARE_MASS = C.LEXICAL_FLOOR_SIM_SANITY = False
        try:
            assert RS.node_passes_two_floor(node, stats, sim=0.0) is True
        finally:
            (C.LEXICAL_FLOOR_RARE_MASS, C.LEXICAL_FLOOR_SIM_SANITY) = orig_flags
        print("  ok  1c. lexical floor — 1.0 coverage passes alone when no cosine exists "
              "(and under Leg 5 flags-off, at sim=0.0 unconditionally)")
    finally:
        (C.LEXICAL_FLOOR, C.SEMANTIC_FLOOR,
         C.CORROBORATION_SIM, C.CORROBORATION_LEX_MIN) = orig


def test_corroboration_band() -> None:
    from .. import rank_signals as RS

    orig = (C.LEXICAL_FLOOR, C.SEMANTIC_FLOOR, C.CORROBORATION_SIM, C.CORROBORATION_LEX_MIN)
    C.LEXICAL_FLOOR, C.SEMANTIC_FLOOR = 0.42, 0.60
    C.CORROBORATION_SIM, C.CORROBORATION_LEX_MIN = 0.45, 0.30
    try:
        # Middling sim (inside [CORROBORATION_SIM, SEMANTIC_FLOOR)) with REAL lexical
        # corroboration (>= CORROBORATION_LEX_MIN, but below the full LEXICAL_FLOOR) passes.
        # This is exactly para-04's shape (sim 0.473 / lex 0.364) from the golden set.
        node = _n("mid", "genuine paraphrase, modest shared vocabulary", "x.md", "")
        # Two matched terms (restart, app) plus one unmatched rare term (misbehaving)
        # dilute coverage to land inside the corroboration band (0.333) — below the full
        # LEXICAL_FLOOR, above CORROBORATION_LEX_MIN.
        stats = {"restart": (200, 3.0), "app": (400, 2.0), "misbehaving": (1, 10.0)}
        node["summary"] = "restart the app before escalating"
        cov = RS.lexical_coverage(node, stats)
        assert C.CORROBORATION_LEX_MIN <= cov < C.LEXICAL_FLOOR, cov
        assert RS.node_passes_two_floor(node, stats, sim=0.47) is True, (
            f"middling sim + real corroboration should pass (cov={cov})")

        # ZERO / near-zero coverage at the SAME middling sim must refuse — this is exactly
        # the failing-negative shape (neg-03 "goats": sim 0.452, lex 0.093).
        empty_node = _n("weak", "unrelated document", "x.md", "")
        weak_stats = {"goats": (6, 9.0), "founder": (11967, 1.6), "own": (5475, 2.4),
                      "does": (3401, 2.8)}
        empty_node["summary"] = "the founder identity record"  # no "goats", no "own"/"does"
        weak_cov = RS.lexical_coverage(empty_node, weak_stats)
        assert weak_cov < C.CORROBORATION_LEX_MIN, weak_cov
        assert RS.node_passes_two_floor(empty_node, weak_stats, sim=0.452) is False, (
            f"middling sim + incidental coverage must refuse (cov={weak_cov})")
        print("  ok  2. corroboration band — real coverage passes, incidental "
              "common-word coverage at the same sim refuses")
    finally:
        (C.LEXICAL_FLOOR, C.SEMANTIC_FLOOR,
         C.CORROBORATION_SIM, C.CORROBORATION_LEX_MIN) = orig


# --------------------------------------------------------------- 3. rare-token bypass intact

def test_rare_token_bypass_unaffected_by_two_floor() -> None:
    """The bypass lives in brain.py, upstream of pack_passes_floor / pack_confidence — it
    short-circuits BOTH mechanisms identically because it never calls either. This asserts
    the invariant the bypass depends on: a bypassed query never reaches the two-floor
    decision at all, so TWO_FLOOR_CONFIDENCE cannot change bypass behaviour."""
    import inspect
    from .. import brain as B

    src = inspect.getsource(B._query_impl)
    assert "bypass" in src and "RELEVANCE_FLOOR_RARE_TOKEN_BYPASS" in src, (
        "bypass gate must still exist upstream of the floor decision")
    # The bypass check must be evaluated before EITHER floor path runs.
    bypass_idx = src.index("bypass = (")
    two_floor_idx = src.index("TWO_FLOOR_CONFIDENCE")
    assert bypass_idx < two_floor_idx, (
        "bypass must short-circuit before the two-floor branch is reached")
    print("  ok  3. rare-token bypass — still gates upstream of both floor mechanisms")


# ----------------------------------------------------------------- 4. v1 byte-identical

def _fixture_db(tmp: Path):
    conn = DB.connect(C.BRAIN_DB)
    DB.init_db(conn)
    DB.upsert_node(conn, _n("gnu_old", "GnuCash is the canonical books system",
                            "SIRIUS/decisions/gnucash.md",
                            "GnuCash is the system of record for the books.",
                            last_modified="2024-06-07T00:00:00Z"))
    DB.upsert_node(conn, _n("csv_new", "GnuCash is dead, the CSV reader is canonical",
                            "SIRIUS/decisions/csv_reader.md",
                            "GnuCash is retired. The CSV reader is the canonical books "
                            "system of record.",
                            last_modified="2026-07-05T00:00:00Z"))
    DB.upsert_node(conn, _n("unrelated", "Dehumidifier wifi setup",
                            "ALTAIR/home/dehumidifier.md",
                            "How the dehumidifier joins the wifi network."))
    conn.commit()
    return conn


def test_v2_byte_identical_when_two_floor_off() -> None:
    """TWO_FLOOR_CONFIDENCE=0 must reproduce the pre-Leg-5 single-floor decision exactly:
    the same pass/refuse outcome as `max(sim, lex) >= RELEVANCE_FLOOR`, unconditionally."""
    tmp = Path(tempfile.mkdtemp(prefix="brain_two_floor_test_"))
    orig_db = C.BRAIN_DB
    orig_v2 = C.RETRIEVAL_V2
    orig_two_floor = C.TWO_FLOOR_CONFIDENCE
    orig_vec = C.VECTOR_SEED_ENABLED
    orig_floor = C.RELEVANCE_FLOOR
    C.BRAIN_DB = tmp / "brain.sqlite"
    C.VECTOR_SEED_ENABLED = False
    try:
        from .. import brain, rank_signals as RS
        RS.reset_cache()
        conn = _fixture_db(tmp)

        C.RETRIEVAL_V2 = True
        C.TWO_FLOOR_CONFIDENCE = False
        C.RELEVANCE_FLOOR = 0.9
        NO_ANSWER = "how does the quarterly submarine lease renewal affect the wifi network"
        pack_off = brain.query(NO_ANSWER, token_budget=2000)
        assert pack_off.text_bundle.startswith(C.LOW_CONFIDENCE_MARKER), pack_off.text_bundle

        C.RELEVANCE_FLOOR = 0.0
        pack_off2 = brain.query("canonical books system of record", token_budget=2000)
        assert not pack_off2.text_bundle.startswith(C.LOW_CONFIDENCE_MARKER)
        assert pack_off2.nodes

        conn.close()
        print("  ok  4. TWO_FLOOR_CONFIDENCE=0 — single-floor decision unchanged "
              "(empties at 0.9, never floors at 0.0)")
    finally:
        C.BRAIN_DB = orig_db
        C.RETRIEVAL_V2 = orig_v2
        C.TWO_FLOOR_CONFIDENCE = orig_two_floor
        C.VECTOR_SEED_ENABLED = orig_vec
        C.RELEVANCE_FLOOR = orig_floor
        from .. import rank_signals as RS
        RS.reset_cache()
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------- 5. LOW_CONFIDENCE_MARKER end-to-end

def test_marker_fires_end_to_end_under_two_floor() -> None:
    """A refused pack returns the LOW_CONFIDENCE_MARKER sentinel with zero nodes, not an
    empty crash — under TWO_FLOOR_CONFIDENCE, exactly as it must under the v1 single floor."""
    tmp = Path(tempfile.mkdtemp(prefix="brain_two_floor_e2e_"))
    orig_db = C.BRAIN_DB
    orig_v2 = C.RETRIEVAL_V2
    orig_two_floor = C.TWO_FLOOR_CONFIDENCE
    orig_vec = C.VECTOR_SEED_ENABLED
    orig = (C.RELEVANCE_FLOOR, C.LEXICAL_FLOOR, C.SEMANTIC_FLOOR,
            C.CORROBORATION_SIM, C.CORROBORATION_LEX_MIN)
    C.BRAIN_DB = tmp / "brain.sqlite"
    C.VECTOR_SEED_ENABLED = False
    try:
        from .. import brain, rank_signals as RS
        RS.reset_cache()
        conn = _fixture_db(tmp)

        C.RETRIEVAL_V2 = True
        C.TWO_FLOOR_CONFIDENCE = True
        # Impossible-to-clear semantic/lexical floors with an outer ceiling that still lets
        # the two-floor branch run (RELEVANCE_FLOOR far below the impossible inner floors).
        # 2.0 is unclearable by construction: lexical_coverage and cosine sim are both
        # capped at 1.0, so no node can ever satisfy any of the three floor conditions —
        # unlike 0.99, which a perfect 1.0 lexical match (this fixture's exact-phrase
        # query) would still clear.
        C.RELEVANCE_FLOOR = 0.0
        C.LEXICAL_FLOOR = 2.0
        C.SEMANTIC_FLOOR = 2.0
        C.CORROBORATION_SIM = 2.0
        C.CORROBORATION_LEX_MIN = 2.0
        pack = brain.query("canonical books system of record", token_budget=2000)
        assert pack.nodes == [], f"two-floor did not empty an unclearable pack: {pack.nodes}"
        assert pack.text_bundle.startswith(C.LOW_CONFIDENCE_MARKER), pack.text_bundle
        assert any(w.startswith("low_confidence_empty:") for w in pack.warnings), pack.warnings
        assert "two-floor gate" in pack.text_bundle, pack.text_bundle
        assert "< floor" not in pack.text_bundle, (
            "two-floor rejection must not compare aggregate confidence against the outer floor: "
            f"{pack.text_bundle}")
        assert isinstance(pack.text_bundle, str) and pack.text_bundle, (
            "refusal must return the marker sentence, never an empty crash")

        # And the inverse: floors at 0 must never fire under two-floor mode either.
        C.LEXICAL_FLOOR = 0.0
        C.SEMANTIC_FLOOR = 0.0
        C.CORROBORATION_SIM = 0.0
        C.CORROBORATION_LEX_MIN = 0.0
        pack2 = brain.query("canonical books system of record", token_budget=2000)
        assert pack2.nodes, "two-floor at 0.0 still emptied an answerable pack"
        assert not pack2.text_bundle.startswith(C.LOW_CONFIDENCE_MARKER)

        conn.close()
        print("  ok  5. LOW_CONFIDENCE_MARKER — fires end to end under two-floor mode "
              "(unclearable -> marker + empty nodes; floors at 0 -> never fires)")
    finally:
        C.BRAIN_DB = orig_db
        C.RETRIEVAL_V2 = orig_v2
        C.TWO_FLOOR_CONFIDENCE = orig_two_floor
        C.VECTOR_SEED_ENABLED = orig_vec
        (C.RELEVANCE_FLOOR, C.LEXICAL_FLOOR, C.SEMANTIC_FLOOR,
         C.CORROBORATION_SIM, C.CORROBORATION_LEX_MIN) = orig
        from .. import rank_signals as RS
        RS.reset_cache()
        shutil.rmtree(tmp, ignore_errors=True)


def run() -> int:
    test_lexical_floor_sufficient_alone()
    test_semantic_floor_sufficient_alone()
    test_lexical_floor_pure_boundary()
    test_corroboration_band()
    test_rare_token_bypass_unaffected_by_two_floor()
    test_v2_byte_identical_when_two_floor_off()
    test_marker_fires_end_to_end_under_two_floor()
    print("PASS — two-floor confidence: lexical/semantic sufficiency, corroboration band, "
          "rare-token bypass upstream, v1 byte-identical, marker end-to-end")
    return 0


if __name__ == "__main__":
    sys.exit(run())
