"""Compute PageRank centrality over the brain graph; persist back to nodes table."""

from __future__ import annotations
import time
import sqlite3
from . import db as DB


def compute_pagerank(verbose: bool = True, alpha: float = 0.85,
                     max_iter: int = 50) -> int:
    import networkx as nx
    conn = DB.connect()
    t0 = time.time()

    if verbose:
        print("[brain] loading edges into NetworkX DiGraph ...")
    g = nx.DiGraph()
    rows = conn.execute("SELECT from_id, to_id, weight FROM edges").fetchall()
    for r in rows:
        # Sum weights when multi-edges collapse to a single DiGraph edge.
        if g.has_edge(r["from_id"], r["to_id"]):
            g[r["from_id"]][r["to_id"]]["weight"] += r["weight"]
        else:
            g.add_edge(r["from_id"], r["to_id"], weight=r["weight"])
    n_nodes = g.number_of_nodes()
    n_edges = g.number_of_edges()
    if verbose:
        print(f"[brain] graph: {n_nodes} nodes, {n_edges} edges; running PageRank (alpha={alpha})")

    pr = nx.pagerank(g, alpha=alpha, max_iter=max_iter, weight="weight")
    if verbose:
        print(f"[brain] PageRank done in {time.time() - t0:.1f}s; writing centrality to nodes table")

    updated = 0
    for nid, score in pr.items():
        cur = conn.execute("UPDATE nodes SET centrality=? WHERE id=?", (float(score), nid))
        updated += cur.rowcount
    conn.commit()
    conn.close()
    if verbose:
        print(f"[brain] centrality updated for {updated} nodes")
    return updated


def main():
    n = compute_pagerank()
    print(f"pagerank_updated={n}")


if __name__ == "__main__":
    main()
