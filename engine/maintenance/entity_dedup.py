"""Entity dedup pass — merge near-duplicate concept entities (Item 4 of 17).

For all `family='concept'` nodes with embeddings, build cosine similarity matrix;
for pairs with cosine ≥THRESHOLD AND name overlap (substring or difflib ratio ≥0.7):

  1. Canonical = oldest by `created`.
  2. Reparent every edge `(*, predicate, loser)` → `(*, predicate, canonical)`.
  3. Reparent every edge `(loser, predicate, *)` → `(canonical, predicate, *)`.
  4. Drop self-loops created by the reparent.
  5. Insert `(loser, superseded_by, canonical, family=lifecycle)`.
  6. Mark loser `status='superseded'`.
  7. Append the alias pair to `ENTITY_ALIASES.md` under
     `## Auto-Discovered (pending Founder review)`.

Run:
    python3 -m engine.maintenance.entity_dedup [--dry-run] [--threshold 0.95]

Wired into brain-reindex.sh as Phase 2c (post-harvest, pre-embed). Idempotent.
"""
from __future__ import annotations

import argparse
import difflib
import sys
from datetime import datetime, timezone

from .. import config as C
from .. import db as DB
from ..util import now_iso

DEFAULT_THRESHOLD = 0.95
NAME_RATIO_THRESHOLD = 0.70
AUTO_DISCOVERED_HEADER = "## Auto-Discovered (pending Founder review)"


def _load_concept_nodes_with_embeddings(conn) -> tuple[list[str], list[str], list[str], list[str], np.ndarray]:
    """Return (ids, titles, summaries, created_strs, embeddings_matrix)."""
    import numpy as np
    rows = conn.execute(
        """SELECT n.id, n.title, n.summary, n.created, e.vector, e.dim
           FROM nodes n
           JOIN embeddings e ON e.node_id = n.id
           WHERE n.family = 'concept' AND n.status != 'superseded'
        """
    ).fetchall()
    if not rows:
        return [], [], [], [], np.empty((0, 0), dtype=np.float32)
    ids = [r["id"] for r in rows]
    titles = [r["title"] or "" for r in rows]
    summaries = [r["summary"] or "" for r in rows]
    created = [r["created"] or "" for r in rows]
    dim = rows[0]["dim"]
    mat = np.zeros((len(rows), dim), dtype=np.float32)
    for i, r in enumerate(rows):
        mat[i] = np.frombuffer(r["vector"], dtype=np.float32)
    # Normalize for cosine-via-dot-product.
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat = mat / norms
    return ids, titles, summaries, created, mat


def _name_overlap(a: str, b: str) -> bool:
    al, bl = a.strip().lower(), b.strip().lower()
    if not al or not bl:
        return False
    if al == bl:
        return True
    if al in bl or bl in al:
        return True
    ratio = difflib.SequenceMatcher(None, al, bl).ratio()
    return ratio >= NAME_RATIO_THRESHOLD


def _pick_canonical(i: int, j: int, created: list[str]) -> tuple[int, int]:
    """Return (canonical_idx, loser_idx). Oldest `created` wins; fallback to lower idx."""
    ci, cj = created[i] or "9999", created[j] or "9999"
    if ci <= cj:
        return i, j
    return j, i


def _find_pairs(ids, titles, mat, threshold: float) -> list[tuple[int, int, float]]:
    """Return (i, j, cosine_sim) for unique pairs above the threshold + name overlap."""
    if len(ids) < 2:
        return []
    sim = mat @ mat.T
    # Mask diagonal and lower triangle
    n = len(ids)
    pairs: list[tuple[int, int, float]] = []
    for i in range(n):
        for j in range(i + 1, n):
            s = float(sim[i, j])
            if s < threshold:
                continue
            if not _name_overlap(titles[i], titles[j]):
                continue
            pairs.append((i, j, s))
    pairs.sort(key=lambda p: -p[2])  # highest similarity first
    return pairs


def _reparent_edges(conn, loser: str, canonical: str) -> dict:
    """Reparent edges, drop self-loops, return counts."""
    out = {"in_reparented": 0, "out_reparented": 0, "self_loops_dropped": 0}
    # to_id rewrites
    cur = conn.execute(
        "UPDATE OR IGNORE edges SET to_id=? WHERE to_id=?",
        (canonical, loser),
    )
    out["in_reparented"] = cur.rowcount or 0
    # from_id rewrites
    cur = conn.execute(
        "UPDATE OR IGNORE edges SET from_id=? WHERE from_id=?",
        (canonical, loser),
    )
    out["out_reparented"] = cur.rowcount or 0
    # Drop any conflicting edges that UPDATE OR IGNORE skipped (UNIQUE constraint)
    # by re-pointing loser_id directly:
    conn.execute("DELETE FROM edges WHERE from_id=? OR to_id=?", (loser, loser))
    # Drop self-loops created by the merge
    cur = conn.execute("DELETE FROM edges WHERE from_id = to_id")
    out["self_loops_dropped"] = cur.rowcount or 0
    return out


def _emit_supersession_edge(conn, loser: str, canonical: str, ts: str) -> None:
    DB.insert_edge(conn, {
        "from": loser, "to": canonical, "predicate": "superseded_by",
        "family": "lifecycle", "weight": 1.0, "confidence": 1.0,
        "asserted_by": "entity_dedup", "asserted_at": ts,
        "evidence_span": f"entity_dedup near-duplicate merge {ts}", "metadata": {},
    })


def _mark_superseded(conn, loser: str) -> None:
    conn.execute("UPDATE nodes SET status='superseded' WHERE id=?", (loser,))


def _append_alias_record(records: list[tuple[str, str]]) -> int:
    """Append discovered alias pairs to ENTITY_ALIASES.md, idempotent."""
    if not records:
        return 0
    p = C.ENTITY_ALIASES
    if not p.exists():
        return 0
    text = p.read_text(encoding="utf-8")
    if AUTO_DISCOVERED_HEADER not in text:
        text = text.rstrip() + f"\n\n{AUTO_DISCOVERED_HEADER}\n\n"
    new_lines = []
    for canonical, alias in records:
        line = f"- canonical: `{canonical}` · alias: `{alias}` (auto, {now_iso()[:10]})"
        if line not in text:
            new_lines.append(line)
    if not new_lines:
        return 0
    text = text.rstrip() + "\n" + "\n".join(new_lines) + "\n"
    p.write_text(text, encoding="utf-8")
    return len(new_lines)


def run(threshold: float = DEFAULT_THRESHOLD, dry_run: bool = False) -> dict:
    conn = DB.connect()
    try:
        ids, titles, summaries, created, mat = _load_concept_nodes_with_embeddings(conn)
        if not ids:
            return {"concepts_loaded": 0, "pairs": 0, "merged": 0, "alias_lines": 0}

        pairs = _find_pairs(ids, titles, mat, threshold)
        if dry_run:
            sample = [
                {"a": ids[i], "b": ids[j], "cos": round(s, 3),
                 "title_a": titles[i], "title_b": titles[j]}
                for i, j, s in pairs[:20]
            ]
            return {
                "concepts_loaded": len(ids),
                "pairs": len(pairs),
                "merged": 0,
                "alias_lines": 0,
                "sample": sample,
            }

        merged_ids: set[str] = set()
        alias_records: list[tuple[str, str]] = []
        merge_log: list[dict] = []
        ts = now_iso()

        for i, j, s in pairs:
            if ids[i] in merged_ids or ids[j] in merged_ids:
                continue
            ci, li = _pick_canonical(i, j, created)
            canonical_id, loser_id = ids[ci], ids[li]
            canonical_title, loser_title = titles[ci], titles[li]
            edge_counts = _reparent_edges(conn, loser_id, canonical_id)
            _emit_supersession_edge(conn, loser_id, canonical_id, ts)
            _mark_superseded(conn, loser_id)
            merged_ids.add(loser_id)
            alias_records.append((canonical_id, loser_title))
            merge_log.append({
                "canonical": canonical_id,
                "loser": loser_id,
                "cosine": round(s, 3),
                **edge_counts,
            })

        conn.commit()
        alias_lines = _append_alias_record(alias_records)
        return {
            "concepts_loaded": len(ids),
            "pairs": len(pairs),
            "merged": len(merge_log),
            "alias_lines": alias_lines,
            "log": merge_log,
        }
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help=f"Cosine threshold (default {DEFAULT_THRESHOLD}).")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    result = run(threshold=args.threshold, dry_run=args.dry_run)
    label = "[dry-run]" if args.dry_run else "[entity_dedup]"
    print(f"\n{label} concept_nodes={result['concepts_loaded']} "
          f"pairs={result['pairs']} merged={result['merged']} "
          f"alias_lines={result['alias_lines']}\n")
    if args.dry_run and result.get("sample"):
        for s in result["sample"]:
            print(f"  • cos={s['cos']} {s['title_a']!r} ⇄ {s['title_b']!r}")
            print(f"    {s['a']} ⇄ {s['b']}")
    if not args.dry_run and result.get("log"):
        for m in result["log"][:20]:
            print(f"  ✅ canonical={m['canonical']} loser={m['loser']} cos={m['cosine']} "
                  f"in={m['in_reparented']} out={m['out_reparented']} loops_dropped={m['self_loops_dropped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
