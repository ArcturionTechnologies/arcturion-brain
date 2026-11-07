"""Hermetic tests for walkable, dangling, and structural edge health."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

from .. import coverage_check as CC
from .. import db as DB


# ── Fixture helpers ────────────────────────────────────────────────────────

def _mkdb(tmp: Path) -> sqlite3.Connection:
    conn = DB.connect(tmp / "brain.sqlite")
    DB.init_db(conn)
    conn.commit()
    return conn


def _node(conn: sqlite3.Connection, nid: str) -> None:
    conn.execute(
        "INSERT INTO nodes (id, type, family, title) VALUES (?, 'Source', 'document', ?)",
        (nid, nid),
    )


def _edge(conn: sqlite3.Connection, frm: str, to: str, pred: str) -> None:
    conn.execute(
        "INSERT INTO edges (from_id, to_id, predicate, family) VALUES (?, ?, ?, 'structure')",
        (frm, to, pred),
    )


_FAILS: list[str] = []


def _check(label: str, got, want) -> None:
    if got != want:
        _FAILS.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  ✗ {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ✓ {label}")


# ── Cases ──────────────────────────────────────────────────────────────────

def case_1_all_walkable(conn) -> None:
    """A graph whose every edge resolves at both ends is 100% walkable."""
    print("case 1: fully-walkable graph")
    for n in ("a", "b", "c"):
        _node(conn, n)
    _edge(conn, "a", "b", "contains")
    _edge(conn, "b", "c", "contains")
    conn.commit()

    h = DB.edge_health(conn)
    _check("edges_total", h["edges_total"], 2)
    _check("edges_walkable", h["edges_walkable"], 2)
    _check("edges_dangling", h["edges_dangling"], 0)
    _check("walkable_pct", h["walkable_pct"], 100.0)
    _check("dangling_by_predicate", h["dangling_by_predicate"], {})
    _check("fully_dangling_predicates", h["fully_dangling_predicates"], [])


def case_2_dangling_source_counts(conn) -> None:
    """An edge whose from_id names a node that does not exist is NOT walkable."""
    print("case 2: dangling source endpoint")
    _edge(conn, "ghost", "a", "part_of")
    conn.commit()

    h = DB.edge_health(conn)
    _check("edges_total", h["edges_total"], 3)
    _check("edges_walkable", h["edges_walkable"], 2)
    _check("edges_dangling", h["edges_dangling"], 1)
    _check("dangling_by_predicate['part_of']", h["dangling_by_predicate"].get("part_of"), 1)


def case_3_dangling_target_counts(conn) -> None:
    """An edge whose to_id names a node that does not exist is NOT walkable."""
    print("case 3: dangling target endpoint")
    _edge(conn, "a", "phantom", "part_of")
    conn.commit()

    h = DB.edge_health(conn)
    _check("edges_total", h["edges_total"], 4)
    _check("edges_walkable", h["edges_walkable"], 2)
    _check("edges_dangling", h["edges_dangling"], 2)
    _check("dangling_by_predicate['part_of']", h["dangling_by_predicate"].get("part_of"), 2)


def case_4_fully_dangling_predicate_is_named(conn) -> None:
    """A predicate with ZERO walkable edges is called out by name.

    A relationship count alone cannot show that every edge is unwalkable.
    """
    print("case 4: 100%-dangling predicate named")
    _edge(conn, "nowhere", "nohost", "runs_on")
    _edge(conn, "nowhere2", "nohost2", "runs_on")
    # Give part_of one surviving edge so it is damaged-but-alive — the contrast that
    # makes "fully dangling" mean something narrower than "has dangling edges".
    _edge(conn, "b", "a", "part_of")
    conn.commit()

    h = DB.edge_health(conn)
    _check("fully_dangling_predicates", h["fully_dangling_predicates"], ["runs_on"])
    _check("dangling_by_predicate['runs_on']", h["dangling_by_predicate"].get("runs_on"), 2)
    # part_of is damaged but alive — it must NOT be listed as fully dangling.
    _check("part_of not fully dangling", "part_of" in h["fully_dangling_predicates"], False)


def case_5_similarity_edges_excluded_from_structural(conn) -> None:
    """Machine-derived similarity edges cannot inflate the structural health number.

    THE REGRESSION GUARD. `semantically_similar_to` is regenerated continuously by the
    embedder and its fresh edges are always walkable. If they counted toward health, a
    busy embedder would mask arbitrary rot in the asserted graph — which is exactly the
    shape of the six-week blind spot. Structural health must ignore them.
    """
    print("case 5: derived similarity edges excluded from structural health")
    for n in ("s1", "s2", "s3", "s4"):
        _node(conn, n)
    for a, b in (("s1", "s2"), ("s2", "s3"), ("s3", "s4"), ("s4", "s1"),
                 ("s1", "s3"), ("s2", "s4")):
        _edge(conn, a, b, "semantically_similar_to")
    conn.commit()

    h = DB.edge_health(conn)
    # Overall is now flattered by the 6 pristine similarity edges — 69% looks survivable.
    _check("edges_total", h["edges_total"], 13)
    _check("edges_walkable", h["edges_walkable"], 9)
    _check("walkable_pct", h["walkable_pct"], 69.23)
    # ...but the structural view still shows the real damage: 3 of 7 asserted edges walk.
    _check("structural_total", h["structural_total"], 7)
    _check("structural_walkable", h["structural_walkable"], 3)
    _check("structural_walkable_pct", h["structural_walkable_pct"], 42.86)


def case_6_empty_graph_does_not_divide_by_zero(tmp: Path) -> None:
    """An empty edges table reports zeros and a null percentage, not a crash."""
    print("case 6: empty graph")
    conn = DB.connect(tmp / "empty.sqlite")
    conn.executescript(DB.SCHEMA)
    conn.commit()
    try:
        h = DB.edge_health(conn)
        _check("edges_total", h["edges_total"], 0)
        _check("edges_walkable", h["edges_walkable"], 0)
        _check("walkable_pct", h["walkable_pct"], None)
        _check("structural_walkable_pct", h["structural_walkable_pct"], None)
        _check("fully_dangling_predicates", h["fully_dangling_predicates"], [])
    finally:
        conn.close()


def case_7_coverage_check_fails_on_the_historic_rot() -> None:
    """A synthetic low-walkability graph must fail the coverage assertion."""
    print("case 7: coverage_check verdicts")
    historic = {"edges_total": 1000, "edges_walkable": 89, "edges_dangling": 911,
                "walkable_pct": 8.94, "structural_total": 1000, "structural_walkable": 89,
                "structural_walkable_pct": 8.91,
                "dangling_by_predicate": {"semantically_similar_to": 911},
                "fully_dangling_predicates": []}
    check, status, detail = CC.edge_walkability_result(historic)
    _check("check name", check, "edge-walkability")
    _check("historic rot verdict", status, CC.FAIL)
    _check("detail cites structural pct", "8.9" in detail, True)

    healthy = {"edges_total": 1000, "edges_walkable": 1000, "edges_dangling": 0,
               "walkable_pct": 100.0, "structural_total": 600, "structural_walkable": 600,
               "structural_walkable_pct": 100.0,
               "dangling_by_predicate": {}, "fully_dangling_predicates": []}
    _check("healthy verdict", CC.edge_walkability_result(healthy)[1], CC.PASS)

    # Healthy overall, but a whole relationship class is dead → WARN, named.
    dead_class = dict(healthy, fully_dangling_predicates=["runs_on", "depends_on"],
                      dangling_by_predicate={"runs_on": 13, "depends_on": 3},
                      edges_dangling=946)
    check, status, detail = CC.edge_walkability_result(dead_class)
    _check("dead-predicate verdict", status, CC.WARN)
    _check("detail names runs_on", "runs_on" in detail, True)

    empty = {"edges_total": 0, "edges_walkable": 0, "edges_dangling": 0, "walkable_pct": None,
             "structural_total": 0, "structural_walkable": 0, "structural_walkable_pct": None,
             "dangling_by_predicate": {}, "fully_dangling_predicates": []}
    _check("empty graph verdict", CC.edge_walkability_result(empty)[1], CC.WARN)


def case_8_status_surfaces_carry_the_walkable_number() -> None:
    """`brain.status()` and `/metrics` must expose edge health, not just a raw total."""
    print("case 8: status surfaces expose edge_health")
    import inspect
    from .. import brain as B
    from .. import metrics as M
    _check("brain.status calls edge_health",
           "edge_health" in inspect.getsource(B.status), True)
    _check("metrics.aggregate exposes graph_health",
           "graph_health" in inspect.getsource(M.aggregate), True)


def case_9_health_endpoint_and_warmup_share_one_payload(conn) -> None:
    """`/health` must report walkable edges — including on the pre-warmed first hit.

    Caught live: the handler was taught the walkable count but `_warmup()` pre-filled the
    same TTL cache with a plain `DB.stats()`, so for the first 60 s after every daemon
    restart `/health` silently served the old raw-total-only payload. Two writers to one
    cache is the drift; one shared builder is the fix, and this test is what stops the
    two from separating again.
    """
    print("case 9: /health payload is built in exactly one place")
    import inspect
    from .. import http_server as H

    payload = H._health_payload(conn)
    _check("payload has edges_walkable", "edges_walkable" in payload, True)
    _check("payload has edges_dangling", "edges_dangling" in payload, True)
    _check("payload has structural_walkable_pct", "structural_walkable_pct" in payload, True)
    _check("payload keeps raw edges too", "edges" in payload, True)
    # Exercise the warmup path against the same fixture census; never load models.
    import sys
    import types
    from unittest.mock import patch
    observed = []
    def snapshot():
        observed.append(H._health_payload(conn))
        return observed[-1]
    fake_vector = types.ModuleType(H.__package__ + ".vector_seed")
    fake_vector.prewarm = lambda: None
    with patch.object(H, "_health_snapshot", new=snapshot), \
            patch.object(H.B, "query", return_value={}), \
            patch.object(sys.modules[H.__package__], "vector_seed", fake_vector, create=True), \
            patch.dict(sys.modules, {H.__package__ + ".vector_seed": fake_vector}):
        H._warmup()
    _check("warmup obtains the complete canonical census", observed, [payload])


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="brain_edge_health_test_"))
    conn = _mkdb(tmp)
    try:
        case_1_all_walkable(conn)
        case_2_dangling_source_counts(conn)
        case_3_dangling_target_counts(conn)
        case_4_fully_dangling_predicate_is_named(conn)
        case_5_similarity_edges_excluded_from_structural(conn)
        case_6_empty_graph_does_not_divide_by_zero(tmp)
        case_7_coverage_check_fails_on_the_historic_rot()
        case_8_status_surfaces_carry_the_walkable_number()
        case_9_health_endpoint_and_warmup_share_one_payload(conn)
    finally:
        conn.close()

    print()
    if _FAILS:
        print(f"🛑 FAIL — {len(_FAILS)} assertion(s)")
        for f in _FAILS:
            print(f"   - {f}")
        return 1
    print("✅ PASS — edge-health signal")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
