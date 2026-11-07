"""Hermetic test for Phase 7 dedup + GC (no pytest).

Run:  python3 -m engine.tests.test_content_dedup
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from .. import config as C
from .. import db as DB


def _n(nid, title, path, ch, lm, status="active"):
    return {"id": nid, "type": "Source", "family": "document", "title": title,
            "path": path, "owner": "X", "status": status, "governance_tier": 0,
            "content_hash": ch, "summary": title, "body_excerpt": None, "centrality": 0.0,
            "created": None, "last_modified": lm, "last_reviewed": None, "metadata": {}}


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="brain_dedup_test_"))
    orig_db = C.BRAIN_DB
    C.BRAIN_DB = tmp / "brain.sqlite"
    try:
        from ..maintenance import content_dedup as CD

        conn = DB.connect(C.BRAIN_DB)
        DB.init_db(conn)

        # --- 7a fixture: same path, 3 ids (2 dated twins + 1 dateless stable id) ---
        P = "ALTAIR/notes/plan.md"
        DB.upsert_node(conn, _n("mem_20260518_plan_aaaaaa", "Plan", P, "h1", "2026-05-18"))
        DB.upsert_node(conn, _n("mem_20260601_plan_aaaaaa", "Plan", P, "h1", "2026-06-01"))
        DB.upsert_node(conn, _n("mem_plan_aaaaaa", "Plan", P, "h1", "2026-06-16"))  # dateless = canonical
        # an external node + a harvested-style edge pointing at the OLD dated id
        DB.upsert_node(conn, {**_n("agent_altair", "ALTAIR", "ALTAIR/id.md", None, "2026-01-01"),
                              "type": "Agent", "family": "concept"})
        DB.insert_edge(conn, {"from": "agent_altair", "to": "mem_20260518_plan_aaaaaa",
                              "predicate": "touched_by", "family": "temporal", "asserted_by": "T"})

        # --- 7b fixture: same content_hash + title across a live + an archive path ---
        DB.upsert_node(conn, _n("mem_doc_live", "Doctrine", "HQ/00_DOCTRINE/doctrine.md", "h2", "2026-06-10"))
        DB.upsert_node(conn, _n("mem_doc_arch", "Doctrine",
                                "HQ/99_ARCHIVE/old/doctrine.md", "h2", "2026-06-12"))
        DB.insert_edge(conn, {"from": "agent_altair", "to": "mem_doc_arch",
                              "predicate": "authored", "family": "structure", "asserted_by": "T"})
        # a same-hash-but-different-title pair that must NOT merge (boilerplate guard)
        DB.upsert_node(conn, _n("mem_init_a", "__init__.py", "a/__init__.py", "h3", "2026-06-01"))
        DB.upsert_node(conn, _n("mem_init_b", "__init__.py different", "b/__init__.py", "h3", "2026-06-01"))
        conn.commit()

        # === 7a ===
        r7a = CD.gc_same_path_twins(conn, verbose=False)
        assert r7a["losers_removed"] == 2, f"expected 2 removed, got {r7a}"
        # only the dateless canonical survives for that path
        survivors = [x[0] for x in conn.execute("SELECT id FROM nodes WHERE path=?", (P,)).fetchall()]
        assert survivors == ["mem_plan_aaaaaa"], f"wrong survivor: {survivors}"
        # the harvested edge was reparented from the deleted dated id to the canonical
        e = conn.execute("SELECT to_id FROM edges WHERE from_id='agent_altair' AND predicate='touched_by'").fetchone()
        assert e and e["to_id"] == "mem_plan_aaaaaa", f"edge not reparented: {dict(e) if e else None}"

        # === 7b dry-run then live ===
        dry = CD.collapse_content_twins(conn, dry_run=True, verbose=False)
        assert dry["merges"] == 1, f"dry-run expected 1 merge (doctrine), got {dry['merges']}"
        live = CD.collapse_content_twins(conn, dry_run=False, verbose=False)
        assert live["superseded"] == 1, f"expected 1 superseded, got {live}"
        # the ARCHIVE copy is the loser; the live copy is canonical
        arch = conn.execute("SELECT status FROM nodes WHERE id='mem_doc_arch'").fetchone()["status"]
        liv = conn.execute("SELECT status FROM nodes WHERE id='mem_doc_live'").fetchone()["status"]
        assert arch == "superseded" and liv == "active", f"wrong canonical: arch={arch} live={liv}"
        # superseded_by edge + reparented authored edge now point at the live canonical
        assert conn.execute("SELECT 1 FROM edges WHERE from_id='mem_doc_arch' AND predicate='superseded_by' "
                            "AND to_id='mem_doc_live'").fetchone(), "superseded_by edge missing"
        assert conn.execute("SELECT 1 FROM edges WHERE to_id='mem_doc_live' AND predicate='authored'").fetchone(), \
            "authored edge not reparented to canonical"
        # boilerplate guard: different titles never merged
        assert conn.execute("SELECT status FROM nodes WHERE id='mem_init_a'").fetchone()["status"] == "active"
        assert conn.execute("SELECT status FROM nodes WHERE id='mem_init_b'").fetchone()["status"] == "active"

        # === idempotent ===
        assert CD.gc_same_path_twins(conn, verbose=False)["losers_removed"] == 0
        assert CD.collapse_content_twins(conn, dry_run=False, verbose=False)["superseded"] == 0

        conn.close()
        print("PASS — content_dedup: 7a same-path GC (reparent+delete), "
              "7b cross-path supersede (live>archive, boilerplate-guarded), idempotent")
        return 0
    finally:
        C.BRAIN_DB = orig_db
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run())
