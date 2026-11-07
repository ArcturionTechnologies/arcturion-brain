from __future__ import annotations

import json

from engine import db as DB
from engine.maintenance import validity_pass as V


def _conn(tmp_path):
    conn = DB.connect(tmp_path / "brain.sqlite")
    DB.init_db(conn)
    for nid in ("old", "new", "invalid"):
        conn.execute(
            "INSERT INTO nodes (id,type,family,title,status) VALUES (?,?,?,?,?)",
            (nid, "Test", "document", nid, "active"),
        )
    conn.commit()
    return conn


def test_supersession_stamping_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    path = tmp_path / "supersessions.jsonl"
    path.write_text(json.dumps({
        "id": "s1", "status": "active", "old_id": "old", "new_id": "new",
        "activated": "2026-09-05T10:00:00-04:00",
    }) + "\n")
    assert V.stamp_from_supersessions(conn, path, apply=False, verbose=False)["candidates"] == 1
    assert V.stamp_from_supersessions(conn, path, apply=True, verbose=False)["stamped"] == 1
    post = V.stamp_from_supersessions(conn, path, apply=False, verbose=False)
    assert post["candidates"] == 0
    assert post["already_current"] == 1
    conn.close()


def test_contradiction_stamping_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    path = tmp_path / "contradictions.jsonl"
    path.write_text(json.dumps({
        "id": "c1", "status": "resolved", "invalidates_id": "invalid",
        "resolved_at": "2026-09-05T10:00:00-04:00",
    }) + "\n")
    assert V.stamp_from_contradictions(conn, path, apply=False, verbose=False)["candidates"] == 1
    assert V.stamp_from_contradictions(conn, path, apply=True, verbose=False)["stamped"] == 1
    post = V.stamp_from_contradictions(conn, path, apply=False, verbose=False)
    assert post["candidates"] == 0
    assert post["already_current"] == 1
    conn.close()
