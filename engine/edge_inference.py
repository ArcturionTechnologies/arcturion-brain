"""Typed-edge inference + resolution (Brain v2 Phase 3b).

Runs after inverse-derivation in graph_builder.reindex. Two jobs:

  A. RESOLUTION (the load-bearing fix) — re-point dangling typed edges whose
     `to_id` is a bare string ("ALTAIR") at the real node id ("agent_altair").
     Resolution makes matching typed edges traversable.
     A resolved twin edge is inserted (asserted_by='INFERRED_RESOLVE'); the
     original is left intact so provenance/rollback is clean.

  B. INFERENCE — derive new high-value typed edges from signals that already
     exist (no human frontmatter). Shipped: validated_by (doc/code → its test).
     Scaffolded (off until verified): routes_to, depends_on, derived_from.

All inserts: asserted_by='INFERRED_<RULE>', confidence ≤0.85, metadata.inferred=True,
predicate validated against the active canon, idempotent (UNIQUE constraint),
fully reversible:  DELETE FROM edges WHERE asserted_by LIKE 'INFERRED_%';

Run standalone:  python3 -m engine.maintenance... (invoked from graph_builder.reindex)
"""
from __future__ import annotations

import sys

from . import db as DB
from . import resolver as RES
from . import util as U

try:
    from . import validator as V
    _ACTIVE = getattr(V, "ACTIVE_PREDICATE_CANON", None) or getattr(V, "PREDICATE_CANON", None)
except Exception:
    _ACTIVE = None

# Predicates whose endpoint is meant to be a real node — safe to resolve string→id.
RESOLVABLE_PREDICATES = (
    "owned_by", "owns", "authored_by", "maintained_by", "maintains",
    "requires_approval_from", "routes_to", "implemented_by", "validated_by",
    "derived_from", "source_of",
)

_FAMILY = {
    "owned_by": "structure", "owns": "structure", "authored_by": "structure",
    "maintained_by": "structure", "maintains": "structure",
    "requires_approval_from": "operational", "routes_to": "operational",
    "implemented_by": "operational", "validated_by": "operational",
    "derived_from": "evidence", "source_of": "evidence",
}


def _ok(pred: str) -> bool:
    return _ACTIVE is None or pred in _ACTIVE


def _emit(conn, from_id, to_id, predicate, rule, confidence):
    if from_id == to_id or not _ok(predicate):
        return False
    return DB.insert_edge(conn, {
        "from": from_id, "to": to_id, "predicate": predicate,
        "family": _FAMILY.get(predicate, "structure"),
        "weight": confidence, "confidence": confidence,
        "asserted_by": f"INFERRED_{rule}", "asserted_at": U.now_iso(),
        "evidence_span": "", "metadata": {"inferred": True, "rule": rule},
    })


def resolve_dangling(conn, R: RES.Resolver) -> dict:
    """Job A — re-point dangling typed edges at real node ids."""
    ids = R._ids
    counts: dict[str, int] = {}
    for pred in RESOLVABLE_PREDICATES:
        rows = conn.execute(
            "SELECT DISTINCT from_id, to_id FROM edges WHERE predicate = ?", (pred,)
        ).fetchall()
        made = 0
        for r in rows:
            from_id, to_id = r["from_id"], r["to_id"]
            if to_id in ids:
                continue  # already resolved
            nid = R.resolve(to_id)
            if nid and nid in ids and nid != to_id and from_id in ids:
                if _emit(conn, from_id, nid, pred, "RESOLVE", 0.85):
                    made += 1
        if made:
            counts[pred] = made
            conn.commit()
    return counts


def infer_validated_by(conn, R: RES.Resolver) -> int:
    """Job B1 — a node validated by its test file: <stem>.py ⟶ test_<stem>.py.

    Conservative: only when a `test_<stem>.py` node exists and the subject node
    is code/document. Emits  subject --validated_by--> test_node."""
    ids = R._ids
    # index test nodes by stem (test_foo.py -> foo)
    tests = {}
    for r in conn.execute(
        "SELECT id, title, path FROM nodes WHERE family='code' AND "
        "(title LIKE 'test_%' OR path LIKE '%/test_%' OR path LIKE '%/tests/%')"
    ).fetchall():
        stem = (r["title"] or "").removeprefix("test_").removesuffix(".py").lower()
        if stem:
            tests.setdefault(stem, r["id"])
    made = 0
    for r in conn.execute(
        "SELECT id, title FROM nodes WHERE family IN ('code','document')"
    ).fetchall():
        stem = (r["title"] or "").removesuffix(".py").lower()
        tnode = tests.get(stem)
        if tnode and tnode != r["id"] and tnode in ids:
            if _emit(conn, r["id"], tnode, "validated_by", "TEST", 0.75):
                made += 1
    if made:
        conn.commit()
    return made


def run(conn=None, verbose: bool = True) -> dict:
    own = conn is None
    if own:
        conn = DB.connect()
    R = RES.build_resolution_index(conn)
    resolved = resolve_dangling(conn, R)
    validated = infer_validated_by(conn, R)
    if own:
        conn.close()
    summary = {"resolved": resolved, "resolved_total": sum(resolved.values()),
               "validated_by": validated}
    if verbose:
        print(f"[edge_inference] resolved dangling typed edges: {summary['resolved_total']} "
              f"({resolved}); validated_by: {validated}")
    return summary


if __name__ == "__main__":
    run()
    sys.exit(0)
