"""Application + Service + Endpoint + Skill nodes.

Sources:
- `mdfind kind:com.apple.application` for installed GUI apps
- `brew list --formula -1` and `brew list --cask -1` for CLI tools + casks
- LaunchAgents already indexed; cross-link to their Programs
- MCP servers discovered from `~/.claude.json` mcpServers
- Claude Code skills under `~/.claude/skills/`
"""

from __future__ import annotations
import json
import hashlib
import subprocess
import time
from pathlib import Path
from .. import config as C
from .. import db as DB
from ..util import slugify, short_hash, now_iso


def _run(cmd: list[str], timeout: int = 30, required: bool = False) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, check=False)
        if required and proc.returncode != 0:
            raise RuntimeError(f"{cmd[0]} exited {proc.returncode}: {proc.stderr[:200]}")
        return proc.stdout
    except Exception:
        if required:
            raise
        return ""


def ensure_runtime_anchors(conn, asserted_at: str) -> int:
    """Create the stable infrastructure entities referenced by harvested edges."""
    anchors = (
        ("hw_macbook_m5", "Hardware", "hardware", "Founder MacBook M5",
         "Host machine for locally harvested applications"),
        ("tool_homebrew", "Tool", "application", "Homebrew",
         "Package manager that installs harvested formulae and casks"),
        ("tool_claude_code", "Tool", "application", "Claude Code",
         "Agent tool exposing harvested MCP servers and skills"),
    )
    inserted = 0
    for node_id, node_type, family, title, summary in anchors:
        if conn.execute("SELECT 1 FROM nodes WHERE id=?", (node_id,)).fetchone():
            continue
        DB.upsert_node(conn, {
            "id": node_id, "type": node_type, "family": family,
            "title": title, "path": None, "owner": "FOUNDER", "status": "configured",
            "governance_tier": 1, "content_hash": None, "summary": summary,
            "body_excerpt": None, "centrality": 0.0, "created": None,
            "last_modified": asserted_at, "last_reviewed": None,
            "metadata": {"kind": "runtime_anchor", "source": "applications_harvester",
                         "runtime_observed": False},
        })
        inserted += 1
    return inserted


def harvest_apps(conn, asserted_at: str) -> tuple[int, int, str]:
    """Apps via mdfind. Returns (nodes, edges)."""
    out = _run(["mdfind", "kMDItemContentType==com.apple.application-bundle"],
               timeout=30, required=True)
    nodes = edges = 0
    seen_paths = set()
    for line in out.splitlines():
        path = line.strip()
        if not path or not path.endswith(".app"):
            continue
        if path in seen_paths:
            continue
        seen_paths.add(path)
        name = Path(path).stem
        node_id = f"app_{slugify(name)}_{short_hash(path)}"
        bundle_id = None
        info_plist = Path(path) / "Contents" / "Info.plist"
        if info_plist.exists():
            bid_out = _run(["defaults", "read", str(info_plist.with_suffix("")), "CFBundleIdentifier"], timeout=2)
            bundle_id = bid_out.strip() or None
        DB.upsert_node(conn, {
            "id": node_id, "type": "Application", "family": "application",
            "title": name, "path": path, "owner": "FOUNDER", "status": "active",
            "governance_tier": 0, "content_hash": None,
            "summary": f"macOS application at {path}",
            "body_excerpt": None, "centrality": 0.0,
            "created": None, "last_modified": now_iso(), "last_reviewed": None,
            "metadata": {"bundle_id": bundle_id, "kind": "macos_app"},
        })
        nodes += 1
        # runs_on host
        if DB.insert_edge(conn, {
            "from": node_id, "to": "hw_macbook_m5", "predicate": "runs_on",
            "family": "runtime", "weight": 1.0, "confidence": 0.95,
            "asserted_by": "HARVEST_APP_MDFIND", "asserted_at": asserted_at,
            "evidence_span": "mdfind",
        }):
            edges += 1
    return nodes, edges, hashlib.sha256(json.dumps(
        sorted(seen_paths), separators=(",", ":")
    ).encode()).hexdigest()


def harvest_brew(conn, asserted_at: str) -> tuple[int, int, str]:
    nodes = edges = 0
    evidence = []
    for kind, args in (("formula", ["brew", "list", "--formula", "-1"]),
                       ("cask", ["brew", "list", "--cask", "-1"])):
        out = _run(args, timeout=30, required=True)
        evidence.append((kind, sorted(out.splitlines())))
        for name in (out.splitlines() if out else []):
            name = name.strip()
            if not name:
                continue
            node_id = f"cli_{kind}_{slugify(name)}"
            DB.upsert_node(conn, {
                "id": node_id, "type": "CLI_Tool" if kind == "formula" else "Application",
                "family": "application",
                "title": name, "path": f"/opt/homebrew/{kind}/{name}",
                "owner": "FOUNDER", "status": "active", "governance_tier": 0,
                "content_hash": None, "summary": f"Homebrew {kind}: {name}",
                "body_excerpt": None, "centrality": 0.0,
                "created": None, "last_modified": now_iso(), "last_reviewed": None,
                "metadata": {"installer": "brew", "kind": kind},
            })
            nodes += 1
            if DB.insert_edge(conn, {
                "from": "tool_homebrew", "to": node_id, "predicate": "installs",
                "family": "runtime", "weight": 1.0, "confidence": 1.0,
                "asserted_by": "HARVEST_APP_BREW", "asserted_at": asserted_at,
                "evidence_span": f"brew list --{kind}",
            }):
                edges += 1
    return nodes, edges, hashlib.sha256(
        json.dumps(sorted(evidence), sort_keys=True).encode()).hexdigest()


def harvest_mcp_servers(conn, asserted_at: str) -> tuple[int, int, str]:
    """MCP servers from ~/.claude.json."""
    nodes = edges = 0
    p = C.CLAUDE_ROOT / "settings.json"
    if not p.exists():
        return 0, 0, hashlib.sha256(b"absent:.claude.json").hexdigest()
    try:
        raw = p.read_bytes()
        data = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"cannot read MCP source {p}: {exc}") from exc
    servers = data.get("mcpServers") or {}
    for name, cfg in servers.items():
        node_id = f"mcp_server_{slugify(name)}"
        cmd = cfg.get("command") or "(remote)"
        DB.upsert_node(conn, {
            "id": node_id, "type": "Service", "family": "application",
            "title": f"MCP: {name}", "path": cmd, "owner": "FOUNDER",
            "status": "active", "governance_tier": 1, "content_hash": None,
            "summary": f"MCP server `{name}` exposed to Claude Code",
            "body_excerpt": None, "centrality": 0.0,
            "created": None, "last_modified": now_iso(), "last_reviewed": None,
            "metadata": {"command": cmd, "transport": cfg.get("type") or "stdio"},
        })
        nodes += 1
        if DB.insert_edge(conn, {
            "from": node_id, "to": "tool_claude_code", "predicate": "exposes",
            "family": "runtime", "weight": 1.0, "confidence": 1.0,
            "asserted_by": "HARVEST_APP_MCP", "asserted_at": asserted_at,
            "evidence_span": "~/.claude.json:mcpServers",
        }):
            edges += 1
    return nodes, edges, hashlib.sha256(raw).hexdigest()


def harvest_skills(conn, asserted_at: str) -> tuple[int, int, str]:
    nodes = edges = 0
    evidence = []
    root = C.CLAUDE_ROOT / "skills"
    if not root.exists():
        return 0, 0, hashlib.sha256(b"absent:.claude/skills").hexdigest()
    for skill_md in root.glob("*/SKILL.md"):
        name = skill_md.parent.name
        node_id = f"skill_{slugify(name)}"
        try:
            raw = skill_md.read_bytes()
            head = raw.decode("utf-8", errors="replace")[:600]
            evidence.append((str(skill_md), hashlib.sha256(raw).hexdigest()))
        except Exception as exc:
            raise RuntimeError(f"cannot read skill source {skill_md}: {exc}") from exc
        DB.upsert_node(conn, {
            "id": node_id, "type": "Skill", "family": "application",
            "title": f"Skill: {name}", "path": str(skill_md),
            "owner": "FOUNDER", "status": "active", "governance_tier": 1,
            "content_hash": None, "summary": head[:300],
            "body_excerpt": None, "centrality": 0.0,
            "created": None, "last_modified": now_iso(), "last_reviewed": None,
            "metadata": {"skill_name": name},
        })
        nodes += 1
        if DB.insert_edge(conn, {
            "from": node_id, "to": "tool_claude_code", "predicate": "exposes",
            "family": "runtime", "weight": 1.0, "confidence": 1.0,
            "asserted_by": "HARVEST_APP_SKILL", "asserted_at": asserted_at,
            "evidence_span": "~/.claude/skills/<name>/SKILL.md",
        }):
            edges += 1
    return nodes, edges, hashlib.sha256(
        json.dumps(sorted(evidence), sort_keys=True).encode()).hexdigest()


def harvest(verbose: bool = True) -> dict:
    t0 = time.time()
    conn = DB.connect()
    asserted_at = now_iso()
    try:
        anchor_n = ensure_runtime_anchors(conn, asserted_at)
        a_n, a_e, a_h = harvest_apps(conn, asserted_at)
        b_n, b_e, b_h = harvest_brew(conn, asserted_at)
        m_n, m_e, m_h = harvest_mcp_servers(conn, asserted_at)
        s_n, s_e, s_h = harvest_skills(conn, asserted_at)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    elapsed = time.time() - t0
    nodes = anchor_n + a_n + b_n + m_n + s_n
    edges = a_e + b_e + m_e + s_e
    if verbose:
        print(f"[harvest:apps] anchors={anchor_n} apps={a_n}+e{a_e} brew={b_n}+e{b_e} "
              f"mcp={m_n}+e{m_e} skills={s_n}+e{s_e} | total {nodes} nodes, {edges} edges in {elapsed:.1f}s")
    source_digest = hashlib.sha256("".join((a_h, b_h, m_h, s_h)).encode()).hexdigest()
    return {"family": "application", "status": "ok", "nodes": nodes, "edges": edges,
            "source_digest": source_digest,
            "elapsed_sec": round(elapsed, 2)}


if __name__ == "__main__":
    print(harvest())
