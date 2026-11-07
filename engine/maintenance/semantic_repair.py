"""Bounded, reversible semantic repair for the live Brain edge store.

This module intentionally handles only two evidence-backed legacy classes:

1. active edges whose predicate is outside ACTIVE_PREDICATE_CANON; and
2. domain/range violations caused by exact agent-name resolution to a document
   node instead of the already-materialized canonical agent node.

Every removed active row is first copied, in full, to
``edge_admission_quarantine`` in the same transaction.  A snapshot is required
by the CLI before either mutation class.  No node is deleted or merged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .. import config as C
from .. import db as DB
from .. import validator as V
from ..util import now_iso

REPAIR_ASSERTION_SUFFIX = "+SEMANTIC_REPAIR_P3"
FORWARD_AGENT_PREDICATES = frozenset({"owned_by", "authored_by", "maintained_by"})
INVERSE_AGENT_PREDICATES = frozenset({"owns", "authored", "maintains"})


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _edge_payload(row: sqlite3.Row) -> dict[str, Any]:
    payload = _row_dict(row)
    payload["from"] = payload["from_id"]
    payload["to"] = payload["to_id"]
    payload["quarantined_at"] = now_iso()
    payload["quarantined_by"] = "engine.maintenance.semantic_repair"
    return payload


def _quarantine_existing(conn: sqlite3.Connection, row: sqlite3.Row, reason: str) -> None:
    """Copy an existing edge into quarantine without an implicit transaction commit."""
    payload = _edge_payload(row)
    conn.execute(
        """INSERT INTO edge_admission_quarantine
           (from_id, to_id, predicate, asserted_by, reason, payload_json)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(from_id, to_id, predicate, asserted_by, reason)
           DO UPDATE SET payload_json=excluded.payload_json,
                         last_seen=CURRENT_TIMESTAMP,
                         times=edge_admission_quarantine.times+1""",
        (
            row["from_id"], row["to_id"], row["predicate"],
            row["asserted_by"] or "<unknown>", reason,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        ),
    )


def noncanonical_edges(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in V.ACTIVE_PREDICATE_CANON)
    return conn.execute(
        f"SELECT * FROM edges WHERE predicate NOT IN ({placeholders}) ORDER BY id",
        tuple(sorted(V.ACTIVE_PREDICATE_CANON)),
    ).fetchall()


def domain_range_violations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return resolvable active edge rows that violate the modeled endpoint rules."""
    rows = conn.execute(
        """SELECT e.*, nf.family AS from_family, nf.title AS from_title,
                         nt.family AS to_family, nt.title AS to_title
           FROM edges e
           JOIN nodes nf ON nf.id=e.from_id
           JOIN nodes nt ON nt.id=e.to_id
           ORDER BY e.id"""
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        codes: list[str] = []
        rule = V.DOMAIN_RANGE.get(r["predicate"])
        if rule:
            domain = rule.get("domain")
            rng = rule.get("range")
            if domain is not None and r["from_family"] not in domain:
                codes.append("DOMAIN_MISMATCH")
            if rng is not None and r["to_family"] not in rng:
                codes.append("RANGE_MISMATCH")
        if (r["predicate"] in V.SAME_FAMILY_PREDICATES
                and r["from_family"] != r["to_family"]):
            codes.append("SAME_FAMILY_MISMATCH")
        if codes:
            d = _row_dict(r)
            d["violation_codes"] = codes
            out.append(d)
    return out


def _unique_agent_title_map(conn: sqlite3.Connection) -> dict[str, str]:
    found: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT id, title FROM nodes WHERE family='agent' AND title IS NOT NULL"
    ):
        found.setdefault(r["title"].strip().casefold(), []).append(r["id"])
    return {title: ids[0] for title, ids in found.items() if len(set(ids)) == 1}


def _planned_agent_repairs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    agent_by_title = _unique_agent_title_map(conn)
    plans: list[dict[str, Any]] = []
    for v in domain_range_violations(conn):
        new_from, new_to = v["from_id"], v["to_id"]
        reason = "legacy_domain_range_violation:unresolved"
        action = "quarantine_only"
        if v["predicate"] in FORWARD_AGENT_PREDICATES and v["to_family"] == "document":
            canonical = agent_by_title.get((v["to_title"] or "").strip().casefold())
            if canonical:
                new_to = canonical
                action = "correct_agent_endpoint"
                reason = "legacy_domain_range_violation:agent_label_document_endpoint"
        elif v["predicate"] in INVERSE_AGENT_PREDICATES and v["from_family"] == "document":
            canonical = agent_by_title.get((v["from_title"] or "").strip().casefold())
            if canonical:
                new_from = canonical
                action = "correct_agent_endpoint"
                reason = "legacy_domain_range_violation:agent_label_document_endpoint"
        elif "SAME_FAMILY_MISMATCH" in v["violation_codes"]:
            reason = "legacy_domain_range_violation:same_family_mismatch"

        plans.append({
            "row": v,
            "action": action,
            "reason": reason,
            "new_from": new_from,
            "new_to": new_to,
        })
    return plans


def snapshot_database(conn: sqlite3.Connection, destination: Path) -> dict[str, Any]:
    """Create and verify a consistent SQLite backup before a mutation class."""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite snapshot: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(destination)
    try:
        conn.backup(out, pages=8192)
        quick = out.execute("PRAGMA quick_check").fetchone()[0]
        integrity = out.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {
            "nodes": out.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
            "edges": out.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
            "quarantine": out.execute(
                "SELECT COUNT(*) FROM edge_admission_quarantine"
            ).fetchone()[0],
        }
    finally:
        out.close()
    h = hashlib.sha256()
    with destination.open("rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    return {
        "path": str(destination), "sha256": h.hexdigest(),
        "bytes": destination.stat().st_size,
        "quick_check": quick, "integrity_check": integrity, **counts,
    }


def quarantine_noncanonical(conn: sqlite3.Connection, apply: bool = False) -> dict[str, Any]:
    rows = noncanonical_edges(conn)
    by_predicate: dict[str, int] = {}
    for r in rows:
        by_predicate[r["predicate"]] = by_predicate.get(r["predicate"], 0) + 1
    result: dict[str, Any] = {
        "candidates": len(rows), "by_predicate": by_predicate, "applied": apply,
        "quarantined": 0, "removed_from_active": 0,
    }
    if not apply or not rows:
        return result

    conn.execute("BEGIN IMMEDIATE")
    try:
        for r in rows:
            reason = (
                "legacy_retired_predicate"
                if r["predicate"] in V.RETIRED_PREDICATES
                else "legacy_unknown_predicate"
            )
            _quarantine_existing(conn, r, reason)
            conn.execute("DELETE FROM edges WHERE id=?", (r["id"],))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    result["quarantined"] = len(rows)
    result["removed_from_active"] = len(rows)
    return result


def repair_agent_endpoints(conn: sqlite3.Connection, apply: bool = False) -> dict[str, Any]:
    plans = _planned_agent_repairs(conn)
    correctable = sum(p["action"] == "correct_agent_endpoint" for p in plans)
    quarantine_only = len(plans) - correctable
    result: dict[str, Any] = {
        "candidates": len(plans), "correctable": correctable,
        "quarantine_only": quarantine_only, "applied": apply,
        "corrected_edges_inserted": 0, "quarantined": 0,
        "removed_from_active": 0,
    }
    if not apply or not plans:
        return result

    inserted = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for p in plans:
            r = p["row"]
            _quarantine_existing(conn, r, p["reason"])
            if p["action"] == "correct_agent_endpoint":
                try:
                    metadata = json.loads(r["metadata"] or "{}")
                except (TypeError, ValueError):
                    metadata = {"legacy_metadata": r["metadata"]}
                metadata["semantic_repair"] = {
                    "original_edge_id": r["id"],
                    "original_from_id": r["from_id"],
                    "original_to_id": r["to_id"],
                    "reason": p["reason"],
                }
                edge = {
                    "from": p["new_from"], "to": p["new_to"],
                    "predicate": r["predicate"], "family": r["family"],
                    "weight": r["weight"], "confidence": r["confidence"],
                    "asserted_by": f"{r['asserted_by'] or 'UNKNOWN'}{REPAIR_ASSERTION_SUFFIX}",
                    "asserted_at": r["asserted_at"] or now_iso(),
                    "evidence_span": r["evidence_span"], "metadata": metadata,
                    "asserted_in_session": r["asserted_in_session"],
                }
                if DB.insert_edge(conn, edge):
                    inserted += 1
            conn.execute("DELETE FROM edges WHERE id=?", (r["id"],))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    result.update({
        "corrected_edges_inserted": inserted, "quarantined": len(plans),
        "removed_from_active": len(plans),
    })
    return result


def verify(conn: sqlite3.Connection) -> dict[str, Any]:
    noncanon = noncanonical_edges(conn)
    violations = domain_range_violations(conn)
    quick = conn.execute("PRAGMA quick_check").fetchone()[0]
    result = {
        "ok": not noncanon and not violations and quick == "ok",
        "noncanonical_active_edges": len(noncanon),
        "domain_range_violations": len(violations),
        "quarantine_rows": conn.execute(
            "SELECT COUNT(*) FROM edge_admission_quarantine"
        ).fetchone()[0],
        "quick_check": quick,
        "edge_health": DB.edge_health(conn),
    }
    return result


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Bounded reversible semantic edge repair")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply-noncanonical", action="store_true")
    group.add_argument("--apply-agent-endpoints", action="store_true")
    group.add_argument("--verify", action="store_true")
    group.add_argument("--plan-noncanonical", action="store_true")
    group.add_argument("--plan-agent-endpoints", action="store_true")
    ap.add_argument("--snapshot", type=Path)
    ap.add_argument("--receipt", type=Path)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    conn = DB.connect()
    try:
        before = DB.stats(conn)
        if args.verify:
            result = verify(conn)
        elif args.plan_noncanonical:
            result = quarantine_noncanonical(conn, apply=False)
        elif args.plan_agent_endpoints:
            result = repair_agent_endpoints(conn, apply=False)
        else:
            if args.snapshot is None or args.receipt is None:
                ap.error("apply modes require --snapshot and --receipt")
            snap = snapshot_database(conn, args.snapshot)
            if snap["quick_check"] != "ok" or snap["integrity_check"] != "ok":
                raise RuntimeError(f"snapshot verification failed: {snap}")
            if args.apply_noncanonical:
                mutation = quarantine_noncanonical(conn, apply=True)
            else:
                mutation = repair_agent_endpoints(conn, apply=True)
            result = {
                "started_at": now_iso(), "snapshot": snap, "before": before,
                "mutation": mutation, "after": DB.stats(conn),
                "verification": verify(conn),
                "rollback": (
                    "Stop Brain writers and restore the class snapshot over the live DB; "
                    "the snapshot was integrity-checked before mutation."
                ),
                "finished_at": now_iso(),
            }
            _write_receipt(args.receipt, result)
    finally:
        conn.close()

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if (not args.verify or result["ok"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
