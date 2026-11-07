"""Capability registry harvester (Brain v2 Phase 5).

Indexes every skill / MCP connector an agent can lazy-load as a queryable
`Capability` node — name, purpose (when-to-use), where-it-lives (a SKILL.md path or
`mcp://<server>` id), and owning agent. Reads the live sources; never copies them.

Sources (all on disk, read at harvest time):
  - user skills   ~/.claude/skills/*/SKILL.md
  - plugin skills ~/.claude/plugins/**/SKILL.md
  - connectors    ~/.claude.json  (top-level + per-project `mcpServers`)

Nodes (family `capability`):
  cap_class_skill / cap_class_connector  — class grouping nodes (type Concept)
  cap_skill_<name>                       — one loadable skill
  cap_connector_<name>                   — one MCP connector

Edges (asserted_by='HARVEST_REGISTRY'):
  capability --instance_of--> cap_class_<kind>   (family semantic)
  capability --owned_by--> agent_<x>             (family structure; agent-specific only)

Idempotent (upsert + INSERT OR IGNORE). Reversible:
  DELETE FROM edges WHERE asserted_by='HARVEST_REGISTRY';
  DELETE FROM nodes WHERE type='Capability' OR id LIKE 'cap_class_%';

Run standalone:  python3 -m engine.registry_harvester
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import config as C
from . import db as DB
from . import resolver as RES
from .util import slugify, now_iso

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def _parse_skill_md(path: Path) -> tuple[str | None, str]:
    """Return (name, description) from a SKILL.md frontmatter."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None, ""
    m = _FM_RE.match(text)
    if not m:
        return path.parent.name, ""
    try:
        import yaml
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        fm = {}
    if not isinstance(fm, dict):
        fm = {}
    name = (fm.get("name") or path.parent.name or "").strip()
    desc = (fm.get("description") or "").strip()
    return name or None, desc


def _iter_skill_files(skills_dir: Path, plugins_root: Path):
    """Yield (name, description, where, scope) for each discoverable skill."""
    for p in sorted(skills_dir.glob("*/SKILL.md")):
        name, desc = _parse_skill_md(p)
        if name:
            yield name, desc, str(p), "user"
    if plugins_root.exists():
        for p in sorted(plugins_root.rglob("SKILL.md")):
            name, desc = _parse_skill_md(p)
            if name:
                yield name, desc, str(p), "plugin"


def _collect_connectors(claude_json: Path) -> dict[str, set[str]]:
    """name -> set of project basenames that declare the server ('UNIVERSE' for top-level)."""
    out: dict[str, set[str]] = {}
    try:
        d = json.loads(claude_json.read_text(encoding="utf-8"))
    except Exception:
        return out
    for name in (d.get("mcpServers") or {}):
        out.setdefault(name, set()).add("UNIVERSE")
    for path, cfg in (d.get("projects") or {}).items():
        base = os.path.basename(path.rstrip("/"))
        for name in (cfg.get("mcpServers") or {}):
            out.setdefault(name, set()).add(base)
    return out


def _class_node(conn, cid: str, title: str, summary: str):
    DB.upsert_node(conn, {
        "id": cid, "type": "Concept", "family": "capability",
        "title": title, "path": f"registry://{cid}", "owner": "UNIVERSE",
        "status": "active", "governance_tier": 0, "content_hash": None,
        "summary": summary, "body_excerpt": None, "centrality": 0.0,
        "created": None, "last_modified": now_iso(), "last_reviewed": None,
        "metadata": {"registry_class": True},
    })


def harvest(conn=None, *, skills_dir: Path | None = None, plugins_root: Path | None = None,
            claude_json: Path | None = None, registry_dir: Path | None = None,
            verbose: bool = True) -> dict:
    t0 = time.time()
    own = conn is None
    if own:
        conn = DB.connect()
    DB.init_db(conn)

    home = C.WORK_ROOT
    skills_dir = skills_dir or home / ".claude" / "skills"
    plugins_root = plugins_root or home / ".claude" / "plugins"
    claude_json = claude_json or home / ".claude.json"
    registry_dir = registry_dir or (C.BRAIN_ROOT / "registry")

    R = RES.build_resolution_index(conn)
    asserted_at = now_iso()

    _class_node(conn, "cap_class_skill", "Skill", "Class of loadable Claude Code skills.")
    _class_node(conn, "cap_class_connector", "MCP Connector",
                "Class of MCP servers (connectors) agents can call.")

    caps = edges = 0
    manifest: list[dict] = []
    seen_ids: set[str] = set()

    def _emit_cap(cid, kind, name, summary, where, owner, projects=None):
        nonlocal caps, edges
        DB.upsert_node(conn, {
            "id": cid, "type": "Capability", "family": "capability",
            "title": name, "path": where, "owner": owner,
            "status": "active", "governance_tier": 0, "content_hash": None,
            "summary": (summary or f"{kind}: {name}")[:400],
            "body_excerpt": None, "centrality": 0.0,
            "created": None, "last_modified": now_iso(), "last_reviewed": None,
            "metadata": {"kind": kind, "name": name, "where": where,
                         "projects": sorted(projects) if projects else None},
        })
        if cid not in seen_ids:
            caps += 1
            seen_ids.add(cid)
        cls = f"cap_class_{kind}"
        if DB.insert_edge(conn, {
            "from": cid, "to": cls, "predicate": "instance_of", "family": "semantic",
            "weight": 1.0, "confidence": 1.0, "asserted_by": "HARVEST_REGISTRY",
            "asserted_at": asserted_at, "evidence_span": where,
        }):
            edges += 1
        manifest.append({"id": cid, "kind": kind, "name": name, "owner": owner,
                         "where": where, "summary": (summary or "")[:200]})

    # --- skills ---
    for name, desc, where, scope in _iter_skill_files(skills_dir, plugins_root):
        cid = f"cap_skill_{slugify(name)}"
        try:
            where_rel = os.path.relpath(where, str(home))
            where_disp = f"~/{where_rel}"
        except ValueError:
            where_disp = where
        _emit_cap(cid, "skill", name, desc, where_disp, "UNIVERSE")

    # --- connectors ---
    for name, projects in _collect_connectors(claude_json).items():
        cid = f"cap_connector_{slugify(name)}"
        agent_projects = sorted(p for p in projects if p != "UNIVERSE")
        owner = "UNIVERSE" if "UNIVERSE" in projects or not agent_projects else agent_projects[0]
        _emit_cap(cid, "connector", name, f"MCP connector '{name}'",
                  f"mcp://{name}", owner, projects=projects)
        # owned_by edges for any agent-specific project
        for base in agent_projects:
            aid = R.resolve(base)
            if aid and aid.startswith("agent_"):
                if DB.insert_edge(conn, {
                    "from": cid, "to": aid, "predicate": "owned_by", "family": "structure",
                    "weight": 1.0, "confidence": 0.9, "asserted_by": "HARVEST_REGISTRY",
                    "asserted_at": asserted_at, "evidence_span": f".claude.json:{base}",
                }):
                    edges += 1

    # --- agent ownership from per-agent skills.md / tools.md loadouts ---
    # Each agent's `## 🎯 <AGENT>'s loadout` / `tools` section lists the
    # capabilities it owns or primarily uses. We link the matching harvested
    # capability nodes (skill or connector) to the owning agent via owned_by.
    # Floor capabilities live in a separate `🌌 Universe floor` section and are
    # deliberately NOT parsed — those stay universal (no ownership edge).
    desktop = home / "Desktop"
    loadout_re = re.compile(r"##\s*🎯[^\n]*\n(.*?)(?=\n##\s|\Z)", re.DOTALL)
    btick_re = re.compile(r"`([^`]+)`")
    mcp_re = re.compile(r"mcp__([A-Za-z0-9_-]+?)__")

    def _frontmatter_owner(text: str) -> str | None:
        m = _FM_RE.match(text)
        if not m:
            return None
        try:
            import yaml
            fm = yaml.safe_load(m.group(1)) or {}
        except Exception:
            return None
        if not isinstance(fm, dict):
            return None
        o = str(fm.get("owner") or fm.get("agent") or "").strip()
        return o or None

    cap_files = sorted(desktop.glob("*/000 */Skills/skills.md")) + \
                sorted(desktop.glob("*/000 */Tools/tools.md"))
    own_edges = 0
    for cap_file in cap_files:
        try:
            text = cap_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        owner = _frontmatter_owner(text)
        if not owner:
            continue
        aid = f"agent_{slugify(owner)}"
        secs = loadout_re.findall(text)
        if not secs:
            continue
        cand_ids: set[str] = set()
        for tok in btick_re.findall("\n".join(secs)):
            tok = tok.strip()
            for mm in mcp_re.findall(tok):           # connector form: mcp__name__
                cand_ids.add(f"cap_connector_{slugify(mm)}")
            base = tok.split(":")[-1].strip().rstrip("*").strip("/")  # skill form
            if base and "/" not in base and " " not in base:
                cand_ids.add(f"cap_skill_{slugify(base)}")
                cand_ids.add(f"cap_connector_{slugify(base)}")
        for cid in sorted(cand_ids):
            if cid in seen_ids:                       # only link real harvested caps
                if DB.insert_edge(conn, {
                    "from": cid, "to": aid, "predicate": "owned_by", "family": "structure",
                    "weight": 1.0, "confidence": 0.85, "asserted_by": "HARVEST_REGISTRY",
                    "asserted_at": asserted_at, "evidence_span": f"{owner}:{cap_file.name}",
                }):
                    edges += 1
                    own_edges += 1

    conn.commit()

    # --- manifest snapshot (debug artifact; live store is brain.sqlite) ---
    try:
        registry_dir.mkdir(parents=True, exist_ok=True)
        with (registry_dir / "capabilities.jsonl").open("w", encoding="utf-8") as fh:
            for row in sorted(manifest, key=lambda r: (r["kind"], r["name"].lower())):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass

    elapsed = time.time() - t0
    if own:
        conn.close()
    if verbose:
        print(f"[harvest:registry] {caps} capabilities, {edges} edges "
              f"({own_edges} agent-loadout owned_by) in {elapsed:.1f}s")
    return {"family": "capability", "nodes": caps, "edges": edges,
            "loadout_owned_by": own_edges, "elapsed_sec": round(elapsed, 2)}


if __name__ == "__main__":
    print(harvest())
