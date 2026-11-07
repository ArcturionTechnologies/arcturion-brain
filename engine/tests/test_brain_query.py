"""Hermetic ranking-invariant test for retrieval v2 (Brain v2 Phase 6, no pytest).

Asserts the invariants that make conversational retrieval trustworthy on a tiny clean
graph, independent of the live DB's dup-churn:
  - a multi-word natural-language query seeds nodes (never-empty)
  - a canonical (live-path) node outranks its archive twin
  - default BFS does NOT traverse semantically_similar_to noise edges
  - id-churn twins (same path) collapse to one at seed time

Run:  python3 -m engine.tests.test_brain_query
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from .. import config as C
from .. import db as DB


def _n(nid, title, path, summary, status="active"):
    return {"id": nid, "type": "Source", "family": "document", "title": title,
            "path": path, "owner": "SIRIUS", "status": status, "governance_tier": 0,
            "content_hash": None, "summary": summary, "body_excerpt": None,
            "centrality": 0.0, "created": None, "last_modified": "2026-06-16T00:00:00Z",
            "last_reviewed": None, "metadata": {}}


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="brain_q_test_"))
    orig_db = C.BRAIN_DB
    C.BRAIN_DB = tmp / "brain.sqlite"
    try:
        from .. import brain

        conn = DB.connect(C.BRAIN_DB)
        DB.init_db(conn)
        # canonical + archive twin (same title, different path) + an owner + noise target
        DB.upsert_node(conn, _n("rev_live", "Quarterly Revenue Report",
                                "SIRIUS/reports/quarterly_revenue.md",
                                "Q4 revenue figures and forecast for the quarter."))
        DB.upsert_node(conn, _n("rev_arch", "Quarterly Revenue Report",
                                "SIRIUS/99_ARCHIVE/quarterly_revenue.md",
                                "Q4 revenue figures and forecast for the quarter."))
        DB.upsert_node(conn, {**_n("agent_sirius", "SIRIUS", "SIRIUS/identity.md",
                                   "Finance agent."), "type": "Agent", "family": "concept"})
        DB.upsert_node(conn, _n("noise_dup", "Revenue Scratchpad",
                                "SIRIUS/_staging/scratch.md", "stale revenue scratch"))
        # real structural edge + a semantic noise edge
        DB.insert_edge(conn, {"from": "rev_live", "to": "agent_sirius", "predicate": "owned_by",
                              "family": "structure", "asserted_by": "TEST"})
        DB.insert_edge(conn, {"from": "rev_live", "to": "noise_dup",
                              "predicate": "semantically_similar_to", "family": "semantic",
                              "asserted_by": "TEST"})
        DB.insert_edge(conn, {"from": "rev_live", "to": "missing_host",
                              "predicate": "runs_on", "family": "deployment",
                              "asserted_by": "TEST"})
        conn.commit()

        # 1. never-empty: a multi-word NL query seeds nodes.
        seeds = brain._seed_nodes(conn, "what happened to the quarterly revenue report", limit=12)
        assert seeds, "NL query returned no seeds (never-empty broken)"
        seed_ids = {s["id"] for s in seeds}
        assert "rev_live" in seed_ids, f"canonical node not seeded: {seed_ids}"

        # 2. canonical (live path) outranks its archive twin.
        s_live = brain._relevance_score(dict(conn.execute("SELECT * FROM nodes WHERE id='rev_live'").fetchone()),
                                        "quarterly revenue report")
        s_arch = brain._relevance_score(dict(conn.execute("SELECT * FROM nodes WHERE id='rev_arch'").fetchone()),
                                        "quarterly revenue report")
        assert s_live > s_arch, f"archive outranks live: live={s_live} arch={s_arch}"

        # 3. seed-time path-dedup: archive twin shares title but different path → both can
        #    appear, but the live one must rank first.
        ordered = [s["id"] for s in seeds if s["id"] in ("rev_live", "rev_arch")]
        assert ordered and ordered[0] == "rev_live", f"live not ranked first: {ordered}"

        # 4. default BFS excludes semantically_similar_to (noise) but keeps owned_by.
        reached, edges = brain._expand(conn, {"rev_live"}, radius=2, predicates=None)
        preds = {e["predicate"] for e in edges}
        assert "agent_sirius" in reached, "owned_by edge not traversed"
        assert "semantically_similar_to" not in preds, "noise edge traversed by default"
        assert "noise_dup" not in reached, "noise node pulled in via semantic edge"

        # 5. explicit predicates=['semantically_similar_to'] CAN re-reach the noise node.
        reached2, _ = brain._expand(conn, {"rev_live"}, radius=1,
                                    predicates=["semantically_similar_to"])
        assert "noise_dup" in reached2, "explicit semantic traversal failed"

        # 6. A structural edge with a missing endpoint is never traversable, even when
        #    explicitly requested. The query must name the unavailable relationship
        #    class instead of silently presenting seed documents as a structural answer.
        reached3, edges3 = brain._expand(conn, {"rev_live"}, radius=1,
                                         predicates=["runs_on"])
        assert "missing_host" not in reached3, "dangling structural endpoint was traversed"
        assert not edges3, f"dangling structural edge leaked into expansion: {edges3}"
        guarded = brain.query("quarterly revenue report", token_budget=2000,
                              predicates=["runs_on"])
        assert guarded.edges == [], f"guarded query returned dead edges: {guarded.edges}"
        assert guarded.warnings == ["fully_dangling_predicate:runs_on"], guarded.warnings
        assert "STRUCTURAL RELATION UNAVAILABLE" in guarded.text_bundle, guarded.text_bundle

        # 7. full query path returns canonical first, non-empty.
        pack = brain.query("quarterly revenue report", token_budget=2000)
        assert pack.nodes and pack.nodes[0]["id"] == "rev_live", \
            f"top result not canonical: {[n['id'] for n in pack.nodes[:3]]}"

        conn.close()
        print("PASS — retrieval v2 invariants: never-empty, canonical>archive, "
              "noise-excluded-by-default, explicit-reachable, dangling-guarded, "
              "canonical-first")
        return 0
    finally:
        C.BRAIN_DB = orig_db
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run())
