"""Hermetic test: cross-path twin nodes (same title, different paths that ARCHIVE_MARKERS
do NOT catch) collapse to ONE in the query pack, keeping the highest-scored (canonical)
copy — while genuinely distinct titles are preserved.

Why: copies may share a title under distinct paths.
Path deduplication alone cannot prevent repeated results from crowding out context.

Run:  python3 -m engine.tests.test_twin_title_dedup
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

from .. import config as C
from .. import db as DB


def _n(nid, title, path, summary, centrality=0.0, status="active"):
    return {"id": nid, "type": "Source", "family": "document", "title": title,
            "path": path, "owner": "COVENANT", "status": status, "governance_tier": 0,
            "content_hash": None, "summary": summary, "body_excerpt": None,
            "centrality": centrality, "created": None,
            "last_modified": "2025-01-02T00:00:00Z", "last_reviewed": None, "metadata": {}}


def _norm(t):
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="brain_twin_test_"))
    orig_db = C.BRAIN_DB
    C.BRAIN_DB = tmp / "brain.sqlite"
    try:
        from .. import brain

        conn = DB.connect(C.BRAIN_DB)
        DB.init_db(conn)
        TITLE = "Credential Vault Constellation Sync"
        SUMM = "How the credential vault syncs across the constellation agents."
        # Canonical live copy — most connected (highest centrality).
        DB.upsert_node(conn, _n("cred_live", TITLE,
                                "examples/copy_3/example.md", SUMM, centrality=0.6))
        # Backup/migrated twins under DISTINCT paths that current ARCHIVE_MARKERS miss.
        DB.upsert_node(conn, _n("cred_t1", TITLE,
            "examples/copy_0/example.md", SUMM))
        DB.upsert_node(conn, _n("cred_t2", TITLE,
            "examples/copy_5/example.md", SUMM))
        DB.upsert_node(conn, _n("cred_t3", TITLE,
            "examples/copy_4/example.md", SUMM))
        DB.upsert_node(conn, _n("cred_t4", TITLE,
            "examples/copy_1/example.md", SUMM))
        # A genuinely distinct on-topic node (different title) — must survive dedup.
        DB.upsert_node(conn, _n("breaker", "Credential Circuit Breaker",
            "examples/copy_2/example.md",
            "The credential circuit breaker and automation vault commandment."))
        conn.commit()

        pack = brain.query("credential vault constellation sync", token_budget=2000)
        ids = [n["id"] for n in pack.nodes]
        titles = [_norm(n["title"]) for n in pack.nodes]

        # (a) no two returned nodes share a normalized non-empty title
        nonempty = [t for t in titles if t]
        assert len(nonempty) == len(set(nonempty)), f"duplicate titles in pack: {titles}"

        # (b) the single surviving twin is the canonical (highest-scored) copy
        twin_ids = [i for i in ids if i.startswith("cred_")]
        assert twin_ids == ["cred_live"], f"expected only cred_live among twins, got {twin_ids}"

        # (c) the distinct on-topic node still appears (dedup didn't over-collapse)
        assert "breaker" in ids, f"distinct node was dropped: {ids}"

        conn.close()
        print("PASS — cross-path twins collapse to the canonical copy; distinct titles preserved")
        return 0
    finally:
        C.BRAIN_DB = orig_db
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run())
