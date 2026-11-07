"""engine.maintenance.edge_endpoint_resolve / walkable_predicates — no pytest.

Builds a small throwaway sqlite fixture shaped like the real dangling-edge
classes this module targets (alias-table identity mismatch, path-suffix
match, curated name-identity match) plus the classes that must stay refused
(no corresponding node under any name; malformed non-id endpoints). Asserts
the resolver fixes exactly what it should and nothing else, that writes are
additive (original dangling edges untouched, resolved copies tagged and
reversible), and that the manifest agrees with `engine.db.edge_health`.

Run:  python3 -m engine.tests.test_edge_endpoint_resolve
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from .. import db as DB
from ..maintenance import edge_endpoint_resolve as ER
from ..maintenance import walkable_predicates as WP


def _fixture_conn():
    tmp = Path(tempfile.mkdtemp(prefix="edge_resolve_")) / "fixture.sqlite"
    conn = DB.connect(tmp)
    DB.init_db(conn)

    nodes = [
        # (id, type, family, title, path)
        ("app_chrome_bfa04b", "Application", "application", "Chrome", None),
        ("app_safari_c148a3", "Application", "application", "Safari", None),
        ("mem_test_significance_py_cac639", "Source", "document", "test_significance.py",
         "examples/fixture_0.md"),
        ("founder_avery_example", "Founder", "document", "Avery Example", None),
        ("bookmark_1", "Bookmark", "external", "a bookmark", None),
    ]
    for nid, typ, fam, title, path in nodes:
        conn.execute(
            "INSERT INTO nodes (id, type, family, title, path) VALUES (?,?,?,?,?)",
            (nid, typ, fam, title, path),
        )

    conn.execute(
        "INSERT INTO aliases (alias, canonical_id) VALUES (?,?)",
        ("avery", "founder_avery_example"),
    )
    # A dangling alias whose canonical itself has no node — must NOT resolve.
    conn.execute(
        "INSERT INTO aliases (alias, canonical_id) VALUES (?,?)",
        ("ghost phrase", "concept_never_materialized"),
    )

    edges = [
        # predicate, from_id, to_id, family, asserted_by  — dangling via name-identity slug
        ("bookmarked_in", "bookmark_1", "tool_chrome", "external", "HARVEST_EXT_CHROME"),
        # dangling via alias table (case-insensitive)
        ("synonymous_with", "founder_avery_example", "Avery", "semantic", "ENTITY_ALIASES_MD"),
        # dangling via path-suffix match
        ("tested_by", "examples/fixture_1.md",
         "mem_test_significance_py_cac639", "reasoning", "INVERSE_DERIVATION"),
        # genuinely unresolvable: no node under any name for this service id
        ("configured_by", "phantom_service", "mem_test_significance_py_cac639",
         "structural", "INVERSE_DERIVATION"),
        # genuinely unresolvable: alias exists but its canonical is not a node
        ("synonymous_with", "concept_never_materialized", "ghost phrase",
         "semantic", "ENTITY_ALIASES_MD"),
        # a DIFFERENT predicate, already fully walkable — must be left completely alone
        ("contains_bookmark", "app_safari_c148a3", "bookmark_1", "external", "INVERSE_DERIVATION"),
    ]
    for pred, f, t, fam, ab in edges:
        conn.execute(
            "INSERT INTO edges (from_id, to_id, predicate, family, asserted_by, asserted_at) "
            "VALUES (?,?,?,?,?,?)",
            (f, t, pred, fam, ab, "2026-01-01T00:00:00-04:00"),
        )
    conn.commit()
    return conn, tmp


def test_resolves_name_identity_alias_and_path_mechanisms() -> None:
    conn, _tmp = _fixture_conn()
    try:
        health = DB.edge_health(conn)
        assert set(health["fully_dangling_predicates"]) >= {
            "bookmarked_in", "synonymous_with", "tested_by", "configured_by",
        }, health["fully_dangling_predicates"]

        result = ER.run(conn, health["fully_dangling_predicates"], dry_run=False)
        assert result["resolved_edges"] >= 3, result

        health_after = DB.edge_health(conn)
        still_dead = set(health_after["fully_dangling_predicates"])
        assert "bookmarked_in" not in still_dead, "tool_chrome->Chrome should have resolved"
        assert "synonymous_with" not in still_dead, "alias-table match should have resolved"
        assert "tested_by" not in still_dead, "path-suffix match should have resolved"
        # No node exists for phantom_service under any mechanism — must stay dangling.
        assert "configured_by" in still_dead, "phantom_service must NOT be invented a node"
    finally:
        conn.close()


def test_original_dangling_edges_are_never_mutated_only_appended() -> None:
    """Writes must be additive/reversible — the original row survives verbatim, and the
    resolved copy is tagged so it can be found and deleted to undo the pass."""
    conn, _tmp = _fixture_conn()
    try:
        before = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE from_id='bookmark_1' AND to_id='tool_chrome'"
        ).fetchone()[0]
        assert before == 1

        ER.run(conn, ["bookmarked_in"], dry_run=False)

        after = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE from_id='bookmark_1' AND to_id='tool_chrome'"
        ).fetchone()[0]
        assert after == 1, "original dangling edge must be untouched, not rewritten"

        tagged = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE asserted_by LIKE ?",
            (f"%{ER.PRODUCER_SUFFIX}",),
        ).fetchone()[0]
        assert tagged >= 1, "resolved copy must carry the reversible producer tag"

        # Undo path: deleting the tagged rows must restore the pre-resolution state.
        conn.execute("DELETE FROM edges WHERE asserted_by LIKE ?", (f"%{ER.PRODUCER_SUFFIX}",))
        conn.commit()
        health = DB.edge_health(conn)
        assert "bookmarked_in" in health["fully_dangling_predicates"], "undo did not restore dangling state"
    finally:
        conn.close()


def test_already_walkable_edges_are_left_alone() -> None:
    conn, _tmp = _fixture_conn()
    try:
        before = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        ER.run(conn, ["bookmarked_in", "contains_bookmark"], dry_run=False)
        after_safari_rows = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE from_id='app_safari_c148a3' AND to_id='bookmark_1'"
        ).fetchone()[0]
        assert after_safari_rows == 1, "an already-walkable edge must not be duplicated"
        after = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        assert after > before, "resolvable dangling edges should have produced new rows"
    finally:
        conn.close()


def test_dry_run_writes_nothing() -> None:
    conn, _tmp = _fixture_conn()
    try:
        before = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        result = ER.run(conn, ["bookmarked_in", "synonymous_with", "tested_by"], dry_run=True)
        assert result["resolved_edges"] >= 3
        after = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        assert after == before, "dry_run must not write any rows"
    finally:
        conn.close()


def test_resolved_count_and_inserted_count_diverge_on_many_to_one_collapse() -> None:
    """Two distinct dangling edges that both resolve to the SAME (from,to) pair (e.g. two
    alias phrases for one canonical) must count as 2 resolutions but only 1 actual insert
    — the second is a legitimate UNIQUE(from_id,to_id,predicate,asserted_by) collision, not
    a bug. `run()` must report both numbers, and the DB row count must match `inserted`,
    never `resolved` (2026-08-08 orchestrator review caught this — a prior version reported
    553 'resolved' when only 425 rows had actually landed)."""
    conn, _tmp = _fixture_conn()
    try:
        # A second alias phrase for the same canonical as the 'avery' edge already in the
        # fixture — both resolve to founder_avery_example -> founder_avery_example.
        conn.execute(
            "INSERT INTO aliases (alias, canonical_id) VALUES (?,?)",
            ("jj", "founder_avery_example"),
        )
        conn.execute(
            "INSERT INTO edges (from_id, to_id, predicate, family, asserted_by, asserted_at) "
            "VALUES (?,?,?,?,?,?)",
            ("founder_avery_example", "Jj", "synonymous_with", "semantic",
             "ENTITY_ALIASES_MD", "2026-01-01T00:00:00-04:00"),
        )
        conn.commit()

        before_rows = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE from_id='founder_avery_example' "
            "AND to_id='founder_avery_example'"
        ).fetchone()[0]
        assert before_rows == 0

        result = ER.run(conn, ["synonymous_with"], dry_run=False)
        assert result["by_predicate"]["synonymous_with"]["resolved"] == 2, result
        assert result["by_predicate"]["synonymous_with"]["inserted"] == 1, result
        assert result["resolved_edges"] > result["inserted_edges"], result

        after_rows = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE from_id='founder_avery_example' "
            "AND to_id='founder_avery_example'"
        ).fetchone()[0]
        assert after_rows == 1, "DB row count must match inserted, not resolved"

        total_tagged = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE asserted_by LIKE ?",
            (f"%{ER.PRODUCER_SUFFIX}",),
        ).fetchone()[0]
        assert total_tagged == result["inserted_edges"], (
            "reported inserted_edges must equal the actual tagged row count in the DB"
        )
    finally:
        conn.close()


def test_manifest_agrees_with_edge_health() -> None:
    conn, tmp = _fixture_conn()
    try:
        ER.run(conn, DB.edge_health(conn)["fully_dangling_predicates"], dry_run=False)
        health = DB.edge_health(conn)
    finally:
        conn.close()

    manifest = WP.build(tmp)
    walkable_in_manifest = {p["predicate"] for p in manifest["predicates"] if p["walkable"]}
    dead_in_manifest = {p["predicate"] for p in manifest["predicates"] if not p["walkable"]}

    assert dead_in_manifest == set(health["fully_dangling_predicates"]), (
        dead_in_manifest, health["fully_dangling_predicates"]
    )
    assert manifest["zero_walkable_predicate_count"] == len(health["fully_dangling_predicates"])
    assert "engine/ontology.py" in manifest["source_of_truth"]
    for p in manifest["predicates"]:
        assert p["edge_count"] == p["resolved_endpoint_count"] + p["dangling_edge_count"]


def run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  ❌ {t.__name__}: {e}")
    print(f"\n{'PASS' if not failures else 'FAIL'} — edge endpoint resolve: "
          f"{len(tests) - failures}/{len(tests)} tests")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
