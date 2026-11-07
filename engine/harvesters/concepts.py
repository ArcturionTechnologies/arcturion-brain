"""Concept/Entity nodes via NER over top-centrality hub nodes.

For each high-centrality document node, ask the local Qwen 7B to extract entities,
then resolve them through ENTITY_ALIASES, create Entity nodes, and emit
`mentioned_in` edges back to the source documents.

Idempotent: existing entity nodes get new edges; never duplicates.
"""

from __future__ import annotations
import hashlib
import json
import time
import sys
import urllib.request
from pathlib import Path

# Make `from local_llm_client import ...` importable.
from .. import config as C
sys.path.insert(0, str(C.LOCAL_LLM_CLIENT.parent))

from .. import db as DB
from ..aliases import load_aliases
from ..util import slugify, now_iso

try:
    from local_llm_client import chat, health, LocalLLMError, HEALTH_URL
    _HAS_LOCAL = True
except Exception:
    _HAS_LOCAL = False
    HEALTH_URL = None

try:
    from local_llm_client import chat_14b, health_14b
    _HAS_14B = True
except Exception:
    _HAS_14B = False

# Item 5: smart-selection thresholds
SATURATION_EDGE_FLOOR = 5      # skip docs with ≥5 existing mentioned_in edges
ESCALATE_14B_TOP_K = 20        # use Qwen 14B for the top-K most-central hubs


def _local_service_available() -> bool:
    """Return whether the local service is reachable and advertises a model.

    The loaded flag describes current residency only. A reachable unloaded
    service may load lazily when the existing thermally gated chat() is called.
    """
    if not _HAS_LOCAL or not HEALTH_URL:
        return False
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=1.5) as response:
            if getattr(response, "status", None) != 200:
                return False
            payload = response.read(64 * 1024 + 1)
        if len(payload) > 64 * 1024:
            return False
        health_data = json.loads(payload.decode("utf-8"))
        return (
            isinstance(health_data, dict)
            and health_data.get("status") == "ok"
            and isinstance(health_data.get("model"), str)
            and bool(health_data["model"].strip())
            and type(health_data.get("loaded")) is bool
        )
    except Exception:
        return False


_NER_SYSTEM = (
    "Extract named entities from the text. Return a JSON object with key "
    "`entities` containing a list of objects with fields: name (string), "
    "type (one of: person, agent, organization, project, technology, tool, "
    "concept, brand, location). Return ONLY JSON, no prose, no fences. "
    "Max 10 entities. Skip generic words and common file types."
)


def _extract_entities(text: str, use_14b: bool = False) -> list[dict]:
    """Run NER over `text`. When `use_14b` and 14B server is healthy, escalate
    to Qwen 14B for higher-quality entity extraction on top hubs. Falls back
    to 7B (which itself falls back to no-op if down)."""
    snippet = text[:1500]
    if use_14b and _HAS_14B and health_14b():
        try:
            out = chat_14b(snippet, system=_NER_SYSTEM, max_tokens=500,
                           temperature=0.0, timeout=30, fallback_to_7b=True)
        except Exception:
            out = None
    else:
        out = None
    if out is None:
        if not _HAS_LOCAL or not _local_service_available():
            raise RuntimeError("local NER source unavailable during harvest")
        try:
            out = chat(snippet, system=_NER_SYSTEM, max_tokens=400, temperature=0.0, timeout=15)
        except LocalLLMError as exc:
            raise RuntimeError(f"local NER query failed: {exc}") from exc
    raw = out.strip()
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
            raise RuntimeError("local NER returned malformed JSON")
        try:
            obj = json.loads(raw[s:e+1])
        except Exception as exc:
            raise RuntimeError(f"local NER returned malformed JSON: {exc}") from exc
    ents = obj.get("entities") or []
    return [e for e in ents if isinstance(e, dict) and e.get("name")]


def _canonical_entity_id(name: str, kind: str, alias_to_canonical: dict) -> str:
    canonical = alias_to_canonical.get(name.lower().strip())
    if canonical:
        return canonical
    return f"entity_{kind}_{slugify(name)}"


def harvest(limit: int = 500, verbose: bool = True, source_id: str | None = None) -> dict:
    if source_id is not None and (
            not isinstance(source_id, str)
            or not source_id
            or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-" for ch in source_id)):
        raise ValueError("source_id must be a non-empty exact node ID")
    if not _HAS_LOCAL:
        return {"family": "concept", "status": "no_local_llm_client",
                "nodes": 0, "edges": 0, "elapsed_sec": 0.0}
    if not _local_service_available():
        return {"family": "concept", "status": "local_llm_down",
                "nodes": 0, "edges": 0, "elapsed_sec": 0.0,
                "note": "Local model service is unreachable or returned invalid health metadata; retry when reachable"}

    _, alias_to_canonical = load_aliases()
    asserted_at = now_iso()
    conn = DB.connect()
    t0 = time.time()

    # Item 5: skip docs already saturated with mentioned_in edges (≥5 floor).
    # Plus the existing selector: document family + non-trivial summary, ranked by centrality.
    if source_id is None:
        # Default corpus-wide behavior is intentionally unchanged.
        rows = conn.execute(
            """SELECT n.*
               FROM nodes n
               WHERE n.family = 'document' AND length(n.summary) > 60
                 AND (SELECT COUNT(*) FROM edges WHERE to_id = n.id AND predicate = 'mentioned_in')
                     < ?
               ORDER BY n.centrality DESC
               LIMIT ?""", (SATURATION_EDGE_FLOOR, limit)
        ).fetchall()
    else:
        # Exact-source mode never falls back to the ranked corpus selector.
        rows = conn.execute(
            """SELECT n.*
               FROM nodes n
               WHERE n.id = ? AND n.family = 'document' AND length(n.summary) > 60
                 AND (SELECT COUNT(*) FROM edges WHERE to_id = n.id AND predicate = 'mentioned_in')
                     < ?""", (source_id, SATURATION_EDGE_FLOOR)
        ).fetchall()
    from ..source_integrity import filter_retrieval
    rows = filter_retrieval(conn, [dict(r) for r in rows])
    if source_id is not None and (
            len(rows) != 1 or rows[0].get("id") != source_id):
        conn.close()
        return {"family": "concept", "status": "source_not_eligible",
                "error": "requested source is missing or fails Concepts eligibility",
                "nodes": 0, "edges": 0, "processed": 0, "skipped": 0,
                "elapsed_sec": 0.0}

    nodes_created = 0
    edges_added = 0
    processed = 0
    skipped = 0

    seen_entity_ids: set[str] = set()
    source_facts: list[dict] = []
    extract_cache: dict[str, list[dict]] = {}
    cache_hits = 0
    used_14b = 0
    for idx, r in enumerate(rows):
        text = ((r["summary"] or "") + "\n" + (r["body_excerpt"] or ""))[:1800]
        if len(text.strip()) < 50:
            skipped += 1
            continue
        cache_key = (r["id"], "14b" if idx < ESCALATE_14B_TOP_K else "7b")
        if cache_key in extract_cache:
            entities = extract_cache[cache_key]
            cache_hits += 1
        else:
            escalate = idx < ESCALATE_14B_TOP_K
            entities = _extract_entities(text, use_14b=escalate)
            extract_cache[cache_key] = entities
            if escalate and entities:
                used_14b += 1
        if not entities:
            source_facts.append({"node_id": r["id"], "text_sha256": hashlib.sha256(
                text.encode()).hexdigest(), "entities": []})
            skipped += 1
            continue
        source_facts.append({"node_id": r["id"], "text_sha256": hashlib.sha256(
            text.encode()).hexdigest(), "entities": sorted(
                [{"name": str(e.get("name") or ""), "type": str(e.get("type") or "concept")}
                 for e in entities], key=lambda x: (x["name"], x["type"]))})
        processed += 1
        for ent in entities:
            name = str(ent.get("name") or "").strip()
            if not name or len(name) < 2:
                continue
            kind = (str(ent.get("type") or "concept")).lower()
            eid = _canonical_entity_id(name, kind, alias_to_canonical)
            if eid not in seen_entity_ids:
                DB.upsert_node(conn, {
                    "id": eid, "type": "Entity", "family": "concept",
                    "title": name, "path": None, "owner": "FOUNDER",
                    "status": "active", "governance_tier": 0, "content_hash": None,
                    "summary": f"NER-extracted entity of type {kind}",
                    "body_excerpt": None, "centrality": 0.0,
                    "created": None, "last_modified": now_iso(),
                    "last_reviewed": None,
                    "metadata": {"entity_kind": kind, "first_seen_in": r["id"]},
                })
                seen_entity_ids.add(eid)
                nodes_created += 1
            if DB.insert_edge(conn, {
                "from": eid, "to": r["id"],
                "predicate": "mentioned_in", "family": "external",
                "weight": 0.6, "confidence": 0.7,
                "asserted_by": "LLM_LOCAL_7B_NER", "asserted_at": asserted_at,
                "evidence_span": f"ner:{kind}", "metadata": {"entity_name": name},
            }):
                edges_added += 1
        if verbose and processed % 25 == 0:
            print(f"[harvest:concepts] {processed}/{len(rows)} docs processed, "
                  f"{nodes_created} entities, {edges_added} edges, "
                  f"{time.time()-t0:.1f}s")
        if processed % 50 == 0:
            conn.commit()

    conn.commit()
    conn.close()
    elapsed = time.time() - t0
    if verbose:
        print(f"[harvest:concepts] DONE — processed={processed} skipped={skipped} "
              f"entities={nodes_created} edges={edges_added} "
              f"cache_hits={cache_hits} used_14b={used_14b} "
              f"in {elapsed:.1f}s")
    source_digest = hashlib.sha256(json.dumps(
        source_facts, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    if source_id is not None:
        if len(source_facts) != 1 or source_facts[0].get("node_id") != source_id:
            return {"family": "concept", "status": "source_not_processed",
                    "error": "requested source did not yield a source-bound result",
                    "nodes": nodes_created, "edges": edges_added, "processed": processed,
                    "skipped": skipped, "elapsed_sec": round(elapsed, 2)}
        return {"family": "concept", "status": "ok", "scope": "source",
                "source_id": source_id, "processed": processed, "skipped": skipped,
                "nodes": nodes_created, "edges": edges_added,
                "cache_hits": cache_hits, "used_14b": used_14b,
                "source_digest": source_digest, "elapsed_sec": round(elapsed, 2)}
    return {"family": "concept", "status": "ok", "processed": processed,
            "skipped": skipped, "nodes": nodes_created, "edges": edges_added,
            "cache_hits": cache_hits, "used_14b": used_14b,
            "source_digest": source_digest,
            "elapsed_sec": round(elapsed, 2)}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500)
    args = ap.parse_args()
    print(harvest(limit=args.limit))
