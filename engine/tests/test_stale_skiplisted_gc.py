"""Hermetic test for Phase 7c stale skip-listed sweep (no pytest).

Guards three behaviors of gc_stale_skiplisted:
  1. an archive-path node NOT in this pass's seen set is removed (+ its edges/FTS),
  2. a Runtime-MAP node whose path matches a skip marker (ARCTURION_EXTERNAL_RUNTIME)
     but IS in seen_paths survives — the seen-set clause protects walked files,
  3. a live node survives, and a live node's inbound edge is untouched.

Run:  python3 -m engine.tests.test_stale_skiplisted_gc
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from .. import config as C
from .. import db as DB


def _n(nid, title, path, status="active"):
    return {"id": nid, "type": "Source", "family": "document", "title": title,
            "path": path, "owner": "X", "status": status, "governance_tier": 0,
            "content_hash": "h_" + nid, "summary": title, "body_excerpt": None,
            "centrality": 0.0, "created": None, "last_modified": "2026-06-16",
            "last_reviewed": None, "metadata": {}}


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="brain_skipgc_test_"))
    orig_db = C.BRAIN_DB
    C.BRAIN_DB = tmp / "brain.sqlite"
    try:
        from ..maintenance.content_dedup import gc_stale_skiplisted

        conn = DB.connect(C.BRAIN_DB)
        DB.init_db(conn)

        live = "ALTAIR/000 🤖 Altair/Memory/Decisions/voice-mode-architecture.md"
        archive = ("ALTAIR/99 🗄️ Archive/Reorg_Backups/_memory-pre-restructure_2026-06-09/"
                   "project_voice-mode-architecture.md")
        runtime = "ARCTURION_EXTERNAL_RUNTIME/SERVICE_CATALOG.md"  # skip-path but walked

        DB.upsert_node(conn, _n("mem_live", "Voice Mode Architecture", live))
        DB.upsert_node(conn, _n("mem_arch", "Voice Mode Architecture", archive))
        DB.upsert_node(conn, _n("mem_runtime", "Service Catalog", runtime))
        DB.upsert_node(conn, {**_n("agent_altair", "ALTAIR", "ALTAIR/id.md"),
                              "type": "Agent", "family": "concept"})
        # edges: one into the doomed archive node, one into the live node
        DB.insert_edge(conn, {"from": "agent_altair", "to": "mem_arch",
                              "predicate": "authored", "family": "structure", "asserted_by": "T"})
        DB.insert_edge(conn, {"from": "agent_altair", "to": "mem_live",
                              "predicate": "authored", "family": "structure", "asserted_by": "T"})
        # Vectors for both. `vector_seed` reads `embeddings` with NO join back to
        # `nodes`, so a vector left behind by a sweep still seeds a candidate id that
        # resolves to nothing. Harmless while these passes were gated behind `--full`
        # (which nothing ran); a nightly leak once the checks were enabled.
        for nid in ("mem_live", "mem_arch"):
            conn.execute("INSERT OR REPLACE INTO embeddings (node_id, dim, vector) "
                         "VALUES (?,?,?)", (nid, C.EMBED_DIM, b"\x00" * 4))
        conn.commit()

        # This pass walked the live + runtime files (NOT the archive copy).
        seen_paths = {live, runtime, "ALTAIR/id.md"}
        r = gc_stale_skiplisted(conn, seen_paths, verbose=False)

        assert r["removed"] == 1, f"expected exactly 1 removed (archive), got {r}"

        def exists(nid):
            return conn.execute("SELECT 1 FROM nodes WHERE id=?", (nid,)).fetchone() is not None

        assert not exists("mem_arch"), "archive node should be swept"
        assert exists("mem_live"), "live node must survive"
        assert exists("mem_runtime"), "runtime-MAP node (in seen_paths) must survive despite skip path"
        assert exists("agent_altair"), "agent anchor must survive"
        # archive node's edge gone; live node's edge intact
        assert not conn.execute("SELECT 1 FROM edges WHERE to_id='mem_arch'").fetchone(), \
            "edges into swept node must be deleted"
        assert conn.execute("SELECT 1 FROM edges WHERE to_id='mem_live'").fetchone(), \
            "edges into live node must be untouched"
        # No orphan vector left pointing at the swept node, and the survivor keeps its own.
        assert not conn.execute("SELECT 1 FROM embeddings WHERE node_id='mem_arch'").fetchone(), \
            "swept node's embedding must be deleted — vector_seed does not join to nodes"
        assert conn.execute("SELECT 1 FROM embeddings WHERE node_id='mem_live'").fetchone(), \
            "live node's embedding must survive"
        assert conn.execute(
            "SELECT COUNT(*) FROM embeddings e LEFT JOIN nodes n ON n.id = e.node_id "
            "WHERE n.id IS NULL").fetchone()[0] == 0, "sweep left orphan vectors"

        # idempotent — second run with same seen set removes nothing
        assert gc_stale_skiplisted(conn, seen_paths, verbose=False)["removed"] == 0

        conn.close()
        print("PASS — stale skip-listed sweep: archive removed (+edges), runtime-MAP & live "
              "protected by seen_paths, idempotent")
        return 0
    finally:
        C.BRAIN_DB = orig_db
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run())
