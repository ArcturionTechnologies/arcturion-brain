"""Derive part_of / contains edges from directory hierarchy."""

from __future__ import annotations
import sqlite3
from . import db as DB
from .util import now_iso


def derive_path_edges(conn: sqlite3.Connection) -> int:
    """For every node with a parent path that is itself a node, emit part_of."""
    asserted_at = now_iso()
    rows = conn.execute(
        "SELECT id, path FROM nodes WHERE path IS NOT NULL AND path != ''"
    ).fetchall()
    by_path = {r["path"]: r["id"] for r in rows if r["path"]}
    # Index also parent dirs that match any node path (e.g., a README in that dir).
    inserted = 0
    for r in rows:
        nid, p = r["id"], r["path"]
        if not p:
            continue
        parts = p.split("/")
        if len(parts) < 2:
            continue
        parent = "/".join(parts[:-1])
        # Try common parent representations: parent dir + README.md, parent dir itself.
        candidate_targets = [parent, f"{parent}/README.md", f"{parent}/0000_README.md"]
        for tgt in candidate_targets:
            if tgt in by_path and by_path[tgt] != nid:
                edge = {
                    "from": nid, "to": by_path[tgt],
                    "predicate": "part_of", "family": "structure",
                    "weight": 0.8, "confidence": 0.85,
                    "asserted_by": "STATIC_PATH", "asserted_at": asserted_at,
                    "evidence_span": "filesystem-parent", "metadata": {},
                }
                try:
                    if DB.insert_edge(conn, edge):
                        inserted += 1
                except sqlite3.IntegrityError:
                    pass
                break  # only one parent per node
    return inserted
