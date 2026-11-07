"""Generate coverage_dashboard.md, reciprocity_report.md, drift_log.md.

Item 3: drift detection + alert emission to COVENANT inbox. On every heartbeat,
compare current stats vs prior snapshot. If any of (orphan growth >5%, any family
shrinks, top-100 centrality cosine drift >0.1), append a `## Brain Drift Alert`
block to `02_GOVERNANCE/0201_COVENANT/inbox.md`.
"""

from __future__ import annotations
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from . import config as C
from . import db as DB
from .util import now_iso

# Drift thresholds — tune by observation.
ORPHAN_GROWTH_THRESHOLD = 0.05  # 5% growth triggers alert
CENTROID_COSINE_THRESHOLD = 0.10  # 1 - cosine_sim > 0.10 triggers alert
TOP_K_FOR_CENTROID = 100


def write_coverage_dashboard() -> Path:
    conn = DB.connect()
    s = DB.stats(conn)
    s["node_families"] = DB.family_counts(conn, "nodes")
    s["top_predicates"] = DB.predicate_counts(conn)
    s["orphan_count"] = len(DB.orphan_ids(conn))
    conn.close()

    p = C.COVERAGE_DASHBOARD
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Infinite Brain — Coverage Dashboard",
        "",
        f"_generated: {now_iso()}_",
        "",
        "## Totals",
        "",
        f"- Nodes: **{s.get('nodes', 0):,}**",
        f"- Edges: **{s.get('edges', 0):,}**",
        f"- Embeddings: **{s.get('embeddings', 0):,}**",
        f"- Orphans: **{s.get('orphan_count', 0):,}**",
        "",
        "## Node Families",
        "",
        "| Family | Count |",
        "|---|---|",
    ]
    for fam, cnt in sorted(s["node_families"].items(), key=lambda x: -x[1]):
        lines.append(f"| `{fam}` | {cnt:,} |")

    lines.append("")
    lines.append("## Edge Predicates (top 40)")
    lines.append("")
    lines.append("| Predicate | Count |")
    lines.append("|---|---|")
    for pred, cnt in list(s["top_predicates"].items())[:40]:
        lines.append(f"| `{pred}` | {cnt:,} |")

    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def write_reciprocity_report(sample_hubs: list[str] | None = None) -> Path:
    """For each hub (high-centrality node), count incoming vs outgoing edges."""
    conn = DB.connect()
    if not sample_hubs:
        rows = conn.execute(
            "SELECT id, title FROM nodes ORDER BY centrality DESC LIMIT 30"
        ).fetchall()
        sample_hubs = [(r["id"], r["title"]) for r in rows]
    else:
        sample_hubs = [(h, h) for h in sample_hubs]

    lines = [
        "# Infinite Brain — Reciprocity Report",
        "",
        f"_generated: {now_iso()}_",
        "",
        "Top-centrality hubs and their in/out edge counts.",
        "",
        "| Hub | Title | Out | In | Ratio (in/out) |",
        "|---|---|---|---|---|",
    ]
    overall_in = overall_out = 0
    for hid, title in sample_hubs:
        out_c = conn.execute("SELECT COUNT(*) c FROM edges WHERE from_id=?", (hid,)).fetchone()["c"]
        in_c = conn.execute("SELECT COUNT(*) c FROM edges WHERE to_id=?", (hid,)).fetchone()["c"]
        overall_in += in_c
        overall_out += out_c
        ratio = (in_c / out_c) if out_c else float("inf") if in_c else 0
        lines.append(f"| `{hid[:48]}` | {title[:40]} | {out_c} | {in_c} | {ratio:.2f} |")
    overall_ratio = (overall_in / overall_out) if overall_out else 0
    lines.append("")
    lines.append(f"**Overall in/out ratio across sampled hubs:** {overall_ratio:.2f}")
    conn.close()
    p = C.RECIPROCITY_REPORT
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def append_drift_log(stats: dict) -> Path:
    p = C.DRIFT_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = now_iso()
    header_needed = not p.exists() or p.stat().st_size == 0
    with p.open("a", encoding="utf-8") as f:
        if header_needed:
            f.write("# Infinite Brain — Drift Log\n\n")
            f.write("| Timestamp | Nodes | Edges | Embeddings | Orphans | Notes |\n")
            f.write("|---|---|---|---|---|---|\n")
        f.write(
            f"| {ts} | {stats.get('nodes', 0)} | {stats.get('edges', 0)} | "
            f"{stats.get('embeddings', 0)} | {stats.get('orphan_count', 0)} | "
            f"{stats.get('notes', '')} |\n"
        )
    return p


def _load_snapshot() -> dict | None:
    if not C.DRIFT_SNAPSHOT.exists():
        return None
    try:
        return json.loads(C.DRIFT_SNAPSHOT.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_snapshot(snapshot: dict) -> None:
    C.DRIFT_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    C.DRIFT_SNAPSHOT.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _top_centrality_vector(conn, k: int = TOP_K_FOR_CENTROID) -> dict[str, float]:
    rows = conn.execute(
        "SELECT id, centrality FROM nodes WHERE centrality IS NOT NULL "
        "ORDER BY centrality DESC LIMIT ?",
        (k,),
    ).fetchall()
    return {r["id"]: float(r["centrality"] or 0.0) for r in rows}


def _cosine_distance(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine distance (1 - similarity) between two sparse weighted node sets.
    Missing keys treated as 0. Returns 0.0 when both are empty."""
    keys = set(a) | set(b)
    if not keys:
        return 0.0
    dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 1.0
    return 1.0 - (dot / (na * nb))


def _build_snapshot(conn) -> dict:
    s = DB.stats(conn)
    families = DB.family_counts(conn, "nodes")
    orphans = len(DB.orphan_ids(conn))
    centroid = _top_centrality_vector(conn)
    return {
        "ts": now_iso(),
        "nodes": s.get("nodes", 0),
        "edges": s.get("edges", 0),
        "embeddings": s.get("embeddings", 0),
        "orphan_count": orphans,
        "families": families,
        "centroid_top_k": centroid,
    }


def _detect_drift(current: dict, prior: dict | None) -> list[dict]:
    """Return a list of alert dicts. Empty list = healthy."""
    if not prior:
        return []
    alerts: list[dict] = []

    # 1. Orphan growth
    prev_orphans = max(prior.get("orphan_count", 0), 1)
    cur_orphans = current.get("orphan_count", 0)
    growth = (cur_orphans - prev_orphans) / prev_orphans
    if growth > ORPHAN_GROWTH_THRESHOLD:
        alerts.append({
            "metric": "orphan_growth",
            "before": prior.get("orphan_count", 0),
            "after": cur_orphans,
            "delta_pct": round(growth * 100, 1),
            "likely_cause": "extractor regression or harvest pulling in disconnected leaves",
            "remediation": "inspect last 3 heartbeat logs in `12_LOGS/brain/`; consider re-running affected harvesters",
        })

    # 2. Family shrink
    prior_families = prior.get("families", {})
    cur_families = current.get("families", {})
    for fam in prior_families:
        prev = prior_families.get(fam, 0)
        cur = cur_families.get(fam, 0)
        if cur < prev and prev > 0 and (prev - cur) / prev > 0.02:
            alerts.append({
                "metric": "family_shrink",
                "family": fam,
                "before": prev,
                "after": cur,
                "delta_pct": round(-100.0 * (prev - cur) / prev, 1),
                "likely_cause": f"`{fam}` harvester returned fewer records than last heartbeat",
                "remediation": f"check `brain.harvesters.{fam}` log + source availability (TCC, API, network)",
            })

    # 3. Centroid cosine drift
    cos_dist = _cosine_distance(
        current.get("centroid_top_k", {}),
        prior.get("centroid_top_k", {}),
    )
    if cos_dist > CENTROID_COSINE_THRESHOLD:
        alerts.append({
            "metric": "centroid_drift",
            "cosine_distance": round(cos_dist, 4),
            "threshold": CENTROID_COSINE_THRESHOLD,
            "likely_cause": "top-centrality hubs shifted >10% — major structural change or PageRank regression",
            "remediation": "compare `coverage_dashboard.md` top predicates vs prior; inspect new hub IDs",
        })

    return alerts


def _ensure_inbox_header(p: Path) -> None:
    if p.exists() and p.stat().st_size > 0:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\n"
        "created: " + now_iso() + "\n"
        "owner: COVENANT\n"
        "ttl_tier: 1\n"
        "status: active\n"
        "---\n\n"
        "# COVENANT Inbox\n\n"
        "Append-only queue of governance events that need COVENANT review:\n"
        "brain drift alerts, proposal queue entries, approval requests.\n"
        "Reviewed entries get moved to `## Resolved`.\n\n"
        "## Active\n\n",
        encoding="utf-8",
    )


def _write_alert_block(alerts: list[dict]) -> None:
    if not alerts:
        return
    _ensure_inbox_header(C.COVENANT_INBOX)
    ts = now_iso()
    block = [f"### 🚨 Brain Drift Alert — {ts}", ""]
    for a in alerts:
        block.append(f"- **{a['metric']}**")
        for k, v in a.items():
            if k == "metric":
                continue
            block.append(f"    - {k}: {v}")
    block.append(f"    - source: `06_MEMORY_SYSTEM/0611_LLM_WIKI/graph/drift_log.md`")
    block.append("")
    with C.COVENANT_INBOX.open("a", encoding="utf-8") as f:
        f.write("\n".join(block) + "\n")


def check_drift_and_alert() -> dict:
    """Build current snapshot, compare to prior, emit alerts, save new snapshot.
    Returns a summary dict with alert count + snapshot path."""
    conn = DB.connect()
    try:
        current = _build_snapshot(conn)
    finally:
        conn.close()

    prior = _load_snapshot()
    alerts = _detect_drift(current, prior)
    _write_alert_block(alerts)
    _save_snapshot(current)
    return {
        "ts": current["ts"],
        "alerts_emitted": len(alerts),
        "alerts": alerts,
        "snapshot_path": str(C.DRIFT_SNAPSHOT),
        "inbox_path": str(C.COVENANT_INBOX) if alerts else None,
    }


def main():
    print(f"coverage: {write_coverage_dashboard()}")
    print(f"reciprocity: {write_reciprocity_report()}")
    conn = DB.connect()
    s = DB.stats(conn)
    s["orphan_count"] = len(DB.orphan_ids(conn))
    s["notes"] = "manual report run"
    conn.close()
    print(f"drift: {append_drift_log(s)}")
    drift = check_drift_and_alert()
    if drift["alerts_emitted"]:
        print(f"🚨 drift: {drift['alerts_emitted']} alerts emitted to {drift['inbox_path']}")
    else:
        print(f"✅ drift: no alerts (snapshot at {drift['snapshot_path']})")


if __name__ == "__main__":
    main()
