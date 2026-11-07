from __future__ import annotations

import sqlite3

from engine import db as DB
from engine.maintenance import semantic_repair as S


def _conn(tmp_path):
    conn = DB.connect(tmp_path / "brain.sqlite")
    DB.init_db(conn)
    for nid, family, title in (
        ("doc_source", "document", "Source"),
        ("doc_altair", "document", "ALTAIR"),
        ("agent_altair", "agent", "ALTAIR"),
        ("agent_gemini", "agent", "GEMINI"),
        ("concept_capability", "concept", "Capability"),
    ):
        conn.execute(
            "INSERT INTO nodes (id,type,family,title,status) VALUES (?,?,?,?,?)",
            (nid, "Test", family, title, "active"),
        )
    conn.commit()
    return conn


def _raw_edge(conn, from_id, to_id, predicate, asserted_by="TEST"):
    conn.execute(
        """INSERT INTO edges
           (from_id,to_id,predicate,family,weight,confidence,asserted_by,metadata)
           VALUES (?,?,?,'structure',1.0,1.0,?,'{}')""",
        (from_id, to_id, predicate, asserted_by),
    )
    conn.commit()


def test_noncanonical_edges_move_to_quarantine(tmp_path):
    conn = _conn(tmp_path)
    _raw_edge(conn, "doc_source", "doc_altair", "http")
    assert S.quarantine_noncanonical(conn, apply=False)["candidates"] == 1
    result = S.quarantine_noncanonical(conn, apply=True)
    assert result["quarantined"] == 1
    assert S.noncanonical_edges(conn) == []
    row = conn.execute("SELECT reason,payload_json FROM edge_admission_quarantine").fetchone()
    assert row["reason"] == "legacy_unknown_predicate"
    assert '"predicate": "http"' in row["payload_json"]
    conn.close()


def test_agent_endpoint_repair_and_unrepairable_quarantine(tmp_path):
    conn = _conn(tmp_path)
    _raw_edge(conn, "doc_source", "doc_altair", "owned_by", "FORWARD")
    _raw_edge(conn, "doc_altair", "doc_source", "owns", "INVERSE")
    _raw_edge(conn, "concept_capability", "agent_gemini", "superseded_by", "BAD_MERGE")

    plan = S.repair_agent_endpoints(conn, apply=False)
    assert plan["candidates"] == 3
    assert plan["correctable"] == 2
    assert plan["quarantine_only"] == 1

    result = S.repair_agent_endpoints(conn, apply=True)
    assert result["corrected_edges_inserted"] == 2
    assert result["quarantined"] == 3
    assert conn.execute(
        "SELECT 1 FROM edges WHERE from_id='doc_source' AND to_id='agent_altair' "
        "AND predicate='owned_by'"
    ).fetchone()
    assert conn.execute(
        "SELECT 1 FROM edges WHERE from_id='agent_altair' AND to_id='doc_source' "
        "AND predicate='owns'"
    ).fetchone()
    assert S.domain_range_violations(conn) == []
    assert S.verify(conn)["ok"] is True
    conn.close()


def test_snapshot_is_verified_and_refuses_overwrite(tmp_path):
    conn = _conn(tmp_path)
    destination = tmp_path / "snapshot.sqlite"
    snap = S.snapshot_database(conn, destination)
    assert snap["quick_check"] == "ok"
    assert snap["integrity_check"] == "ok"
    assert len(snap["sha256"]) == 64
    try:
        S.snapshot_database(conn, destination)
    except FileExistsError:
        pass
    else:
        raise AssertionError("snapshot overwrite was not refused")
    conn.close()
