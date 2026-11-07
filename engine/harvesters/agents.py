"""Agent + Workspace nodes. One Workspace node per constellation agent root.

Each Workspace gets:
- a stable node id `workspace_<agent>`
- `owned_by` -> the agent (`agent_<lowercase>`)
- `contains` -> every file-backed node whose `path` starts with `<AGENT>/`
- `participates_in` -> ARCTURION_UNIVERSE concept node if cross-referenced
"""

from __future__ import annotations
import hashlib
import json
import time
from .. import config as C
from .. import db as DB
from ..util import now_iso

# Canonical agent labels — match ENTITY_ALIASES.md canonical ids.
AGENT_CANONICALS = {
    "ALTAIR": ("agent_altair", "ALTAIR", "Infrastructure"),
    "ARCTURUS": ("agent_arcturus", "ARCTURUS", "CEO / Strategist"),
    "ARCTURUS_CEO": ("agent_arcturus", "ARCTURUS_CEO", "CEO workspace (alias)"),
    "CASSIOPEIA": ("agent_cassiopeia", "CASSIOPEIA", "Web"),
    "CODEX": ("agent_codex", "CODEX", "Execution Engine"),
    "COVENANT": ("agent_covenant", "COVENANT", "Memory Governance"),
    "GEMINI": ("agent_gemini", "GEMINI", "Intelligence"),
    "IO": ("agent_io", "IO", "Digital Products"),
    "LILITH": ("agent_lilith", "LILITH", "Esoteric"),
    "LYRA": ("agent_lyra", "LYRA", "Creative"),
    "NEMESIS": ("agent_nemesis", "NEMESIS", "Legal"),
    "ORION": ("agent_orion", "ORION", "Research"),
    "POLARIS": ("agent_polaris", "POLARIS", "Personal OS"),
    "SIRIUS": ("agent_sirius", "SIRIUS", "Finance"),
    "VEGA": ("agent_vega", "VEGA", "Capital / Trading"),
}


def harvest(verbose: bool = True) -> dict:
    t0 = time.time()
    conn = DB.connect()
    asserted_at = now_iso()

    upserted_nodes = 0
    edges_added = 0
    source_facts: list[dict] = []

    for agent_dir, (canonical_id, label, role) in AGENT_CANONICALS.items():
        agent_root = C.DESKTOP_ROOT / agent_dir
        if not agent_root.exists():
            source_facts.append({"agent_dir": agent_dir, "exists": False})
            continue

        # 1) Agent node (one per canonical, idempotent)
        agent_node = {
            "id": canonical_id, "type": "Agent", "family": "agent",
            "title": label, "path": str(agent_root.relative_to(C.DESKTOP_ROOT)),
            "owner": "FOUNDER", "status": "active", "governance_tier": 2,
            "content_hash": None, "summary": f"Constellation agent: {role}",
            "body_excerpt": None, "centrality": 0.0,
            "created": None, "last_modified": now_iso(), "last_reviewed": None,
            "metadata": {"role": role, "workspace_dir": str(agent_root)},
        }
        DB.upsert_node(conn, agent_node)
        upserted_nodes += 1

        # 2) Workspace node (one per directory; aliases like ARCTURUS_CEO get their own)
        ws_id = f"workspace_{agent_dir.lower()}"
        ws_node = {
            "id": ws_id, "type": "Workspace", "family": "agent",
            "title": f"{agent_dir} workspace", "path": agent_dir,
            "owner": canonical_id, "status": "active", "governance_tier": 1,
            "content_hash": None,
            "summary": f"Local operating workspace for {label} ({role})",
            "body_excerpt": None, "centrality": 0.0,
            "created": None, "last_modified": now_iso(), "last_reviewed": None,
            "metadata": {"agent": canonical_id, "dir": agent_dir},
        }
        DB.upsert_node(conn, ws_node)
        upserted_nodes += 1

        # 3) Workspace owned_by Agent
        if DB.insert_edge(conn, {
            "from": ws_id, "to": canonical_id, "predicate": "owned_by", "family": "structure",
            "weight": 1.0, "confidence": 1.0, "asserted_by": "HARVEST_AGENT",
            "asserted_at": asserted_at, "evidence_span": "canonical-agent-map",
        }):
            edges_added += 1

        # 4) Workspace contains every file node whose path starts with this agent dir
        prefix = f"{agent_dir}/%"
        member_ids = []
        for r in conn.execute(
            "SELECT id FROM nodes WHERE path LIKE ?", (prefix,)
        ).fetchall():
            member_ids.append(r["id"])
            if DB.insert_edge(conn, {
                "from": ws_id, "to": r["id"], "predicate": "contains", "family": "structure",
                "weight": 0.6, "confidence": 0.9, "asserted_by": "HARVEST_AGENT",
                "asserted_at": asserted_at, "evidence_span": "path-prefix-match",
            }):
                edges_added += 1
            # And the agent owns those files via maintained_by
            if DB.insert_edge(conn, {
                "from": r["id"], "to": canonical_id, "predicate": "maintained_by",
                "family": "structure", "weight": 0.5, "confidence": 0.7,
                "asserted_by": "HARVEST_AGENT", "asserted_at": asserted_at,
                "evidence_span": "workspace-membership",
            }):
                edges_added += 1
        source_facts.append({
            "agent_dir": agent_dir, "canonical_id": canonical_id,
            "label": label, "role": role, "exists": True,
            "member_ids": sorted(member_ids),
        })

    conn.commit()
    conn.close()
    elapsed = time.time() - t0
    if verbose:
        print(f"[harvest:agents] {upserted_nodes} nodes, {edges_added} edges in {elapsed:.1f}s")
    source_digest = hashlib.sha256(json.dumps(
        source_facts, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return {"family": "agent", "status": "ok", "nodes": upserted_nodes, "edges": edges_added,
            "source_digest": source_digest,
            "elapsed_sec": round(elapsed, 2)}


if __name__ == "__main__":
    print(harvest())
