"""Append edges + nodes to the canonical JSONL ground-truth files."""

from __future__ import annotations
import json
import sqlite3
from . import config as C


def export_triples_jsonl(conn: sqlite3.Connection, out_path=None) -> int:
    p = out_path or C.GRAPH_DIR / "triples_brain_v2.jsonl"
    rows = conn.execute(
        """SELECT from_id, to_id, predicate, family, weight, confidence,
                  asserted_by, asserted_at, evidence_span, metadata
           FROM edges"""
    ).fetchall()
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            obj = {
                "subject": r["from_id"], "verb": r["predicate"], "object": r["to_id"],
                "family": r["family"], "weight": r["weight"], "confidence": r["confidence"],
                "asserted_by": r["asserted_by"], "asserted_at": r["asserted_at"],
                "evidence_span": r["evidence_span"],
            }
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return len(rows)


def export_node_index_jsonl(conn: sqlite3.Connection, out_path=None) -> int:
    p = out_path or C.GRAPH_DIR / "node_index_brain_v2.jsonl"
    rows = conn.execute(
        "SELECT id, title, type, family, status, owner, path, summary, last_modified, centrality FROM nodes"
    ).fetchall()
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            obj = {
                "id": r["id"], "title": r["title"], "node_type": r["type"],
                "family": r["family"], "status": r["status"], "owner": r["owner"],
                "path": r["path"], "summary": r["summary"],
                "last_modified": r["last_modified"], "centrality": r["centrality"],
            }
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return len(rows)


def export_graph_json(conn: sqlite3.Connection, out_path=None) -> dict:
    p = out_path or C.GRAPH_JSON_EXPORT
    nodes = []
    for r in conn.execute(
        "SELECT id, type, family, title, path, owner, status, governance_tier, centrality FROM nodes"
    ).fetchall():
        nodes.append(dict(r))
    edges = []
    for r in conn.execute(
        "SELECT from_id, to_id, predicate, family, weight, confidence, asserted_by FROM edges"
    ).fetchall():
        edges.append(dict(r))
    obj = {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
    }
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=None))
    return {"nodes": len(nodes), "edges": len(edges)}
