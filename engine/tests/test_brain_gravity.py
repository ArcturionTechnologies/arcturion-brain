"""Gravity audit + coverage-metrics test (Brain v2 Phase 9, no pytest).

Run:  python3 -m engine.tests.test_brain_gravity
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from .. import config as C
from .. import db as DB
from ..maintenance import gravity as G


def _n(nid, typ, fam, title, path, prong=None, status="active"):
    return {"id": nid, "type": typ, "family": fam, "title": title, "path": path,
            "owner": "X", "status": status, "governance_tier": 0, "content_hash": None,
            "summary": title, "body_excerpt": None, "centrality": 0.0, "created": None,
            "last_modified": "2026-06-16", "prong": prong, "metadata": {}}


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="brain_grav_test_"))
    try:
        db = tmp / "brain.sqlite"
        conn = DB.connect(db)
        DB.init_db(conn)
        # nodes across prongs + a capability + a session/edit
        DB.upsert_node(conn, _n("agent_altair", "Agent", "concept", "ALTAIR", "ALTAIR/id.md", "constellation"))
        DB.upsert_node(conn, _n("mem_a", "Source", "document", "Doc A", "ALTAIR/a.md", "constellation"))
        DB.upsert_node(conn, _n("cap_skill_x", "Capability", "capability", "x", "~/.claude/skills/x", "hq"))
        DB.upsert_node(conn, {**_n("cap_skill_x", "Capability", "capability", "x", "~/.claude/skills/x", "hq"),
                              "metadata": {"kind": "skill"}})
        DB.upsert_node(conn, _n("session_altair_aaa", "Session", "event", "S", "ALTAIR/s.jsonl", "constellation"))
        DB.upsert_node(conn, _n("edit_aaa_bbb", "Edit", "event", "E", "ALTAIR/a.md#x", "constellation"))
        # edges: a RESOLVED owned_by + a DANGLING owned_by + a temporal edge
        DB.insert_edge(conn, {"from": "mem_a", "to": "agent_altair", "predicate": "owned_by", "family": "structure", "asserted_by": "T"})
        DB.insert_edge(conn, {"from": "mem_a", "to": "NONEXISTENT_STRING", "predicate": "owned_by", "family": "structure", "asserted_by": "T"})
        DB.insert_edge(conn, {"from": "mem_a", "to": "session_altair_aaa", "predicate": "touched_by", "family": "temporal", "asserted_by": "T"})
        conn.commit()
        conn.close()

        # empty desktop_root → existence guard removes ALL agents (proves no phantom)
        report = G.audit(db_path=db, desktop_root=tmp, with_answer_quality=False)

        # 1. CODEX phantom is gone — config is the single source of truth.
        assert "CODEX" not in G.ACTIVE_AGENT_WORKSPACES, "CODEX still in agent list"
        assert tuple(G.ACTIVE_AGENT_WORKSPACES) == tuple(C.ACTIVE_CONSTELLATION_AGENTS), "gravity not sourcing config"
        assert len(G.ACTIVE_AGENT_WORKSPACES) == 12, f"expected 12 agents, got {len(G.ACTIVE_AGENT_WORKSPACES)}"

        # 2. Desktop-home guard: no agent folders under tmp → none audited (no phantoms).
        assert report["agent_graph_views"]["total"] == 0, report["agent_graph_views"]

        # 3. All coverage metric blocks present.
        cov = report["coverage_metrics"]
        for block in ("prong_coverage", "typed_edge_coverage", "temporal_coverage", "capability_coverage"):
            assert block in cov, f"missing coverage block: {block}"

        # 4. typed-edge coverage distinguishes resolved from dangling.
        ob = cov["typed_edge_coverage"]["by_predicate"]["owned_by"]
        assert ob["raw"] == 2 and ob["resolved"] == 1, f"owned_by coverage wrong: {ob}"

        # 5. temporal + capability counts populated.
        assert cov["temporal_coverage"]["session_nodes"] == 1
        assert cov["temporal_coverage"]["edit_nodes"] == 1
        assert cov["temporal_coverage"]["edges"]["touched_by"] == 1
        assert cov["capability_coverage"]["total"] == 1
        assert cov["capability_coverage"]["by_kind"].get("skill") == 1
        assert cov["prong_coverage"].get("constellation", 0) >= 4

        # 6. markdown renders the coverage section.
        md = G.to_markdown(report)
        assert "Coverage Metrics" in md and "Typed-edge coverage" in md

        # 7. the Answer Quality line renders from the CURRENT metric keys.
        #
        # This branch tested `top1_canonical_rate` — a key that stopped being produced when
        # _coverage_metrics moved to engine.retrieval_eval (2026-08-04). The condition was
        # therefore always false and the Answer Quality row silently disappeared from every
        # markdown report. A renamed metric that VANISHES is worse than one that reads wrong:
        # a wrong number gets questioned, an absent row gets overlooked. This asserts the
        # RENDERED OUTPUT, not the dict, because the dict was never the thing that broke.
        aq_report = dict(report)
        aq_report["coverage_metrics"] = dict(report["coverage_metrics"])
        aq_report["coverage_metrics"]["answer_quality"] = {
            "recall_at_5": 0.316, "top1_rate": 0.053, "guard_pass_rate": 0.429,
            "questions": 26,
            "by_class": {"paraphrase": 0.333, "common-token": 0.0, "contamination": 1.0},
        }
        aq_md = G.to_markdown(aq_report)
        assert "Answer quality" in aq_md, f"Answer Quality section vanished:\n{aq_md}"
        for token in ("0.316", "0.053", "0.429", "26 questions", "paraphrase 0.333"):
            assert token in aq_md, f"{token!r} missing from rendered markdown:\n{aq_md}"

        # …and an eval that could not run says so, rather than rendering nothing at all.
        err_report = dict(report)
        err_report["coverage_metrics"] = dict(report["coverage_metrics"])
        err_report["coverage_metrics"]["answer_quality"] = {"error": "FileNotFoundError: golden.jsonl"}
        err_md = G.to_markdown(err_report)
        assert "unavailable" in err_md and "golden.jsonl" in err_md, err_md

        print("PASS — gravity: CODEX phantom gone (config-sourced, 12 agents), Desktop-home "
              "guard, all coverage blocks present, typed-edge resolved/raw split correct")
        return 0
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run())
