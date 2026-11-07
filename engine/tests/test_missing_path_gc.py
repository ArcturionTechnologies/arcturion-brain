"""Hermetic test for Phase 7d missing-path GC (no pytest).

Guards gc_missing_path against the brain-v2 residual: pre-reorg nodes whose file was
moved/deleted linger as status=active with no content-twin (7b skips) and a non-archive
path (7c skips). 7d sweeps them by on-disk absence, with three safety guards verified:

  1. a file-backed node whose path is GONE on disk is removed (+ its edges/FTS);
  2. a node whose file STILL EXISTS on disk survives, edges intact;
  3. a HARVESTED node (type=Session) with a synthetic/missing path is NEVER swept;
  4. dry_run=True (the default) mutates NOTHING but reports the would-remove count;
  5. the seen_paths clause protects a walked path even if os.exists would flap;
  6. idempotent — a second apply removes nothing;
  7. a node whose file exists only under UNIVERSE_ROOT (not DESKTOP_ROOT) survives — the GC
     must resolve against ALL configured roots, not just one (the bug that would have
     false-flagged every live HQ node, and later every live ~/.claude node).

Real temp files on disk back the "present" cases so os.exists is genuine; DESKTOP_ROOT and
UNIVERSE_ROOT are repointed at temp dirs for the duration to exercise multi-root resolution.

Run:  python3 -m engine.tests.test_missing_path_gc
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from .. import config as C
from .. import db as DB


def _n(nid, title, path, status="active", ntype="Source", family="document"):
    return {"id": nid, "type": ntype, "family": family, "title": title,
            "path": path, "owner": "X", "status": status, "governance_tier": 0,
            "content_hash": "h_" + nid, "summary": title, "body_excerpt": None,
            "centrality": 0.0, "created": None, "last_modified": "2026-06-16",
            "last_reviewed": None, "metadata": {}}


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="brain_misspath_test_"))
    uni_tmp = Path(tempfile.mkdtemp(prefix="brain_misspath_uni_"))
    orig_db = C.BRAIN_DB
    orig_desk = C.DESKTOP_ROOT
    orig_uni = C.UNIVERSE_ROOT
    C.BRAIN_DB = tmp / "brain.sqlite"
    C.DESKTOP_ROOT = tmp       # most prongs resolve under here
    C.UNIVERSE_ROOT = uni_tmp  # HQ-prong + a second root to prove multi-root resolution
    try:
        from ..maintenance.content_dedup import gc_missing_path

        conn = DB.connect(C.BRAIN_DB)
        DB.init_db(conn)

        # present_rel exists on disk; gone_rel does NOT; harvested has a synthetic path.
        present_rel = "ALTAIR/000 🤖 Altair/Memory/live.md"
        gone_rel = "06_MEMORY_SYSTEM/0601_AGENT_MEMORY/ALTAIR/old-prereorg-2026-05-01.md"
        protected_rel = "11_INFRASTRUCTURE/walked-but-flaky.md"  # gone on disk, but in seen
        session_rel = "ARCTURION_Brain/memory/0608_SESSION_DISTILLATIONS/x.jsonl"  # harvested

        # hq_rel exists ONLY under UNIVERSE_ROOT (not DESKTOP_ROOT) — the live-HQ case the
        # two-root fix added; proves a node resolving under a NON-Desktop root survives.
        hq_rel = "08_OPERATIONS/README.md"

        # Create the present file (and the protected one too, then we'll rely on seen_paths
        # — but make protected genuinely gone so the seen clause is what saves it).
        (tmp / present_rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp / present_rel).write_text("hi", encoding="utf-8")
        (uni_tmp / hq_rel).parent.mkdir(parents=True, exist_ok=True)
        (uni_tmp / hq_rel).write_text("hq", encoding="utf-8")

        DB.upsert_node(conn, _n("mem_present", "Live Note", present_rel))
        DB.upsert_node(conn, _n("mem_hq", "Live HQ Readme", hq_rel))
        DB.upsert_node(conn, _n("mem_gone", "Old Prereorg Note", gone_rel))
        DB.upsert_node(conn, _n("mem_protected", "Walked Flaky", protected_rel))
        DB.upsert_node(conn, _n("session_x", "Session X", session_rel,
                                ntype="Session", family="event"))
        DB.insert_edge(conn, {"from": "mem_present", "to": "mem_gone",
                              "predicate": "related_to", "family": "structure", "asserted_by": "T"})
        DB.insert_edge(conn, {"from": "mem_present", "to": "mem_protected",
                              "predicate": "related_to", "family": "structure", "asserted_by": "T"})
        conn.commit()

        seen_paths = {present_rel, protected_rel}

        def exists(nid):
            return conn.execute("SELECT 1 FROM nodes WHERE id=?", (nid,)).fetchone() is not None

        # --- dry-run mutates nothing ---
        dr = gc_missing_path(conn, seen_paths=seen_paths, dry_run=True, verbose=False)
        assert dr["removed"] == 0 and dr["would_remove"] == 1, f"dry-run should report 1, got {dr}"
        assert exists("mem_gone"), "dry-run must NOT delete"

        # --- apply ---
        r = gc_missing_path(conn, seen_paths=seen_paths, dry_run=False, verbose=False)
        assert r["removed"] == 1, f"expected exactly 1 removed (gone), got {r}"
        assert not exists("mem_gone"), "missing-on-disk node should be swept"
        assert exists("mem_present"), "present-on-disk node must survive"
        assert exists("mem_hq"), "node resolving under UNIVERSE_ROOT (not Desktop) must survive"
        assert exists("mem_protected"), "node in seen_paths must survive despite missing file"
        assert exists("session_x"), "harvested Session node must NEVER be swept by 7d"
        assert not conn.execute("SELECT 1 FROM edges WHERE to_id='mem_gone'").fetchone(), \
            "edges into swept node must be deleted"
        assert conn.execute("SELECT 1 FROM edges WHERE to_id='mem_protected'").fetchone(), \
            "edges into protected node must be untouched"

        # --- idempotent ---
        assert gc_missing_path(conn, seen_paths=seen_paths, dry_run=False, verbose=False)["removed"] == 0

        conn.close()
        print("PASS — missing-path GC: gone-on-disk removed (+edges), present & seen-protected "
              "& harvested survive, dry-run is no-op, idempotent")
        return 0
    finally:
        C.BRAIN_DB = orig_db
        C.DESKTOP_ROOT = orig_desk
        C.UNIVERSE_ROOT = orig_uni
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(uni_tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run())
