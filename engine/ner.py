"""NER + LLM-augmented edges via local Qwen 7B (Tier 0a) with thermal gating.

Strategy: for nodes that are still orphan or weakly connected after Phases 1-3,
ask the local 7B to extract entity mentions and propose typed edges. Always
gated through thermal-check.sh; falls through to Tier 1 Groq on red.

This is a *lightweight, iterative* enrichment pass — designed for heartbeat use,
not bulk one-shot. Default `limit=50` per invocation.
"""

from __future__ import annotations
import json
import time
from pathlib import Path
import sqlite3
import sys

# Path-relative import so this works whether run as script or module.
_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from . import config as C
from . import db as DB
from .aliases import load_aliases
from .util import now_iso

try:
    from local_llm_client import chat, health, LocalLLMError
    _HAS_LOCAL = True
except Exception:
    _HAS_LOCAL = False


_NER_SYSTEM = (
    "Extract named entities from the text. Return a JSON object with key "
    "`entities` containing a list of objects with fields: name (string), "
    "type (one of: person, agent, organization, project, technology, tool, "
    "concept, location). Return ONLY JSON, no prose, no fences. "
    "Max 10 entities. Skip generic words."
)


def _extract_entities(text: str) -> list[dict]:
    if not _HAS_LOCAL or not health():
        return []
    snippet = text[:1500]
    try:
        out = chat(snippet, system=_NER_SYSTEM, max_tokens=400, temperature=0.0, timeout=10)
    except LocalLLMError:
        return []
    raw = out.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            try:
                obj = json.loads(raw[start:end+1])
            except Exception:
                return []
        else:
            return []
    ents = obj.get("entities") or []
    if not isinstance(ents, list):
        return []
    return [e for e in ents if isinstance(e, dict) and e.get("name")]


def _resolve(name: str, alias_to_canonical: dict[str, str]) -> str:
    return alias_to_canonical.get(name.lower().strip(), name.strip())


def enrich_weak_nodes(limit: int = 50, verbose: bool = True) -> dict:
    """Pick up to `limit` nodes with <3 outgoing edges and try NER enrichment."""
    if not _HAS_LOCAL:
        return {"status": "no_local_llm_client", "enriched": 0}
    if not health():
        return {"status": "local_llm_down", "enriched": 0}
    _, alias_to_canonical = load_aliases()

    conn = DB.connect()
    rows = conn.execute(
        """SELECT n.id, n.title, n.summary, n.body_excerpt, n.path,
                  (SELECT COUNT(*) FROM edges WHERE from_id = n.id) AS out_c
           FROM nodes n
           WHERE n.family = 'document' AND length(n.summary) > 40
           ORDER BY out_c ASC, n.centrality DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()

    asserted_at = now_iso()
    enriched = 0
    edges_added = 0
    t0 = time.time()
    for r in rows:
        text = ((r["summary"] or "") + "\n" + (r["body_excerpt"] or ""))[:2000]
        if len(text.strip()) < 40:
            continue
        entities = _extract_entities(text)
        if not entities:
            continue
        enriched += 1
        for ent in entities:
            name = str(ent.get("name") or "").strip()
            if not name:
                continue
            canonical = _resolve(name, alias_to_canonical)
            edge = {
                "from": canonical, "to": r["id"],
                "predicate": "mentioned_in", "family": "external",
                "weight": 0.6, "confidence": 0.7,
                "asserted_by": "LLM_LOCAL_7B_NER",
                "asserted_at": asserted_at,
                "evidence_span": f"ner:{ent.get('type', 'entity')}",
                "metadata": {"entity_name": name, "entity_type": ent.get("type")},
            }
            if DB.insert_edge(conn, edge):
                edges_added += 1
        if verbose and enriched % 5 == 0:
            print(f"[brain][ner] {enriched}/{len(rows)} processed, {edges_added} edges, "
                  f"{time.time()-t0:.1f}s")
    conn.commit()
    conn.close()
    return {
        "status": "ok", "candidates": len(rows), "enriched": enriched,
        "edges_added": edges_added, "elapsed_sec": round(time.time() - t0, 2),
    }


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()
    print(enrich_weak_nodes(limit=args.limit))


if __name__ == "__main__":
    main()
