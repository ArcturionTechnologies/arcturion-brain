"""Hermetic tests for transitive closure traversal (Task 3). No pytest, no production DB.

Run:  python3 -m engine.tests.test_closure
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from .. import closure as CL
from .. import ontology as O


# ── Fixture ────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE nodes (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, family TEXT NOT NULL,
  title TEXT, path TEXT, owner TEXT, status TEXT, metadata TEXT
);
CREATE TABLE edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_id TEXT NOT NULL, to_id TEXT NOT NULL, predicate TEXT NOT NULL,
  family TEXT NOT NULL, weight REAL DEFAULT 1.0, confidence REAL DEFAULT 1.0,
  asserted_by TEXT, asserted_at TEXT, evidence_span TEXT, metadata TEXT,
  UNIQUE(from_id, to_id, predicate, asserted_by)
);
CREATE INDEX idx_edges_from ON edges(from_id);
CREATE INDEX idx_edges_to ON edges(to_id);
CREATE INDEX idx_edges_pred ON edges(predicate);
"""

_NODES = [
    # linear chain — lin_ghost is deliberately absent (dangling endpoint)
    ("lin_a", "Document", "document", "A"),
    ("lin_b", "Document", "document", "B"),
    ("lin_c", "Document", "document", "C"),
    ("lin_d", "Document", "document", "D"),
    # cycle
    ("cyc_a", "Document", "document", "cycle A"),
    ("cyc_b", "Document", "document", "cycle B"),
    ("cyc_c", "Document", "document", "cycle C"),
    # diamond
    ("dia_a", "Document", "document", "diamond top"),
    ("dia_b", "Document", "document", "diamond left"),
    ("dia_c", "Document", "document", "diamond right"),
    ("dia_d", "Document", "document", "diamond bottom"),
    # fan-out, for the limit hard stop
    ("wide_root", "Document", "document", "wide"),
    ("wide_1", "Document", "document", "w1"),
    ("wide_2", "Document", "document", "w2"),
    ("wide_3", "Document", "document", "w3"),
    ("wide_4", "Document", "document", "w4"),
    ("wide_5", "Document", "document", "w5"),
    # staged-proposal namespace — reachable structure, deliberately NOT excluded
    ("prop_parent", "Document", "document", "holds a staged child"),
    ("prop_c3281f", "decision", "concept", "staged proposal"),
    # non-transitive predicate lives here
    ("rel_a", "Document", "document", "rel A"),
    ("rel_b", "Document", "document", "rel B"),
    # self-edge: the degenerate cycle
    ("selfy", "Document", "document", "points at itself"),
]

_CONTAINS = [
    ("lin_a", "lin_b"), ("lin_b", "lin_c"), ("lin_c", "lin_d"), ("lin_d", "lin_ghost"),
    ("cyc_a", "cyc_b"), ("cyc_b", "cyc_c"), ("cyc_c", "cyc_a"),
    ("dia_a", "dia_b"), ("dia_a", "dia_c"), ("dia_b", "dia_d"), ("dia_c", "dia_d"),
    ("wide_root", "wide_1"), ("wide_root", "wide_2"), ("wide_root", "wide_3"),
    ("wide_root", "wide_4"), ("wide_root", "wide_5"),
    ("prop_parent", "prop_c3281f"),
    ("selfy", "selfy"),
]

_RELATES = [("rel_a", "rel_b"), ("rel_b", "lin_a")]


def _fixture(tmp: Path, name: str = "fixture.sqlite") -> Path:
    db = tmp / name
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes (id, type, family, title) VALUES (?,?,?,?)", _NODES)
    conn.executemany(
        "INSERT INTO edges (from_id, to_id, predicate, family, asserted_by) "
        "VALUES (?,?,'contains','structure','fixture')", _CONTAINS)
    # the SAME contains edge asserted twice — must not become two closure rows
    conn.execute("INSERT INTO edges (from_id, to_id, predicate, family, asserted_by) "
                 "VALUES ('lin_a','lin_b','contains','structure','other-agent')")
    conn.executemany(
        "INSERT INTO edges (from_id, to_id, predicate, family, asserted_by) "
        "VALUES (?,?,'depends_on','semantic','fixture')", _RELATES)
    conn.commit()
    conn.close()
    return db


def _ids(result) -> list[str]:
    return [r["id"] for r in result]


def _depths(result) -> dict[str, int]:
    return {r["id"]: r["depth"] for r in result}


# ── Cases ──────────────────────────────────────────────────────────────────

def case_1_linear_chain_depths(conn) -> None:
    out = CL.descendants("lin_a", "contains", conn=conn)
    assert isinstance(out, list), "the result must still behave as a plain list"
    assert _depths(out) == {"lin_b": 1, "lin_c": 2, "lin_d": 3, "lin_ghost": 4}, _depths(out)
    assert _ids(out) == ["lin_b", "lin_c", "lin_d", "lin_ghost"], "ordered by depth"
    assert not out.truncated and out.truncation_reason is None, repr(out)
    by_id = {r["id"]: r for r in out}
    assert by_id["lin_d"]["path"] == ["lin_a", "lin_b", "lin_c", "lin_d"], by_id["lin_d"]
    assert by_id["lin_d"]["title"] == "D"
    assert by_id["lin_ghost"]["title"] is None, "a dangling endpoint is still reachable"
    for r in out:
        assert r["path"][0] == "lin_a" and r["path"][-1] == r["id"]
        assert len(r["path"]) == r["depth"] + 1, r
    assert "lin_a" not in _depths(out), "reflexive-free: the seed is not its own descendant"
    assert CL.closure_size("lin_a", "contains", conn=conn) == 4


def case_2_ancestors_invert_the_chain(conn) -> None:
    out = CL.ancestors("lin_ghost", "contains", conn=conn)
    assert _depths(out) == {"lin_d": 1, "lin_c": 2, "lin_b": 3, "lin_a": 4}, _depths(out)
    by_id = {r["id"]: r for r in out}
    assert by_id["lin_a"]["path"] == ["lin_ghost", "lin_d", "lin_c", "lin_b", "lin_a"], \
        "path runs seed -> ... -> id in traversal order"
    assert not out.truncated, repr(out)
    # descendants and ancestors are exact inverses over the same predicate
    assert set(_ids(CL.descendants("lin_a", "contains", conn=conn))) == \
        {"lin_b", "lin_c", "lin_d", "lin_ghost"}
    assert CL.closure_size("lin_ghost", "contains", conn=conn, up=True) == 4


def case_3_cycle_terminates_each_node_once(conn) -> None:
    out = CL.descendants("cyc_a", "contains", conn=conn)          # A -> B -> C -> A
    assert _ids(out) == ["cyc_b", "cyc_c"], _ids(out)
    assert _depths(out) == {"cyc_b": 1, "cyc_c": 2}
    assert len(_ids(out)) == len(set(_ids(out))), "each node exactly once"
    assert not out.truncated, "a cycle is closed, not truncated: " + repr(out)

    out_b = CL.descendants("cyc_b", "contains", conn=conn)
    assert _depths(out_b) == {"cyc_c": 1, "cyc_a": 2}, _depths(out_b)
    assert {r["id"]: r["path"] for r in out_b}["cyc_a"] == ["cyc_b", "cyc_c", "cyc_a"]

    out_up = CL.ancestors("cyc_a", "contains", conn=conn)
    assert _depths(out_up) == {"cyc_c": 1, "cyc_b": 2}, _depths(out_up)

    # the seed is absent from its own closure, but the cycle is still observable
    assert CL.is_reachable("cyc_a", "cyc_a", "contains", conn=conn), \
        "a node on a cycle IS reachable from itself"
    assert not CL.is_reachable("lin_a", "lin_a", "contains", conn=conn), \
        "a node on no cycle is not reachable from itself"

    # degenerate cycle: a self-edge must not spin
    assert CL.descendants("selfy", "contains", conn=conn) == []
    assert CL.is_reachable("selfy", "selfy", "contains", conn=conn)


def case_4_non_transitive_predicate_refused(conn) -> None:
    assert not O.is_transitive("depends_on"), "depends_on LOOKS transitive but the canon never says so"
    assert O.transitive_predicates() == frozenset(
        {"contains", "part_of", "owns", "owned_by"}), O.transitive_predicates()

    for fn, args in (
        (CL.descendants, ("rel_a", "depends_on")),
        (CL.ancestors, ("rel_b", "depends_on")),
        (CL.closure_size, ("rel_a", "depends_on")),
    ):
        try:
            fn(*args, conn=conn)
        except ValueError as e:
            assert "not declared transitive" in str(e), str(e)
        else:
            raise AssertionError(f"{fn.__name__} must refuse a non-transitive predicate")
    try:
        CL.is_reachable("rel_a", "rel_b", "depends_on", conn=conn)
    except ValueError as e:
        assert "not declared transitive" in str(e), str(e)
    else:
        raise AssertionError("is_reachable must refuse a non-transitive predicate")

    # ...and an entirely unknown predicate is refused with a DIFFERENT diagnosis
    try:
        CL.descendants("rel_a", "no_such_predicate", conn=conn)
    except ValueError as e:
        assert "not a declared predicate" in str(e), str(e)
    else:
        raise AssertionError("an unknown predicate must be refused")

    # the refusal is not a filter — the edges are really there
    assert conn.execute(
        "SELECT COUNT(*) FROM edges WHERE predicate='depends_on'").fetchone()[0] == 2


def case_5_max_depth_truncates_and_is_marked(conn) -> None:
    out = CL.descendants("lin_a", "contains", conn=conn, max_depth=1)
    assert _ids(out) == ["lin_b"], _ids(out)
    assert out.truncated and out.truncation_reason == "max_depth", repr(out)
    assert "TRUNCATED(max_depth)" in repr(out), repr(out)
    assert out.max_depth == 1

    out3 = CL.descendants("lin_a", "contains", conn=conn, max_depth=3)
    assert _ids(out3) == ["lin_b", "lin_c", "lin_d"]
    assert out3.truncated, "lin_d still has an unexplored child at the boundary"

    out4 = CL.descendants("lin_a", "contains", conn=conn, max_depth=4)
    assert len(out4) == 4 and not out4.truncated, \
        "a closure that merely ENDS at the boundary is complete, not truncated"

    # a boundary with nowhere left to go must NOT be reported as truncated
    dia = CL.descendants("dia_a", "contains", conn=conn, max_depth=2)
    assert len(dia) == 3 and not dia.truncated, repr(dia)

    zero = CL.descendants("lin_a", "contains", conn=conn, max_depth=0)
    assert zero == [] and zero.truncated and zero.truncation_reason == "max_depth", repr(zero)

    try:
        CL.descendants("lin_a", "contains", conn=conn, max_depth=-1)
    except ValueError as e:
        assert "max_depth" in str(e)
    else:
        raise AssertionError("negative max_depth must raise")


def case_6_limit_honoured_and_marked(conn) -> None:
    out = CL.descendants("wide_root", "contains", conn=conn, limit=2)
    assert len(out) == 2, _ids(out)
    assert out.truncated and out.truncation_reason == "limit", repr(out)
    assert out.limit == 2

    full = CL.descendants("wide_root", "contains", conn=conn, limit=5)
    assert len(full) == 5 and not full.truncated, \
        "a closure exactly `limit` long is COMPLETE — nothing was cut: " + repr(full)

    none_ = CL.descendants("wide_root", "contains", conn=conn, limit=0)
    assert none_ == [] and none_.truncated and none_.truncation_reason == "limit", repr(none_)
    leaf0 = CL.descendants("lin_ghost", "contains", conn=conn, limit=0)
    assert leaf0 == [] and not leaf0.truncated, \
        "limit=0 over an empty closure cut nothing: " + repr(leaf0)

    unbounded = CL.descendants("wide_root", "contains", conn=conn, limit=None)
    assert len(unbounded) == 5 and not unbounded.truncated, repr(unbounded)

    # limit wins over the depth check: a limited walk reports "limit", not "max_depth"
    lim = CL.descendants("wide_root", "contains", conn=conn, max_depth=1, limit=2)
    assert lim.truncation_reason == "limit", repr(lim)

    try:
        CL.descendants("lin_a", "contains", conn=conn, limit=-1)
    except ValueError as e:
        assert "limit" in str(e)
    else:
        raise AssertionError("negative limit must raise")


def case_7_is_reachable_both_ways(conn) -> None:
    assert CL.is_reachable("lin_a", "lin_d", "contains", conn=conn)
    assert CL.is_reachable("lin_a", "lin_ghost", "contains", conn=conn)
    assert not CL.is_reachable("lin_d", "lin_a", "contains", conn=conn), \
        "the chain does not run backwards"
    assert CL.is_reachable("lin_d", "lin_a", "contains", conn=conn, up=True), \
        "...but it does when traversing incoming edges"
    assert not CL.is_reachable("lin_a", "cyc_a", "contains", conn=conn), \
        "disconnected components are not reachable"
    assert not CL.is_reachable("lin_a", "lin_d", "contains", conn=conn, max_depth=2), \
        "bounded search: a False can mean 'not within max_depth'"
    assert CL.is_reachable("lin_a", "lin_d", "contains", conn=conn, max_depth=3)


def case_8_diamond_yields_target_once_at_shortest_depth(conn) -> None:
    out = CL.descendants("dia_a", "contains", conn=conn)
    assert _ids(out) == ["dia_b", "dia_c", "dia_d"], _ids(out)
    assert _depths(out) == {"dia_b": 1, "dia_c": 1, "dia_d": 2}, _depths(out)
    d = {r["id"]: r for r in out}["dia_d"]
    assert len(d["path"]) == 3 and d["path"][0] == "dia_a" and d["path"][-1] == "dia_d", d
    assert d["path"][1] in ("dia_b", "dia_c"), "either shortest path is valid"
    assert not out.truncated


def case_9_staged_proposals_are_included(conn) -> None:
    out = CL.descendants("prop_parent", "contains", conn=conn)
    assert _ids(out) == ["prop_c3281f"], \
        ("staged proposals are real reachable structure; traversal judges nothing, "
         "so unlike ontology.audit()/reasoner it must NOT exclude prop_*")
    assert out[0]["title"] == "staged proposal"


def case_10_duplicate_edge_assertions_collapse(conn) -> None:
    assert conn.execute(
        "SELECT COUNT(*) FROM edges WHERE from_id='lin_a' AND to_id='lin_b'"
    ).fetchone()[0] == 2, "fixture assumption: the same edge is asserted twice"
    out = CL.descendants("lin_a", "contains", conn=conn, max_depth=1)
    assert _ids(out) == ["lin_b"], "two assertions of one edge is one closure row"


def case_11_traversal_never_writes(tmp: Path) -> None:
    db = _fixture(tmp, "readonly.sqlite")
    before = (db.stat().st_mtime_ns, db.stat().st_size)
    conn = sqlite3.connect(db)
    n0 = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    e0 = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    tables0 = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    CL.descendants("lin_a", "contains", conn=conn)
    CL.ancestors("lin_ghost", "contains", conn=conn)
    CL.descendants("cyc_a", "contains", conn=conn)
    CL.closure_size("dia_a", "contains", conn=conn)
    CL.is_reachable("lin_a", "lin_d", "contains", conn=conn)

    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == n0
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == e0
    tables1 = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables1 == tables0, "no closure table may be materialised"
    conn.close()
    assert (db.stat().st_mtime_ns, db.stat().st_size) == before, \
        "traversal must leave the DB file byte-identical"

    # ...and when it opens its own connection, read-only is enforced by SQLite itself
    ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        ro.execute("INSERT INTO nodes (id, type, family) VALUES ('x','X','document')")
    except sqlite3.OperationalError as e:
        assert "readonly" in str(e).lower(), str(e)
    else:
        raise AssertionError("mode=ro must reject writes")
    finally:
        ro.close()


def case_12_planner_hints_are_pinned(conn) -> None:
    """Both planner hints are required for this traversal. Pin them.

    A fixture is too small to fail on a bad plan, so assert the hints themselves and
    verify the planner actually honours them on a real-shaped index set.
    """
    for name, sql, joined in (("_SQL_DOWN", CL._SQL_DOWN, "from_id"),
                              ("_SQL_UP", CL._SQL_UP, "to_id")):
        assert "CROSS JOIN" in sql, f"{name} lost the CROSS JOIN: walk must be the outer loop"
        assert "+e.predicate" in sql, \
            f"{name} lost the unary + : the planner will grab idx_edges_pred and crawl"
        plan = [r[3] for r in conn.execute(
            "EXPLAIN QUERY PLAN " + sql, ("lin_a", "contains", 12))]
        assert any(f"{joined}=?" in step for step in plan), \
            f"{name} must drive `edges` from {joined}, got: {plan}"
        assert not any("idx_edges_pred" in step for step in plan), \
            f"{name} fell back to the predicate index: {plan}"


def case_13_empty_and_unknown_seeds(conn) -> None:
    out = CL.descendants("no_such_node", "contains", conn=conn)
    assert out == [] and not out.truncated, repr(out)
    assert CL.closure_size("no_such_node", "contains", conn=conn) == 0
    assert not CL.is_reachable("no_such_node", "lin_a", "contains", conn=conn)
    leaf = CL.descendants("lin_ghost", "contains", conn=conn)
    assert leaf == [] and not leaf.truncated, "a leaf's closure is empty, not truncated"
    try:
        CL.closure_size("lin_a", "contains", conn=conn, bogus=1)
    except TypeError as e:
        assert "bogus" in str(e), str(e)
    else:
        raise AssertionError("an unrecognised kwarg must not silently unbound the walk")


# ── Runner ─────────────────────────────────────────────────────────────────

def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="brain_closure_test_"))
    try:
        db = _fixture(tmp)
        conn = sqlite3.connect(db)
        try:
            case_1_linear_chain_depths(conn)
            case_2_ancestors_invert_the_chain(conn)
            case_3_cycle_terminates_each_node_once(conn)
            case_4_non_transitive_predicate_refused(conn)
            case_5_max_depth_truncates_and_is_marked(conn)
            case_6_limit_honoured_and_marked(conn)
            case_7_is_reachable_both_ways(conn)
            case_8_diamond_yields_target_once_at_shortest_depth(conn)
            case_9_staged_proposals_are_included(conn)
            case_10_duplicate_edge_assertions_collapse(conn)
            case_12_planner_hints_are_pinned(conn)
            case_13_empty_and_unknown_seeds(conn)
        finally:
            conn.close()
        case_11_traversal_never_writes(tmp)
        print("PASS — closure: linear depths + shortest paths, ancestors invert, "
              "A->B->C->A and self-edges terminate with each node once, non-transitive "
              "AND unknown predicates refused with distinct ValueErrors, max_depth "
              "truncation marked (and NOT marked at a closed boundary), limit honoured "
              "and marked, is_reachable both directions incl. cycle self-reach, diamond "
              "yields the join once at its shortest depth, prop_* included, duplicate "
              "edge assertions collapse, planner hints pinned, traversal writes nothing (13 cases)")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run())
