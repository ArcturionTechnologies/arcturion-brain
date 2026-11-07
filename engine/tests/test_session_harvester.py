"""Hermetic test for the Phase 4 session harvester (no pytest dependency).

Builds a throwaway DB + a fake Desktop with one session transcript, points
config.DESKTOP_ROOT/BRAIN_DB at the temp tree, runs the harvester, and asserts the
Session/Edit nodes + temporal edges land and resolve. Run:

    python3 -m engine.tests.test_session_harvester
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from .. import config as C
from .. import db as DB
from .. import validator as V


def _seed_nodes(conn, file_rel: str):
    """A minimal graph: two agent nodes + the file the session will edit."""
    base = {"family": "concept", "owner": "FOUNDER", "status": "active",
            "governance_tier": 0, "content_hash": None, "summary": "", "body_excerpt": None,
            "centrality": 0.0, "created": None, "last_modified": None, "last_reviewed": None,
            "metadata": {}}
    DB.upsert_node(conn, {**base, "id": "agent_altair", "type": "Agent",
                          "title": "ALTAIR", "path": "ALTAIR/000 Altair/identity.md"})
    DB.upsert_node(conn, {**base, "id": "agent_arcturus", "type": "Agent",
                          "title": "ARCTURUS", "path": "ARCTURUS/identity.md"})
    DB.upsert_node(conn, {**base, "id": "file_target", "type": "Module", "family": "code",
                          "title": "target.py", "path": file_rel})


def _write_transcript(path: Path, edited_abs: str):
    # `cwd` added 2026-08-05 (Leg 5). The harvester now refuses a transcript in which NO
    # working directory can be determined anywhere — absence of evidence must not read as
    # innocence, and the sibling 0608 route has failed closed on this since Leg 4. This
    # fixture omitted `cwd` only because nothing used to read it; every real transcript
    # carries one (measured: 1,730 of 1,730 on the live agent-raw shelf). The fail-closed
    # behaviour itself is asserted in test_pack_content, so this is a fixture made
    # REPRESENTATIVE, not a bar being lowered.
    cwd = str(Path(path).parents[3])
    lines = [
        {"type": "queue-operation", "operation": "enqueue", "cwd": cwd,
         "timestamp": "2026-06-16T10:00:00.000Z",
         "content": "Altair, refactor target.py and coordinate with ARCTURUS on the API."},
        {"type": "assistant", "timestamp": "2026-06-16T10:01:00.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "Pinging ARCTURUS about the shared schema."},
             {"type": "tool_use", "name": "Edit",
              "input": {"file_path": edited_abs}},
         ]}},
        {"type": "assistant", "timestamp": "2026-06-16T10:05:00.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "Write", "input": {"file_path": edited_abs}},
         ]}},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="brain_sess_test_"))
    # Repoint config at the temp tree.
    orig_desktop, orig_db = C.DESKTOP_ROOT, C.BRAIN_DB
    C.DESKTOP_ROOT = tmp
    C.BRAIN_DB = tmp / "brain.sqlite"
    try:
        file_rel = "ALTAIR/000 Altair/Memory/target.py"
        edited_abs = str(tmp / file_rel)

        conn = DB.connect(C.BRAIN_DB)
        DB.init_db(conn)
        _seed_nodes(conn, file_rel)
        conn.commit()

        transcript = tmp / "ALTAIR/000 Altair/Memory/Sessions/raw/2026-06-16_abcd1234-ef56-7890-aaaa-bbbbccccdddd.jsonl"
        _write_transcript(transcript, edited_abs)

        from ..harvesters import sessions as H
        result = H.harvest(conn, verbose=False)

        sid8 = "abcd1234"
        sess = f"session_altair_{sid8}"

        # 1. Session node exists, owned by the agent, with who+when.
        srow = conn.execute("SELECT type, owner, created FROM nodes WHERE id=?", (sess,)).fetchone()
        assert srow is not None, "Session node missing"
        assert srow["type"] == "Session" and srow["owner"] == "ALTAIR", dict(srow)
        assert srow["created"] == "2026-06-16T10:00:00.000Z", f"created={srow['created']}"

        # 2. An Edit node exists.
        ec = conn.execute("SELECT COUNT(*) FROM nodes WHERE type='Edit'").fetchone()[0]
        assert ec == 1, f"expected 1 Edit node, got {ec}"

        # 3. Temporal edges resolve to real node ids (not bare strings).
        def has_edge(frm, pred, to):
            return conn.execute(
                "SELECT 1 FROM edges WHERE from_id=? AND predicate=? AND to_id=?",
                (frm, pred, to)).fetchone() is not None

        assert has_edge(sess, "owned_by", "agent_altair"), "session owned_by agent missing"
        assert has_edge("file_target", "touched_by", sess), "file touched_by session missing"
        assert has_edge("file_target", "modified_by",
                        conn.execute("SELECT id FROM nodes WHERE type='Edit'").fetchone()[0]), \
            "file modified_by edit missing"
        assert has_edge("agent_arcturus", "discussed_in", sess), "cross-agent discussed_in missing"

        # 4. asserted_in_session is populated on the harvested edges.
        ais = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE asserted_by='HARVEST_SESSIONS' "
            "AND asserted_in_session=?", (sess,)).fetchone()[0]
        assert ais >= 4, f"expected >=4 session-tagged edges, got {ais}"

        # 5. Every harvested predicate is in the active canon (lockstep check).
        preds = [r[0] for r in conn.execute(
            "SELECT DISTINCT predicate FROM edges WHERE asserted_by='HARVEST_SESSIONS'").fetchall()]
        bad = [p for p in preds if p not in V.PREDICATE_CANON]
        assert not bad, f"predicates not in canon: {bad}"

        # 6. Idempotent: a second harvest adds no new edges.
        before = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        H.harvest(conn, verbose=False)
        after = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        assert before == after, f"non-idempotent: {before} -> {after}"

        conn.close()
        print(f"PASS — session harvester: {result}")
        print(f"       predicates exercised: {sorted(preds)}")
        return 0
    finally:
        C.DESKTOP_ROOT, C.BRAIN_DB = orig_desktop, orig_db
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run())
