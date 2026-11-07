"""Machine-readable walkable-predicate manifest — the Brain/COVENANT interface.

Brain owns predicate definition + materialization (`engine/ontology.py` is the
single source of truth for the vocabulary itself — domain/range/inverse/
transitivity axioms). COVENANT owns reasoning over that vocabulary
(`docs/plans/CARDS-neurosymbolic.md`). This module is the seam: a live-graph
FACT layer answering the one question a reasoner needs before it walks a
predicate — "does at least one asserted edge under this predicate currently
have both endpoints resolving to a real node?" — separate from whether the
predicate is canonically modeled at all (that's `engine.validator` /
`engine.maintenance.domain_range_audit`).

Read-only: never writes to `edges` or `nodes`. Pairs with
`engine.maintenance.edge_endpoint_resolve`, which performs the (reversible,
additive) endpoint resolution this manifest then reports on.

Usage:
    cd ~/Desktop/ARCTURION_Brain
    python3 -m engine.maintenance.walkable_predicates            # writes the manifest
    python3 -m engine.maintenance.walkable_predicates --no-write # print only
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from .. import config as C
from .. import db as DB
from ..util import now_iso

MANIFEST_FILENAME = "walkable_predicates.json"
SCHEMA_VERSION = "1.0"

SOURCE_OF_TRUTH = (
    "engine/ontology.py (canon/0007_ONTOLOGY.yaml) is the single source of truth "
    "for the PREDICATE VOCABULARY — domain/range/inverse/transitivity axioms. This "
    "manifest is a separate, live-graph FACT layer on top of it: whether at least "
    "one asserted edge under a predicate currently has both endpoints resolving to "
    "a real engine.db nodes row (walkable) or not (dead vocabulary — asserted but "
    "untraversable). Brain owns predicate definition + materialization; COVENANT "
    "consumes this manifest for reasoning, per docs/plans/CARDS-neurosymbolic.md."
)


def _connect_ro(db_path: Path | None = None) -> sqlite3.Connection:
    p = db_path or C.BRAIN_DB
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


_PER_PREDICATE_SQL = """
SELECT e.predicate AS predicate,
       COUNT(*)     AS total,
       SUM(CASE WHEN EXISTS(SELECT 1 FROM nodes n WHERE n.id = e.from_id)
                 AND EXISTS(SELECT 1 FROM nodes n WHERE n.id = e.to_id)
                THEN 1 ELSE 0 END) AS walkable
FROM edges e
GROUP BY e.predicate
ORDER BY e.predicate
"""


def build(db_path: Path | None = None) -> dict[str, Any]:
    conn = _connect_ro(db_path)
    try:
        rows = conn.execute(_PER_PREDICATE_SQL).fetchall()
    finally:
        conn.close()

    predicates: list[dict[str, Any]] = []
    for r in rows:
        total = r["total"]
        ok = r["walkable"] or 0
        predicates.append({
            "predicate": r["predicate"],
            "edge_count": total,
            "resolved_endpoint_count": ok,
            "resolved_endpoint_rate": round(100.0 * ok / total, 2) if total else 0.0,
            "dangling_edge_count": total - ok,
            "walkable": ok > 0,
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_iso(),
        "generated_by": "engine.maintenance.walkable_predicates",
        "source_of_truth": SOURCE_OF_TRUTH,
        "db_snapshot": "index/brain.sqlite",
        "derived_predicates_excluded_from_structural_health": sorted(DB.DERIVED_PREDICATES),
        "predicate_count": len(predicates),
        "walkable_predicate_count": sum(1 for p in predicates if p["walkable"]),
        "zero_walkable_predicate_count": sum(1 for p in predicates if not p["walkable"]),
        "predicates": predicates,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the walkable-predicate manifest")
    ap.add_argument("--json", action="store_true", help="print JSON to stdout (default prints a summary)")
    ap.add_argument("--no-write", action="store_true", help="skip writing index/walkable_predicates.json")
    args = ap.parse_args()

    manifest = build()
    if args.json or args.no_write:
        print(json.dumps(manifest, indent=2, sort_keys=False))
    else:
        print(f"predicates: {manifest['predicate_count']} "
              f"(walkable: {manifest['walkable_predicate_count']}, "
              f"zero-walkable: {manifest['zero_walkable_predicate_count']})")
    if not args.no_write:
        out = C.GRAPH_DIR / MANIFEST_FILENAME
        out.write_text(json.dumps(manifest, indent=2, sort_keys=False), encoding="utf-8")
        print(f"[wrote] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
