"""Hermetic test for the Phase 5 capability registry harvester (no pytest).

Builds a temp skills dir + a temp .claude.json, runs the harvester against a throwaway
DB, and asserts Capability nodes + instance_of/owned_by edges land and are FTS-findable.
Run:  python3 -m engine.tests.test_capability_registry
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from .. import config as C
from .. import db as DB


def _make_skill(skills_dir: Path, name: str, desc: str):
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n", encoding="utf-8")


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="brain_reg_test_"))
    orig_db = C.BRAIN_DB
    C.BRAIN_DB = tmp / "brain.sqlite"
    try:
        skills_dir = tmp / "skills"
        plugins_root = tmp / "plugins"  # empty
        _make_skill(skills_dir, "shopify-builder",
                    "Build and manage a Shopify store — products, themes, checkout.")
        _make_skill(skills_dir, "spotify", "Control Spotify playback universe-wide.")

        claude_json = tmp / ".claude.json"
        claude_json.write_text(json.dumps({
            "mcpServers": {"arcturion-brain": {}},   # top-level → UNIVERSE
            "projects": {
                "/Users/example/Desktop/VEGA": {"mcpServers": {"tradingview": {}}},
            },
        }), encoding="utf-8")

        conn = DB.connect(C.BRAIN_DB)
        DB.init_db(conn)
        # seed the agent node so the connector owned_by edge can resolve
        DB.upsert_node(conn, {"id": "agent_vega", "type": "Agent", "family": "concept",
                              "title": "VEGA", "path": "VEGA/identity.md", "owner": "FOUNDER",
                              "status": "active", "metadata": {}})
        conn.commit()

        from .. import registry_harvester as H
        result = H.harvest(conn, skills_dir=skills_dir, plugins_root=plugins_root,
                           claude_json=claude_json, registry_dir=tmp / "registry",
                           verbose=False)

        def node(nid):
            return conn.execute("SELECT type, family, owner, summary FROM nodes WHERE id=?",
                                (nid,)).fetchone()

        # 1. class nodes + capability nodes exist
        assert node("cap_class_skill") is not None, "skill class node missing"
        assert node("cap_class_connector") is not None, "connector class node missing"
        shop = node("cap_skill_shopify_builder")
        assert shop is not None and shop["type"] == "Capability", "shopify capability missing"
        tv = node("cap_connector_tradingview")
        assert tv is not None and tv["owner"] == "VEGA", f"connector owner wrong: {dict(tv) if tv else None}"

        def has_edge(frm, pred, to):
            return conn.execute("SELECT 1 FROM edges WHERE from_id=? AND predicate=? AND to_id=?",
                                (frm, pred, to)).fetchone() is not None

        # 2. instance_of class grouping + owned_by agent
        assert has_edge("cap_skill_shopify_builder", "instance_of", "cap_class_skill")
        assert has_edge("cap_connector_tradingview", "instance_of", "cap_class_connector")
        assert has_edge("cap_connector_tradingview", "owned_by", "agent_vega"), "owned_by missing"

        # 3. FTS-findable by purpose keyword (the IO scenario: "shopify")
        hits = [r[0] for r in conn.execute(
            "SELECT node_id FROM fts_nodes WHERE fts_nodes MATCH 'shopify'").fetchall()]
        assert "cap_skill_shopify_builder" in hits, f"shopify not FTS-findable: {hits}"

        # 4. manifest written
        man = (tmp / "registry" / "capabilities.jsonl")
        assert man.exists() and man.read_text().strip(), "capabilities.jsonl manifest missing"

        # 5. idempotent
        before = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        H.harvest(conn, skills_dir=skills_dir, plugins_root=plugins_root,
                  claude_json=claude_json, registry_dir=tmp / "registry", verbose=False)
        after = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        assert before == after, f"non-idempotent: {before} -> {after}"

        conn.close()
        print(f"PASS — capability registry: {result}")
        return 0
    finally:
        C.BRAIN_DB = orig_db
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run())
