"""DB chokepoint regression for active-canon edge admission."""
from __future__ import annotations

import json
import sqlite3

from engine import db as DB


def test_unknown_and_retired_predicates_are_durably_quarantined(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "brain.sqlite")
    conn.row_factory = sqlite3.Row
    DB.init_db(conn)

    valid = {
        "from": "node_a", "to": "node_b", "predicate": "related_to",
        "family": "structure", "asserted_by": "TEST_PRODUCER",
    }
    assert DB.insert_edge(conn, valid) is True

    unknown = {
        "from": "node_a", "to": "node_c", "predicate": "http",
        "family": "transport", "asserted_by": "BROKEN_IMPORT",
        "metadata": {"source": "fixture"},
    }
    retired = {
        "from": "node_a", "to": "node_d", "predicate": "archived_to",
        "family": "lifecycle", "asserted_by": "LEGACY_PRODUCER",
    }
    assert DB.insert_edge(conn, unknown) is False
    assert DB.insert_edge(conn, unknown) is False
    assert DB.insert_edge(conn, retired) is False

    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 1
    rows = conn.execute(
        "SELECT predicate, asserted_by, reason, payload_json, times "
        "FROM edge_admission_quarantine ORDER BY predicate"
    ).fetchall()
    assert len(rows) == 2
    by_predicate = {row["predicate"]: row for row in rows}
    assert by_predicate["http"]["reason"] == "unknown_predicate"
    assert by_predicate["http"]["asserted_by"] == "BROKEN_IMPORT"
    assert by_predicate["http"]["times"] == 2
    assert json.loads(by_predicate["http"]["payload_json"])["metadata"] == {
        "source": "fixture"
    }
    assert by_predicate["archived_to"]["reason"] == "retired_predicate"
    conn.close()
