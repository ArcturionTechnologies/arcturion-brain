"""Hermetic unit tests for IDF-weighted lexical scoring (Leg 2 follow-up,
retrieval-honesty 2026-08-06). No pytest.

Covers the two deliverables this leg added on top of the existing retrieval-v2 machinery:

  1. `_lexical_score_idf` — the IDF-weighted twin of `_lexical_score`. A rare query term
     must outrank a common one at equal match count; a query with no rarity information
     must degrade to `_lexical_score`'s equal-weighting behaviour rather than scoring zero.
  2. `_fts_or_seed`'s rarity filter (`C.FTS_SEED_IDF_FILTER`) — sub-threshold-IDF terms are
     dropped from the OR-seed, with a GUARANTEED fallback to the unfiltered term list when
     filtering would empty it.
  3. A/B integrity — with `RETRIEVAL_V2=0`, the v1 path must never call the new scorer, and
     query output must be byte-identical to what it was before this leg.

Run:  python3 -m engine.tests.test_idf_lexical_scoring
"""
from __future__ import annotations

import shutil
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


# --------------------------------------------------------------- 1. _lexical_score_idf


def test_rare_term_outranks_common_term_at_equal_frequency() -> None:
    from .. import brain

    # Both nodes match exactly ONE query term, once, in the title — same field, same
    # count, same phrase-hit tier (neither is a whole-topic match). The only variable is
    # which term each one matched: "zephyr" is corpus-rare, "system" is corpus-common.
    n_docs = 50_000
    stats = {
        "zephyr": (2, brain_idf(n_docs, 2)),
        "system": (9_000, brain_idf(n_docs, 9_000)),
    }
    topic = "zephyr system"
    node_rare = _n("r", "zephyr protocol notes", "A/r.md", "")
    node_common = _n("c", "system protocol notes", "A/c.md", "")

    score_rare = brain._lexical_score_idf(node_rare, topic, stats)
    score_common = brain._lexical_score_idf(node_common, topic, stats)
    assert score_rare > score_common, (
        f"a rare term ({score_rare}) did not outrank a common one ({score_common}) at "
        f"equal match count")

    # The un-weighted scorer must NOT discriminate between them — same field, same count,
    # different only in which term matched. This is the exact defect being fixed.
    assert brain._lexical_score(node_rare, topic) == brain._lexical_score(node_common, topic)

    # The query's rarest term keeps `_lexical_score`'s full per-term constant (6.0 for a
    # title hit) — the ceiling is unchanged, only the distribution among a query's own
    # terms shifts. This is what keeps `C.LEXICAL_WEIGHT`'s calibrated magnitude intact.
    assert abs(score_rare - brain._lexical_score(node_rare, topic)) < 1e-9, (
        "the query's rarest term must score identically to the unweighted scorer")
    print("  ok  1. _lexical_score_idf — rare term outranks common at equal frequency; "
          "unweighted scorer can't tell them apart; rarest term keeps full weight")


def brain_idf(n_docs: int, df: int) -> float:
    from .. import rank_signals as RS
    return RS.idf(df, n_docs)


def test_idf_scorer_degrades_to_equal_weighting_with_no_rarity_signal() -> None:
    """Empty stats (or every term priced at 0 IDF) must not zero out the per-term signal —
    a query this function cannot discriminate must score like `_lexical_score`, matching
    the degrade-to-lexical behaviour `_query_impl`'s `have_signal` check already assumes
    elsewhere in v2."""
    from .. import brain

    topic = "zephyr system"
    node = _n("n", "zephyr system notes", "A/n.md", "")

    for empty_stats in ({}, {"zephyr": (-1, 0.0), "system": (-1, 0.0)}):
        idf_score = brain._lexical_score_idf(node, topic, empty_stats)
        plain_score = brain._lexical_score(node, topic)
        assert abs(idf_score - plain_score) < 1e-9, (
            f"no-signal degrade failed for {empty_stats!r}: {idf_score} != {plain_score}")
    print("  ok  2. _lexical_score_idf — degrades to equal weighting when stats carry no "
          "rarity information (empty, or every term unknown/zero-IDF)")


def test_idf_scorer_uniform_rarity_matches_lexical_score_exactly() -> None:
    """When every query term is EQUALLY rare (w = idf/max_idf = 1.0 for all of them), the
    weighted scorer must reproduce `_lexical_score` exactly — phrase-hit tier and per-term
    tier alike. This is the ceiling case referenced in the scorer's docstring: rarity
    weighting only ever redistributes AWAY from this baseline, never below it uniformly."""
    from .. import brain

    topic = "canonical books system"
    same_idf = brain_idf(50_000, 5)
    stats = {"canonical": (5, same_idf), "books": (5, same_idf), "system": (5, same_idf)}
    for node in (
        _n("exact", "canonical books system", "A/x.md", ""),
        _n("prefix", "canonical books system overview", "A/y.md", ""),
        _n("substr", "the canonical books system, explained", "A/z.md", ""),
        _n("summary_hit", "unrelated title", "A/w.md", "canonical books system"),
    ):
        idf_score = brain._lexical_score_idf(node, topic, stats)
        plain_score = brain._lexical_score(node, topic)
        assert abs(idf_score - plain_score) < 1e-9, (
            node["id"], idf_score, plain_score)
    print("  ok  3. _lexical_score_idf — uniform query-term rarity reproduces "
          "_lexical_score exactly (phrase-hit tier + per-term tier alike)")


# --------------------------------------------------------------- 2. FTS seed rarity filter


def _fts_fixture_db(tmp: Path):
    conn = DB.connect()
    DB.init_db(conn)
    # "common" appears in every doc (near-stopword-grade for this tiny corpus); "rare"
    # appears in exactly one. A real corpus has thousands of docs — this fixture only needs
    # the RELATIVE df gap between the two terms, which document_frequency reads off
    # fts_nodes directly regardless of corpus size.
    for i in range(6):
        DB.upsert_node(conn, _n(f"common_{i}", f"common document {i}", f"A/c{i}.md",
                                "a common word appears in every one of these documents"))
    DB.upsert_node(conn, _n("rare_1", "the rare target document", "A/rare.md",
                            "this document alone contains the rare word"))
    conn.commit()
    return conn


def test_fts_seed_idf_filter_drops_common_terms() -> None:
    from .. import brain, rank_signals as RS

    tmp = Path(tempfile.mkdtemp(prefix="brain_idf_fts_test_"))
    orig_db, orig_filter, orig_min = C.BRAIN_DB, C.FTS_SEED_IDF_FILTER, C.FTS_SEED_MIN_IDF
    C.BRAIN_DB = tmp / "brain.sqlite"
    try:
        RS.reset_cache()
        conn = _fts_fixture_db(tmp)
        terms = ["common", "rare"]
        stats = RS.term_stats(conn, terms)
        # "common" must price far lower than "rare" in this fixture — it's in every doc.
        assert stats["rare"][1] > stats["common"][1], stats

        # Filter ON, threshold between the two: "common" is dropped, "rare" survives, and
        # the OR-seed is scoped to the one document that actually matters.
        C.FTS_SEED_IDF_FILTER = True
        C.FTS_SEED_MIN_IDF = (stats["common"][1] + stats["rare"][1]) / 2
        filtered_rows = brain._fts_or_seed(conn, terms, 50, stats=stats)
        filtered_ids = {r["id"] for r in filtered_rows}
        assert filtered_ids == {"rare_1"}, (
            f"the common term was not filtered out of the OR-seed: {filtered_ids}")

        # Filter OFF: both terms seed, and every doc (common + rare) is a candidate again.
        C.FTS_SEED_IDF_FILTER = False
        unfiltered_rows = brain._fts_or_seed(conn, terms, 50, stats=stats)
        unfiltered_ids = {r["id"] for r in unfiltered_rows}
        assert unfiltered_ids == {f"common_{i}" for i in range(6)} | {"rare_1"}, unfiltered_ids
        conn.close()
        print("  ok  4. FTS seed IDF filter — a sub-threshold term is dropped from the "
              "OR-seed when the flag is on, present when it is off")
    finally:
        C.BRAIN_DB, C.FTS_SEED_IDF_FILTER, C.FTS_SEED_MIN_IDF = orig_db, orig_filter, orig_min
        RS.reset_cache()
        shutil.rmtree(tmp, ignore_errors=True)


def test_fts_seed_idf_filter_empty_filter_fallback() -> None:
    """GUARANTEED FALLBACK: if every term in the query prices below the threshold, the seed
    channel must return the SAME thing it would with the filter off — never an empty seed
    because a ranking refinement over-filtered a query that is entirely common words."""
    from .. import brain, rank_signals as RS

    tmp = Path(tempfile.mkdtemp(prefix="brain_idf_fallback_test_"))
    orig_db, orig_filter, orig_min = C.BRAIN_DB, C.FTS_SEED_IDF_FILTER, C.FTS_SEED_MIN_IDF
    C.BRAIN_DB = tmp / "brain.sqlite"
    try:
        RS.reset_cache()
        conn = _fts_fixture_db(tmp)
        terms = ["common"]
        stats = RS.term_stats(conn, terms)

        C.FTS_SEED_IDF_FILTER = True
        # A threshold above every term's IDF — filtering would empty the seed list entirely.
        C.FTS_SEED_MIN_IDF = stats["common"][1] + 100.0
        fallback_rows = brain._fts_or_seed(conn, terms, 50, stats=stats)
        fallback_ids = {r["id"] for r in fallback_rows}

        C.FTS_SEED_IDF_FILTER = False
        unfiltered_rows = brain._fts_or_seed(conn, terms, 50, stats=stats)
        unfiltered_ids = {r["id"] for r in unfiltered_rows}

        assert fallback_ids == unfiltered_ids and fallback_ids, (
            f"empty-filter fallback did not restore the unfiltered seed: "
            f"{fallback_ids} != {unfiltered_ids}")
        conn.close()
        print("  ok  5. FTS seed IDF filter — empty-filter fallback restores the "
              "unfiltered term list rather than seeding nothing")
    finally:
        C.BRAIN_DB, C.FTS_SEED_IDF_FILTER, C.FTS_SEED_MIN_IDF = orig_db, orig_filter, orig_min
        RS.reset_cache()
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------- 3. A/B integrity


def _ab_fixture_db(tmp: Path):
    conn = DB.connect()
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


def test_v1_path_never_calls_the_idf_scorer() -> None:
    """`RETRIEVAL_V2=0` must route through `_lexical_score` only. Proven by making
    `_lexical_score_idf` raise and confirming a v1 query still succeeds — if v1 called it
    even once, this test fails loudly instead of silently passing on a code path that
    happens not to need the result."""
    from .. import brain

    tmp = Path(tempfile.mkdtemp(prefix="brain_idf_ab_test_"))
    orig_db, orig_v2, orig_vec = C.BRAIN_DB, C.RETRIEVAL_V2, C.VECTOR_SEED_ENABLED
    orig_idf_flag = C.IDF_LEXICAL_WEIGHTING
    orig_fn = brain._lexical_score_idf
    C.BRAIN_DB = tmp / "brain.sqlite"
    C.VECTOR_SEED_ENABLED = False
    try:
        from .. import rank_signals as RS
        RS.reset_cache()
        conn = _ab_fixture_db(tmp)
        conn.close()

        def _boom(*a, **kw):
            raise AssertionError("v1 path called _lexical_score_idf")

        brain._lexical_score_idf = _boom
        C.RETRIEVAL_V2 = False
        C.IDF_LEXICAL_WEIGHTING = True   # on, but must be irrelevant on the v1 path
        pack = brain.query("canonical books system of record", token_budget=2000)
        assert pack.nodes, "v1 query returned nothing while proving isolation"
        print("  ok  6. RETRIEVAL_V2=0 — never calls _lexical_score_idf, flag value "
              "irrelevant")
    finally:
        brain._lexical_score_idf = orig_fn
        C.BRAIN_DB, C.RETRIEVAL_V2 = orig_db, orig_v2
        C.VECTOR_SEED_ENABLED, C.IDF_LEXICAL_WEIGHTING = orig_vec, orig_idf_flag
        from .. import rank_signals as RS
        RS.reset_cache()
        shutil.rmtree(tmp, ignore_errors=True)


def test_v1_output_byte_identical_regardless_of_new_flags() -> None:
    """The A/B switch's whole point: `RETRIEVAL_V2=0` output must not move by even one byte
    when the new flags are flipped, on a fixed query set. If it did, the "restore the old
    path exactly" guarantee from Leg 2 would be broken by this follow-up."""
    from .. import brain

    tmp = Path(tempfile.mkdtemp(prefix="brain_idf_ab_bytes_test_"))
    orig_db, orig_v2, orig_vec = C.BRAIN_DB, C.RETRIEVAL_V2, C.VECTOR_SEED_ENABLED
    orig_idf_flag, orig_fts_flag = C.IDF_LEXICAL_WEIGHTING, C.FTS_SEED_IDF_FILTER
    C.BRAIN_DB = tmp / "brain.sqlite"
    C.VECTOR_SEED_ENABLED = False
    QUERIES = [
        "canonical books system of record",
        "how does the quarterly submarine lease renewal affect the wifi network",
        "GnuCash",
    ]
    try:
        from .. import rank_signals as RS
        RS.reset_cache()
        conn = _ab_fixture_db(tmp)
        conn.close()
        C.RETRIEVAL_V2 = False

        C.IDF_LEXICAL_WEIGHTING, C.FTS_SEED_IDF_FILTER = False, False
        baseline = {q: brain.query(q, token_budget=2000).text_bundle for q in QUERIES}

        C.IDF_LEXICAL_WEIGHTING, C.FTS_SEED_IDF_FILTER = True, True
        with_new_flags = {q: brain.query(q, token_budget=2000).text_bundle for q in QUERIES}

        for q in QUERIES:
            assert baseline[q] == with_new_flags[q], (
                f"v1 output changed for {q!r} when the new flags flipped on")
        print("  ok  7. RETRIEVAL_V2=0 — output byte-identical across the new flags on a "
              "fixed query set")
    finally:
        C.BRAIN_DB, C.RETRIEVAL_V2 = orig_db, orig_v2
        C.VECTOR_SEED_ENABLED = orig_vec
        C.IDF_LEXICAL_WEIGHTING, C.FTS_SEED_IDF_FILTER = orig_idf_flag, orig_fts_flag
        from .. import rank_signals as RS
        RS.reset_cache()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_rare_term_outranks_common_term_at_equal_frequency()
    test_idf_scorer_degrades_to_equal_weighting_with_no_rarity_signal()
    test_idf_scorer_uniform_rarity_matches_lexical_score_exactly()
    test_fts_seed_idf_filter_drops_common_terms()
    test_fts_seed_idf_filter_empty_filter_fallback()
    test_v1_path_never_calls_the_idf_scorer()
    test_v1_output_byte_identical_regardless_of_new_flags()
    print("PASS — IDF-weighted lexical scoring: scorer rarity discrimination + degrade "
          "path, FTS seed filter + guaranteed fallback, A/B integrity (v1 never calls the "
          "new scorer, output byte-identical across the new flags)")
