"""Brain Gravity v1 read-only audit and proposal helpers.

This module reports how well the Brain connects the governed prongs without
mutating graph storage. Proposal helpers emit validator-compatible edge dicts
for review/staging only; callers decide where to write them after policy checks.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .. import config as C
from .. import db as DB
from .. import validator as V

# Brain v2 Phase 9: single source of truth (config), not a hardcoded list — this is what
# retired the CODEX phantom that used to reappear in every audit.
ACTIVE_AGENT_WORKSPACES = C.ACTIVE_CONSTELLATION_AGENTS

UNIVERSE_PREFIXES = (
    "00_FOUNDER", "01_IDENTITY", "02_GOVERNANCE", "03_BRANDS",
    "04_CONSTELLATION_AGENTS", "05_DEPARTMENTS", "06_MEMORY_SYSTEM",
    "07_PROJECTS", "08_OPERATIONS", "09_FINANCE", "09_ASSETS",
    "10_LEGAL", "10_REVENUE_ENGINE", "11_INFRASTRUCTURE", "12_LOGS",
    "90_COMPATIBILITY", "99_ARCHIVE", "AGENTS.md", "CLAUDE.md",
    "GEMINI.md", "ANTIGRAVITY.md", "Founder.md", "MAP.md", "SKILLS.md",
    "TOOLS.md", "SOULS.md",
)

GRAVITY_PREDICATES = (
    "routes_to", "depends_on", "derived_from", "source_of",
    "requires_refresh", "implemented_by", "validated_by",
    "requires_approval_from", "checked_by_eval",
)

DIRECTION_RULES: dict[str, dict[str, str]] = {
    "atlas_projection": {
        "predicate": "derived_from",
        "family": "evidence",
        "description": "ATLAS projection points back to Universe truth.",
    },
    "universe_projection_source": {
        "predicate": "source_of",
        "family": "evidence",
        "description": "Universe truth points to its human-facing projection.",
    },
    "task_route": {
        "predicate": "routes_to",
        "family": "operational",
        "description": "Task or domain routes to the responsible agent or prong.",
    },
    "runtime_dependency": {
        "predicate": "depends_on",
        "family": "reasoning",
        "description": "Runtime artifact depends on canonical owner or source.",
    },
    "refresh_needed": {
        "predicate": "requires_refresh",
        "family": "self_improvement",
        "description": "View or projection needs a refresh owner.",
    },
    "implementation_link": {
        "predicate": "implemented_by",
        "family": "operational",
        "description": "Policy or design is implemented by a script/module.",
    },
    "validation_link": {
        "predicate": "validated_by",
        "family": "operational",
        "description": "Target is validated by an audit, test, or report.",
    },
    "approval_boundary": {
        "predicate": "requires_approval_from",
        "family": "operational",
        "description": "Risky surface routes to Founder or COVENANT approval.",
    },
}

_CANDIDATE_PREDICATES = (
    {
        "predicate": "projects_to",
        "inverse": "projected_from",
        "status": "candidate_only",
        "rationale": "First-class Universe-to-ATLAS projection semantics.",
    },
    {
        "predicate": "syncs_with",
        "inverse": None,
        "status": "candidate_only",
        "rationale": "Expected drift-sensitive alignment between canon and views.",
    },
)


def candidate_predicates() -> list[dict[str, str | None]]:
    return [dict(item) for item in _CANDIDATE_PREDICATES]


def proposal_for_rule(
    rule: str,
    from_id: str,
    to_id: str,
    *,
    asserted_by: str = "ALTAIR",
    evidence_span: str | None = None,
    confidence: float = 0.95,
) -> dict[str, Any]:
    if rule not in DIRECTION_RULES:
        raise KeyError(f"unknown Brain Gravity direction rule: {rule}")
    spec = DIRECTION_RULES[rule]
    predicate = spec["predicate"]
    if predicate not in V.ACTIVE_PREDICATE_CANON:
        raise ValueError(f"predicate is not active canon: {predicate}")
    return {
        "from": from_id,
        "to": to_id,
        "predicate": predicate,
        "family": spec["family"],
        "weight": 1.0,
        "confidence": confidence,
        "asserted_by": asserted_by,
        "evidence_span": evidence_span or f"brain_gravity_v1:{rule}",
        "metadata": {"rule": rule, "description": spec["description"]},
    }


def seed_proposals() -> list[dict[str, Any]]:
    pairs = [
        ("atlas_projection", "atlas_projection_surface", "arcturion_universe_source_truth"),
        ("universe_projection_source", "arcturion_universe_source_truth", "atlas_projection_surface"),
        ("task_route", "brain_gravity_domain", "ALTAIR"),
        ("runtime_dependency", "arcturion_external_runtime", "arcturion_universe"),
        ("refresh_needed", "agent_graph_index_views", "COVENANT"),
        ("implementation_link", "brain_gravity_v1_plan", "brain_maintenance_gravity_module"),
        ("validation_link", "brain_gravity_v1", "brain_gravity_audit"),
        ("approval_boundary", "brain_predicate_taxonomy_changes", "COVENANT"),
    ]
    return [proposal_for_rule(rule, src, dst) for rule, src, dst in pairs]


def _classify_path(path: str | None) -> str:
    if not path:
        return "NO_PATH"
    p = str(path).strip()
    desktop = str(C.DESKTOP_ROOT)
    universe = str(C.UNIVERSE_ROOT)
    atlas = str(C.ATLAS_ROOT)
    # Runtime prong hardcode fix (2026-08-04): was C.DESKTOP_ROOT / "ARCTURION_EXTERNAL_RUNTIME",
    # which ignored the ARCTURION_RUNTIME_ROOT env override — mirrors how C.ATLAS_ROOT is used
    # directly above. The bare relative-prefix check derives its basename from RUNTIME_ROOT.name
    # so an overridden root with a different folder name still matches its own relative form.
    runtime = str(C.RUNTIME_ROOT)
    runtime_name = C.RUNTIME_ROOT.name
    if p.startswith(runtime + "/") or p.startswith(runtime_name + "/"):
        return "RUNTIME"
    if p.startswith(atlas + "/") or p.startswith("ATLAS/"):
        return "ATLAS"
    if p.startswith(universe + "/"):
        return "UNIVERSE"
    if p.startswith(desktop + "/"):
        rest = p[len(desktop) + 1 :]
        head = rest.split("/", 1)[0]
        return "CONSTELLATION" if head in ACTIVE_AGENT_WORKSPACES else "OTHER"
    head = p.split("/", 1)[0]
    if head in ACTIVE_AGENT_WORKSPACES:
        return "CONSTELLATION"
    if head in UNIVERSE_PREFIXES:
        return "UNIVERSE"
    return "OTHER"


def _rows_to_counts(rows) -> dict[str, int]:
    return {str(r[0]): int(r[1]) for r in rows}


def _graph_view_status(desktop_root: Path) -> dict[str, Any]:
    # Desktop-home existence guard (Phase 9): only audit agents that actually have a
    # workspace on disk, so a renamed/retired agent can't show up as a phantom "missing".
    agents = []
    for agent in ACTIVE_AGENT_WORKSPACES:
        if not (desktop_root / agent).exists():
            continue
        graph = desktop_root / agent / "09_Linked_Surfaces" / "GRAPH_INDEX.md"
        agents.append({"agent": agent, "path": str(graph), "exists": graph.exists()})
    return {
        "total": len(agents),
        "present": sum(1 for item in agents if item["exists"]),
        "missing": [item for item in agents if not item["exists"]],
    }


# Predicates whose endpoints should resolve to real node ids (the typed-edge spine).
_TYPED_EDGE_PREDICATES = (
    "owned_by", "implemented_by", "validated_by", "routes_to", "depends_on",
    "authored_by", "maintained_by", "touched_by", "modified_by",
)
_TEMPORAL_PREDICATES = ("touched_by", "modified_by", "changed_in_session",
                        "discussed_in", "occurred_at")


def _coverage_metrics(conn, desktop: Path, with_answer_quality: bool = True) -> dict[str, Any]:
    """Brain v2 Phase 9 coverage block: prong, typed-edge (raw AND resolved),
    temporal, capability, and answer-quality coverage in one read-only pass."""
    def counts(sql, *a):
        return {str(r[0]): int(r[1]) for r in conn.execute(sql, a).fetchall()}

    # prong coverage
    prong = counts("SELECT COALESCE(prong,'(null)'), COUNT(*) FROM nodes GROUP BY prong")

    # typed-edge coverage — raw vs resolved (to_id is a real node id)
    typed: dict[str, dict] = {}
    raw_total = res_total = 0
    for pred in _TYPED_EDGE_PREDICATES:
        raw = conn.execute("SELECT COUNT(*) FROM edges WHERE predicate=?", (pred,)).fetchone()[0]
        resolved = conn.execute(
            "SELECT COUNT(*) FROM edges e WHERE e.predicate=? AND "
            "EXISTS (SELECT 1 FROM nodes n WHERE n.id = e.to_id)", (pred,)).fetchone()[0]
        typed[pred] = {"raw": raw, "resolved": resolved,
                       "resolved_pct": round(100 * resolved / raw, 1) if raw else None}
        raw_total += raw
        res_total += resolved

    # temporal coverage
    temporal = {
        "session_nodes": conn.execute("SELECT COUNT(*) FROM nodes WHERE type='Session'").fetchone()[0],
        "edit_nodes": conn.execute("SELECT COUNT(*) FROM nodes WHERE type='Edit'").fetchone()[0],
        "edges": {p: conn.execute("SELECT COUNT(*) FROM edges WHERE predicate=?", (p,)).fetchone()[0]
                  for p in _TEMPORAL_PREDICATES},
    }

    # capability coverage
    capability = {
        "total": conn.execute("SELECT COUNT(*) FROM nodes WHERE type='Capability'").fetchone()[0],
        "by_kind": counts("SELECT json_extract(metadata,'$.kind'), COUNT(*) "
                          "FROM nodes WHERE type='Capability' GROUP BY json_extract(metadata,'$.kind')"),
    }

    block = {
        "prong_coverage": prong,
        "typed_edge_coverage": {
            "by_predicate": typed,
            "raw_total": raw_total, "resolved_total": res_total,
            "resolved_pct": round(100 * res_total / raw_total, 1) if raw_total else None,
        },
        "temporal_coverage": temporal,
        "capability_coverage": capability,
    }

    if with_answer_quality:
        # engine.eval was deleted 2026-08-04 (Leg 1, retrieval-honesty). It reported
        # top1_canonical_rate = 1.0 forever because the Brain indexed the file holding its
        # own golden questions, so it was the top-1 answer to four of its own tests. The
        # replacement scores node IDs against a de-indexed golden set and reports per class.
        # The field names changed deliberately: `top1_canonical_rate` is the number that was
        # quoted to justify the BFS expansion caps, and it must not silently keep flowing
        # into dashboards as though nothing happened.
        try:
            from .. import retrieval_eval as _E
            aq = _E.run(token_budget=900)
            block["answer_quality"] = {
                "recall_at_5": aq["positive"]["recall5_rate"],
                "top1_rate": aq["positive"]["top1_rate"],
                "guard_pass_rate": aq["guards"]["pass_rate"],
                "questions": aq["questions"],
                "by_class": {c: b.get("recall5_rate", b.get("pass_rate"))
                             for c, b in aq["by_class"].items()},
            }
        except Exception as e:
            # Stays broad: an audit must still produce its other 5 coverage blocks when the
            # eval is unavailable. But `str(e)` alone loses the traceback, and the resulting
            # one-line "error" in a report tells you nothing about WHERE it broke — the
            # difference between "brain.query raised" and "golden.jsonl is missing" is the
            # difference between a retrieval outage and a deleted fixture. The traceback goes
            # to stderr, and the type is recorded alongside the message.
            import traceback
            traceback.print_exc()
            block["answer_quality"] = {"error": f"{type(e).__name__}: {e}"}
    return block


def audit(db_path: Path | None = None, desktop_root: Path | None = None,
          with_answer_quality: bool = True) -> dict[str, Any]:
    conn = DB.connect(db_path)
    try:
        counts = dict(conn.execute(
            "SELECT (SELECT COUNT(*) FROM nodes) nodes, "
            "(SELECT COUNT(*) FROM edges) edges, "
            "(SELECT COUNT(*) FROM embeddings) embeddings, "
            "(SELECT COUNT(*) FROM aliases) aliases"
        ).fetchone())
        predicate_counts = _rows_to_counts(
            conn.execute("SELECT predicate, COUNT(*) FROM edges GROUP BY predicate").fetchall()
        )
        family_counts = _rows_to_counts(
            conn.execute("SELECT family, COUNT(*) FROM nodes GROUP BY family").fetchall()
        )
        paths = conn.execute("SELECT path FROM nodes").fetchall()
        root_counts: dict[str, int] = {}
        for row in paths:
            root = _classify_path(row["path"])
            root_counts[root] = root_counts.get(root, 0) + 1
        orphan_count = len(DB.orphan_ids(conn))
        desktop_for_cov = desktop_root or C.DESKTOP_ROOT
        coverage = _coverage_metrics(conn, desktop_for_cov, with_answer_quality=with_answer_quality)
    finally:
        conn.close()

    retired = {
        pred: count for pred, count in predicate_counts.items()
        if pred in V.RETIRED_PREDICATES
    }
    weak = [pred for pred in GRAVITY_PREDICATES if predicate_counts.get(pred, 0) == 0]
    runtime_nodes = root_counts.get("RUNTIME", 0)
    runtime_edges = sum(predicate_counts.get(pred, 0) for pred in ("runs_on", "hosts", "executes", "depends_on"))
    desktop = desktop_root or C.DESKTOP_ROOT
    # Runtime prong hardcode fix (2026-08-04): was desktop / "ARCTURION_EXTERNAL_RUNTIME" / ...,
    # which ignored the ARCTURION_RUNTIME_ROOT env override even when desktop_root was left at
    # its default. C.RUNTIME_ROOT is the single source of truth for the runtime prong location
    # (see _classify_path above) regardless of any desktop_root override passed for test isolation.
    runtime_manifest = C.RUNTIME_ROOT / "RUNTIME_BOUNDARY_MANIFEST.md"

    return {
        "counts": counts,
        "family_counts": family_counts,
        "root_counts": root_counts,
        "orphan_count": orphan_count,
        "predicate_counts": {pred: predicate_counts.get(pred, 0) for pred in GRAVITY_PREDICATES},
        "weak_gravity_predicates": weak,
        "retired_predicate_drift": retired,
        "runtime": {
            "nodes": runtime_nodes,
            "runtime_dependency_edges": runtime_edges,
            "boundary_manifest": str(runtime_manifest),
            "boundary_manifest_exists": runtime_manifest.exists(),
            "classification": "non-canonical runtime substrate",
        },
        "agent_graph_views": _graph_view_status(desktop),
        "coverage_metrics": coverage,
        "candidate_predicates": candidate_predicates(),
        "seed_proposals": seed_proposals(),
    }


def to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Brain Gravity v1 Audit",
        "",
        "## Counts",
        f"- Nodes: **{report['counts'].get('nodes', 0)}**",
        f"- Edges: **{report['counts'].get('edges', 0)}**",
        f"- Embeddings: **{report['counts'].get('embeddings', 0)}**",
        f"- Aliases: **{report['counts'].get('aliases', 0)}**",
        f"- Orphans: **{report['orphan_count']}**",
        "",
        "## Cross-Prong Coverage",
    ]
    for root, count in sorted(report["root_counts"].items()):
        lines.append(f"- `{root}`: **{count}** nodes")
    lines.extend(["", "## Gravity Predicates"])
    for pred in GRAVITY_PREDICATES:
        count = report["predicate_counts"].get(pred, 0)
        marker = "weak" if pred in report["weak_gravity_predicates"] else "present"
        lines.append(f"- `{pred}`: **{count}** ({marker})")
    lines.extend(["", "## Retired Predicate Drift"])
    if report["retired_predicate_drift"]:
        for pred, count in sorted(report["retired_predicate_drift"].items()):
            lines.append(f"- `{pred}`: **{count}** edges need remediation queueing")
    else:
        lines.append("- None")
    lines.extend([
        "",
        "## External Runtime",
        f"- Classification: {report['runtime']['classification']}",
        f"- Nodes: **{report['runtime']['nodes']}**",
        f"- Runtime/dependency edges: **{report['runtime']['runtime_dependency_edges']}**",
        f"- Boundary manifest exists: **{report['runtime']['boundary_manifest_exists']}**",
        "",
        "## Agent Graph Views",
        f"- Present: **{report['agent_graph_views']['present']} / {report['agent_graph_views']['total']}**",
    ])
    if report["agent_graph_views"]["missing"]:
        for item in report["agent_graph_views"]["missing"]:
            lines.append(f"- Missing `{item['agent']}`: `{item['path']}`")
    cov = report.get("coverage_metrics") or {}
    if cov:
        lines.extend(["", "## Coverage Metrics (Brain v2 Phase 9)"])
        lines.append(f"- Prong coverage: {cov.get('prong_coverage')}")
        tec = cov.get("typed_edge_coverage", {})
        lines.append(f"- Typed-edge coverage: **{tec.get('resolved_total')}/{tec.get('raw_total')}** "
                     f"resolved (**{tec.get('resolved_pct')}%**)")
        temp = cov.get("temporal_coverage", {})
        lines.append(f"- Temporal: **{temp.get('session_nodes')}** sessions, "
                     f"**{temp.get('edit_nodes')}** edits, edges {temp.get('edges')}")
        capb = cov.get("capability_coverage", {})
        lines.append(f"- Capability: **{capb.get('total')}** ({capb.get('by_kind')})")
        # Keyed on the CURRENT field names. This branch tested `top1_canonical_rate`, a key
        # that stopped being produced when _coverage_metrics moved to engine.retrieval_eval —
        # so the condition was always false and the Answer Quality line silently disappeared
        # from every markdown report. A renamed metric that vanishes from the dashboard is
        # worse than one that reads wrong: nobody notices an absent row.
        aq = cov.get("answer_quality", {})
        if aq.get("error"):
            lines.append(f"- Answer quality: ⚠️ **unavailable** — {aq['error']}")
        elif "recall_at_5" in aq:
            by_class = aq.get("by_class") or {}
            per_class = ", ".join(f"{c} {v}" for c, v in sorted(by_class.items()))
            lines.append(f"- Answer quality: recall@5 **{aq['recall_at_5']}**, "
                         f"top1 **{aq['top1_rate']}**, guards **{aq.get('guard_pass_rate')}** "
                         f"over {aq.get('questions')} questions"
                         + (f" ({per_class})" if per_class else ""))
    lines.extend(["", "## Candidate Predicates (not emitted)"])
    for item in report["candidate_predicates"]:
        inverse = f" / `{item['inverse']}`" if item.get("inverse") else ""
        lines.append(f"- `{item['predicate']}`{inverse}: {item['rationale']}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Brain Gravity v1 audit")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    parser.add_argument("--proposals-jsonl", action="store_true", help="emit review-ready seed proposals as JSONL")
    args = parser.parse_args()
    report = audit()
    if args.proposals_jsonl:
        for edge in report["seed_proposals"]:
            print(json.dumps(edge, sort_keys=True))
    elif args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(to_markdown(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
