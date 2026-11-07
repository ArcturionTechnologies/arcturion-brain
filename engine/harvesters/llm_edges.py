"""LLM-augmented hub edges (Item 6).

For each top-centrality hub node with `< 3` outgoing non-semantic edges,
gather its 5 nearest semantic neighbors and ask Tier 1 Groq (via
`llm_router.chat(prompt, task='reason')`) to propose typed edges from a
constrained reasoning vocab. Validated proposals get written directly to
the brain for tier 0 nodes, or staged at
`06_MEMORY_SYSTEM/0611_LLM_WIKI/staging/proposals/` for tier ≥ 1.

Modes:
  --once   run a single bounded batch (default)
  --limit  cap the number of hubs processed (default 200)
  --dry-run print proposals without writing

Wired into `com.arcturion.altair-brain-llm-edges` LaunchAgent (weekly Sunday 03:00 ET).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .. import config as C
from .. import db as DB
from ..util import now_iso, short_hash

ALLOWED_PREDICATES = {
    "depends_on", "implies", "affects", "blocks", "supersedes",
    "derived_from", "cites", "supports", "contradicts",
    "requires_approval_from", "validated_by",
}

# Excluded from "typed edges already exist" count — these are auto-derived
# and don't represent reasoning relationships.
AUTO_DERIVED_PREDICATES = {
    "semantically_similar_to", "mentioned_in",
    "part_of", "contains", "calls", "called_by",
    "imports", "imported_by", "owned_by", "owns",
    "authored_by", "synonymous_with", "co_occurs_with",
}

STAGING_DIR = (
    C.UNIVERSE_ROOT / "06_MEMORY_SYSTEM" / "0611_LLM_WIKI" / "staging" / "proposals"
)
LLM_ROUTER_PATH = (
    C.UNIVERSE_ROOT / "11_INFRASTRUCTURE" / "1107_LOCAL_LLM" / "llm-router.py"
)


def _load_router():
    """Import llm-router.py (hyphen name → importlib)."""
    if not LLM_ROUTER_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location("llm_router", str(LLM_ROUTER_PATH))
    if spec is None or spec.loader is None:
        return None
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except Exception:
        return None
    return m


def _select_hubs(conn, limit: int) -> list[dict]:
    """Top-centrality hubs with <3 non-auto-derived outgoing edges."""
    auto_list = ",".join("?" * len(AUTO_DERIVED_PREDICATES))
    sql = f"""
        SELECT n.id, n.title, n.summary, n.governance_tier, n.centrality
        FROM nodes n
        WHERE n.centrality > 0 AND n.status != 'superseded'
          AND (
            SELECT COUNT(*) FROM edges
            WHERE from_id = n.id AND predicate NOT IN ({auto_list})
          ) < 3
        ORDER BY n.centrality DESC
        LIMIT ?
    """
    params = (*AUTO_DERIVED_PREDICATES, limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _nearest_semantic_neighbors(conn, node_id: str, k: int = 5) -> list[dict]:
    rows = conn.execute(
        """SELECT n.id, n.title, n.summary, e.weight
           FROM edges e
           JOIN nodes n ON n.id = e.to_id
           WHERE e.from_id = ? AND e.predicate = 'semantically_similar_to'
             AND n.status != 'superseded'
           ORDER BY e.weight DESC
           LIMIT ?""",
        (node_id, k),
    ).fetchall()
    return [dict(r) for r in rows]


_PROMPT_TEMPLATE = """\
You are reasoning about relationships between knowledge graph nodes.

HUB NODE:
  id: {hub_id}
  title: {hub_title}
  summary: {hub_summary}

CANDIDATE NEIGHBORS (semantically similar):
{neighbors_block}

Propose 0 to 5 typed relationships FROM the hub TO any candidate neighbor.
Use ONLY these predicates: {predicates}.

Return ONLY valid JSON, no prose, no markdown fences. Schema:
{{"edges": [{{"to": "<neighbor_id>", "predicate": "<from allowed>", "confidence": 0.0-1.0, "rationale": "<≤120 chars>"}}]}}

If no clear typed relationship exists, return {{"edges": []}}.
"""


def _build_prompt(hub: dict, neighbors: list[dict]) -> str:
    nb_lines = []
    for i, n in enumerate(neighbors, 1):
        title = (n.get("title") or n["id"])[:80]
        summary = (n.get("summary") or "")[:200].replace("\n", " ")
        nb_lines.append(f"  {i}. id={n['id']}  title={title!r}  summary={summary!r}")
    return _PROMPT_TEMPLATE.format(
        hub_id=hub["id"],
        hub_title=(hub.get("title") or hub["id"])[:80],
        hub_summary=(hub.get("summary") or "")[:300].replace("\n", " "),
        neighbors_block="\n".join(nb_lines) or "  (none)",
        predicates=sorted(ALLOWED_PREDICATES),
    )


def _parse_edges(raw: str) -> list[dict]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        s, e = raw.find("{"), raw.rfind("}")
        if s < 0 or e <= s:
            return []
        try:
            obj = json.loads(raw[s : e + 1])
        except Exception:
            return []
    edges = obj.get("edges") or []
    out: list[dict] = []
    for e in edges:
        if not isinstance(e, dict):
            continue
        to = e.get("to")
        pred = e.get("predicate")
        if not to or pred not in ALLOWED_PREDICATES:
            continue
        conf = float(e.get("confidence") or 0.7)
        if conf > 1.0:
            conf = 1.0
        if conf < 0.0:
            conf = 0.0
        out.append({
            "to": str(to),
            "predicate": pred,
            "confidence": conf,
            "rationale": str(e.get("rationale") or "")[:200],
        })
    return out


def _write_staging_jsonl(proposals: list[dict], asserted_by: str) -> Path:
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    h = short_hash(f"{asserted_by}:{ts}:{len(proposals)}")[:8]
    path = STAGING_DIR / f"{ts}_{asserted_by}_{h}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for p in proposals:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    return path


def _insert_edge_direct(conn, from_id: str, to_id: str, pred: str,
                        conf: float, rationale: str, ts: str) -> bool:
    return DB.insert_edge(conn, {
        "from": from_id,
        "to": to_id,
        "predicate": pred,
        "family": "reasoning",
        "weight": conf,
        "confidence": conf,
        "asserted_by": "LLM_AUGMENTED_HUB_EDGES",
        "asserted_at": ts,
        "evidence_span": rationale[:200],
        "metadata": {},
    })


def run(limit: int = 200, dry_run: bool = False) -> dict:
    router = _load_router()
    if router is None or not hasattr(router, "chat"):
        return {"status": "no_router", "hubs": 0, "edges_proposed": 0,
                "edges_written": 0, "edges_staged": 0}

    conn = DB.connect()
    try:
        hubs = _select_hubs(conn, limit)
        if not hubs:
            return {"status": "no_eligible_hubs", "hubs": 0, "edges_proposed": 0,
                    "edges_written": 0, "edges_staged": 0}

        ts = now_iso()
        edges_proposed = 0
        edges_written = 0
        staged: list[dict] = []
        processed = 0
        t0 = time.time()

        for hub in hubs:
            neighbors = _nearest_semantic_neighbors(conn, hub["id"], k=5)
            if not neighbors:
                continue
            prompt = _build_prompt(hub, neighbors)
            try:
                raw = router.chat(prompt, task="reason", max_tokens=400, temperature=0.0)
            except Exception:
                continue
            edges = _parse_edges(raw)
            if not edges:
                continue
            edges_proposed += len(edges)
            valid_neighbor_ids = {n["id"] for n in neighbors}
            tier = int(hub.get("governance_tier") or 0)

            for ed in edges:
                if ed["to"] not in valid_neighbor_ids:
                    continue
                if tier >= 1:
                    staged.append({
                        "proposal_id": f"prop_{short_hash(hub['id'] + ed['to'] + ed['predicate'])[:8]}",
                        "kind": "edge",
                        "from": hub["id"],
                        "to": ed["to"],
                        "predicate": ed["predicate"],
                        "confidence": ed["confidence"],
                        "evidence_span": ed["rationale"],
                        "asserted_by": "LLM_AUGMENTED_HUB_EDGES",
                        "asserted_at": ts,
                        "governance_tier": tier,
                    })
                elif not dry_run:
                    if _insert_edge_direct(conn, hub["id"], ed["to"], ed["predicate"],
                                           ed["confidence"], ed["rationale"], ts):
                        edges_written += 1
            processed += 1
            if processed % 25 == 0:
                conn.commit()

        if not dry_run:
            conn.commit()
            staging_path = _write_staging_jsonl(staged, "llm_edges") if staged else None
        else:
            staging_path = None

        return {
            "status": "ok",
            "hubs_processed": processed,
            "edges_proposed": edges_proposed,
            "edges_written": edges_written,
            "edges_staged": len(staged),
            "staging_path": str(staging_path) if staging_path else None,
            "elapsed_sec": round(time.time() - t0, 1),
        }
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    result = run(limit=args.limit, dry_run=args.dry_run)
    label = "[dry-run]" if args.dry_run else "[llm_edges]"
    print(f"\n{label} {json.dumps(result, indent=2)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
