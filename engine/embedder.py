"""Embed every node's text with sentence-transformers/all-MiniLM-L6-v2.

Stores embeddings as packed float32 bytes in the embeddings table.
Then derives `semantically_similar_to` edges via sklearn NearestNeighbors.
"""

from __future__ import annotations
import os
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import time
import numpy as np
import sqlite3
from pathlib import Path
from . import config as C
from . import db as DB
from .util import now_iso


def _row_get(row: sqlite3.Row, key: str):
    """`row[key]` if the query selected that column, else None. `embed_all`'s SELECT list
    changed between callers historically (limit-only test fixtures sometimes select fewer
    columns), so this must not raise on a row that lacks `key` rather than assume every
    caller's SELECT is in sync with this module."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _context_header(row: sqlite3.Row) -> str:
    """Deterministic context header for contextual embedding (Leg 3, retrieval-honesty
    2026-08-06 — Anthropic contextual-retrieval's cheap half: no LLM call, no new deps,
    just the structured fields the node already carries). Prepended to the text that gets
    EMBEDDED, never to the stored body and never to the FTS-indexed text.

    Composition is title, then shelf/prong, owner agent, doc kind — each only if present,
    joined with ' | ' so a node missing a field degrades gracefully rather than emitting
    'None' or a dangling separator. This gives the vector channel signal a bare
    title+summary+body excerpt does not have: e.g. a 'Stop hook feedback' session-log twin
    now carries 'shelf: constellation | kind: Session' where a 'Stop Gate Implementation
    Pattern' knowledge doc carries 'shelf: hq | kind: Preference' — exactly the kind of
    common-token collision ct-01's fixture note describes ('stop' crowded by seven session
    files whose only link to the query was the word 'stop')."""
    bits: list[str] = []
    title = (_row_get(row, "title") or "").strip()
    if title:
        bits.append(title)
    prong = _row_get(row, "prong")
    if prong:
        bits.append(f"shelf: {prong}")
    owner = _row_get(row, "owner")
    if owner:
        bits.append(f"owner: {owner}")
    doc_type = _row_get(row, "type")
    if doc_type:
        bits.append(f"kind: {doc_type}")
    return " | ".join(bits)


def _text_for_embedding(row: sqlite3.Row) -> str:
    """Text handed to the encoder for one node. `body` is exactly the pre-Leg-3 computation
    (title/summary/body-excerpt) — the STORED body and the FTS index are untouched by this
    entire leg, only the embedding INPUT changes, and only when `C.CONTEXTUAL_EMBED_PREFIX`
    is on. Flag off (or a header that composes empty, e.g. a row selected without the extra
    columns) returns `body` unchanged — byte-identical to the pre-Leg-3 function."""
    parts = [row["title"] or "", row["summary"] or "", (row["body_excerpt"] or "")[:C.EMBED_SAMPLE_CHARS]]
    body = "\n".join(p for p in parts if p).strip()
    if not C.CONTEXTUAL_EMBED_PREFIX:
        return body
    header = _context_header(row)
    if not header:
        return body
    return f"{header}\n{body}" if body else header


_MODEL = None  # cached across calls — keeps the model warm in the long-running watch daemon


def _get_model():
    """Load the embed model once per process and reuse it. In the brain-memory-watch
    daemon (a single long-lived process), this means the model loads on the first
    ingest and stays warm for every later burst — no repeated multi-second cold load
    (and the CPU/thermal cost that comes with it). A one-shot CLI run loads it once."""
    global _MODEL
    if _MODEL is None:
        if not C.EMBED_MODEL or not os.path.isabs(C.EMBED_MODEL) or not os.path.isdir(C.EMBED_MODEL):
            raise ValueError('explicit local model directory required')
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer(C.EMBED_MODEL, local_files_only=True, trust_remote_code=False)
    return _MODEL


def embed_all(verbose: bool = True, limit: int | None = None, force: bool = False,
              node_ids: list[str] | None = None) -> dict:
    """Embed every node that lacks an embedding (the normal incremental path — `ingest.
    ingest_paths` drops stale rows for changed nodes before calling this, so "lacks" also
    covers "just changed").

    `force=True` re-embeds EVERY node regardless of whether it already has a vector — the
    maintenance path for a change to `_text_for_embedding`'s INPUT (e.g. Leg 3's contextual
    prefix), where the embedding-input text changed for nodes whose row is otherwise
    unchanged, so the normal "missing embedding" filter would never touch them.
    `DB.upsert_embedding` is an UPSERT (ON CONFLICT DO UPDATE), so this is safe to re-run —
    it overwrites in place rather than duplicating rows."""
    conn = DB.connect()
    DB.init_db(conn)
    t0 = time.time()

    if verbose:
        print(f"[brain] loading {C.EMBED_MODEL} ...")
    model = _get_model()

    select_cols = "n.id, n.title, n.summary, n.body_excerpt, n.content_hash, n.prong, n.owner, n.type, n.path, n.metadata"
    requested = list(dict.fromkeys(node_ids)) if node_ids is not None else None
    if requested == []:
        conn.close()
        return {"embedded": 0, "elapsed_sec": round(time.time() - t0, 2)}
    conditions = [] if force else ["e.node_id IS NULL"]
    if requested is not None:
        conditions.append("n.id IN (" + ",".join("?" for _ in requested) + ")")
    join = "" if force else " LEFT JOIN embeddings e ON e.node_id = n.id"
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(f"SELECT {select_cols} FROM nodes n{join}{where}", requested or []).fetchall()
    from .source_integrity import filter_retrieval
    rows = filter_retrieval(conn, [dict(r) for r in rows])
    if limit:
        rows = rows[:limit]

    if verbose:
        print(f"[brain] {len(rows)} nodes need embeddings")

    ids = [r["id"] for r in rows]
    texts = [_text_for_embedding(r) or r["id"] for r in rows]

    inserted = 0
    batch = C.EMBED_BATCH_SIZE
    asserted_at = now_iso()
    for i in range(0, len(texts), batch):
        chunk_texts = texts[i:i+batch]
        chunk_ids = ids[i:i+batch]
        vecs = model.encode(chunk_texts, batch_size=batch, show_progress_bar=False,
                             convert_to_numpy=True, normalize_embeddings=True)
        for nid, v in zip(chunk_ids, vecs):
            blob = v.astype(np.float32).tobytes()
            DB.upsert_embedding(conn, nid, blob, len(v), asserted_at)
            inserted += 1
        if verbose and (i // batch) % 20 == 0:
            elapsed = time.time() - t0
            print(f"[brain] embeddings: {i + len(chunk_texts)}/{len(texts)} | {elapsed:.1f}s")
        if (i // batch) % 50 == 0:
            conn.commit()
    conn.commit()
    elapsed = time.time() - t0
    if verbose:
        print(f"[brain] embedded {inserted} nodes in {elapsed:.1f}s")
    conn.close()
    return {"embedded": inserted, "elapsed_sec": round(elapsed, 2)}


def derive_semantic_edges(verbose: bool = True, threshold: float | None = None,
                          top_k: int | None = None) -> int:
    """For every embedded node, emit top-K semantically_similar_to edges."""
    from sklearn.neighbors import NearestNeighbors
    threshold = threshold if threshold is not None else C.SEMANTIC_SIM_THRESHOLD
    top_k = top_k if top_k is not None else C.SEMANTIC_TOP_K

    conn = DB.connect()
    t0 = time.time()
    rows = conn.execute("SELECT node_id, dim, vector FROM embeddings").fetchall()
    if not rows:
        if verbose:
            print("[brain] no embeddings present; run embed_all() first")
        return 0
    n = len(rows)
    dim = rows[0]["dim"]
    if verbose:
        print(f"[brain] computing top-{top_k} semantic neighbors for {n} embedded nodes (threshold={threshold})")
    ids = [r["node_id"] for r in rows]
    mat = np.empty((n, dim), dtype=np.float32)
    for i, r in enumerate(rows):
        mat[i] = np.frombuffer(r["vector"], dtype=np.float32)

    # Cosine sim against normalized vectors == dot product == 1 - cosine_distance.
    nn = NearestNeighbors(n_neighbors=min(top_k + 1, n), metric="cosine", algorithm="brute")
    nn.fit(mat)
    dists, idxs = nn.kneighbors(mat)
    asserted_at = now_iso()
    inserted = 0
    for i in range(n):
        for d, j in zip(dists[i], idxs[i]):
            if j == i:
                continue
            sim = 1.0 - float(d)
            if sim < threshold:
                continue
            edge = {
                "from": ids[i], "to": ids[j],
                "predicate": "semantically_similar_to", "family": "semantic",
                "weight": round(sim, 4), "confidence": round(sim, 4),
                "asserted_by": "EMBEDDING_MINILM", "asserted_at": asserted_at,
                "evidence_span": f"cosine={sim:.4f}", "metadata": {},
            }
            if DB.insert_edge(conn, edge):
                inserted += 1
    conn.commit()
    elapsed = time.time() - t0
    if verbose:
        print(f"[brain] semantic edges inserted: {inserted} in {elapsed:.1f}s")
    conn.close()
    return inserted


def rescue_orphans(verbose: bool = True, top_k: int = 3, floor: float = 0.40) -> dict:
    """Connect edgeless nodes to their nearest semantic neighbors, ignoring the
    global SEMANTIC_SIM_THRESHOLD. An orphan is unreachable in graph BFS; a weak
    edge restores retrievability. Edges are tagged EMBEDDING_MINILM_RESCUE so
    COVENANT can audit or prune them separately from threshold-grade edges."""
    from sklearn.neighbors import NearestNeighbors
    conn = DB.connect()
    t0 = time.time()
    orphans = DB.orphan_ids(conn)
    if not orphans:
        if verbose:
            print("[brain] no orphans to rescue")
        conn.close()
        return {"orphans": 0, "rescued": 0, "edges": 0, "remaining": 0}

    rows = conn.execute("SELECT node_id, dim, vector FROM embeddings").fetchall()
    ids = [r["node_id"] for r in rows]
    dim = rows[0]["dim"]
    mat = np.empty((len(rows), dim), dtype=np.float32)
    for i, r in enumerate(rows):
        mat[i] = np.frombuffer(r["vector"], dtype=np.float32)
    idx_of = {nid: i for i, nid in enumerate(ids)}

    targets = [(nid, idx_of[nid]) for nid in orphans if nid in idx_of]
    if verbose:
        print(f"[brain] rescuing {len(targets)} orphan(s); top-{top_k} neighbors, floor={floor}")
    nn = NearestNeighbors(n_neighbors=min(top_k + 1, len(rows)), metric="cosine", algorithm="brute")
    nn.fit(mat)
    dists, idxs = nn.kneighbors(mat[[i for _, i in targets]])

    asserted_at = now_iso()
    inserted = 0
    rescued = 0
    for (nid, i), drow, jrow in zip(targets, dists, idxs):
        got = False
        for d, j in zip(drow, jrow):
            if j == i:
                continue
            sim = 1.0 - float(d)
            if sim < floor:
                continue
            for a, b in ((ids[i], ids[j]), (ids[j], ids[i])):
                if DB.insert_edge(conn, {
                    "from": a, "to": b,
                    "predicate": "semantically_similar_to", "family": "semantic",
                    "weight": round(sim, 4), "confidence": round(sim, 4),
                    "asserted_by": "EMBEDDING_MINILM_RESCUE", "asserted_at": asserted_at,
                    "evidence_span": f"orphan-rescue cosine={sim:.4f}", "metadata": {},
                }):
                    inserted += 1
            got = True
        if got:
            rescued += 1
    conn.commit()
    remaining = len(DB.orphan_ids(conn))
    conn.close()
    elapsed = time.time() - t0
    if verbose:
        print(f"[brain] rescued {rescued}/{len(targets)} orphan(s) with {inserted} edges; "
              f"{remaining} still orphaned in {elapsed:.1f}s")
    return {"orphans": len(targets), "rescued": rescued, "edges": inserted,
            "remaining": remaining, "elapsed_sec": round(elapsed, 2)}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    emb = sub.add_parser("embed")
    emb.add_argument("--limit", type=int, default=None)
    emb.add_argument("--force", action="store_true",
                      help="re-embed every node, not just ones lacking a vector "
                           "(needed after a _text_for_embedding input change)")
    sim = sub.add_parser("sim")
    sim.add_argument("--threshold", type=float, default=None)
    sim.add_argument("--top-k", type=int, default=None)
    rsc = sub.add_parser("rescue")
    rsc.add_argument("--top-k", type=int, default=3)
    rsc.add_argument("--floor", type=float, default=0.40)
    args = ap.parse_args()
    if args.cmd == "embed":
        print(embed_all(limit=args.limit, force=args.force))
    elif args.cmd == "sim":
        n = derive_semantic_edges(threshold=args.threshold, top_k=args.top_k)
        print(f"semantic_edges_inserted={n}")
    elif args.cmd == "rescue":
        print(rescue_orphans(top_k=args.top_k, floor=args.floor))


if __name__ == "__main__":
    main()
