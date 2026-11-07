"""Prong backfill + rename (Brain v2 Phase 2).

Two jobs, both idempotent:
  1. Rename legacy prong value 'universe' → 'hq' (ARCTURION_UNIVERSE→ARCTURION_HQ).
  2. Backfill NULL/blank prongs from each node's stored (universe-relative) path via
     config.prong_for_relpath — the 135k pre-prong-column rows.

Wired into brain-reindex.sh as a self-healing pass so the gap can never reopen.

Run:
    python3 -m engine.maintenance.prong_backfill            # apply
    python3 -m engine.maintenance.prong_backfill --dry-run  # report only
"""
from __future__ import annotations

import argparse
import sys

from .. import db as DB
from .. import config as C

BATCH = 5000


def _dist(conn) -> dict:
    return {
        (r["prong"] if r["prong"] is not None else "NULL"): r["c"]
        for r in conn.execute(
            "SELECT prong, COUNT(*) c FROM nodes GROUP BY prong"
        ).fetchall()
    }


def run(dry_run: bool = False) -> dict:
    conn = DB.connect()
    before = _dist(conn)

    # 1. rename universe → hq
    universe_n = conn.execute(
        "SELECT COUNT(*) c FROM nodes WHERE prong = 'universe'"
    ).fetchone()["c"]
    if not dry_run and universe_n:
        conn.execute("UPDATE nodes SET prong = 'hq' WHERE prong = 'universe'")
        conn.commit()

    # 2. backfill NULL/blank prongs from stored path
    rows = conn.execute(
        "SELECT id, path FROM nodes WHERE prong IS NULL OR prong = ''"
    ).fetchall()
    null_n = len(rows)
    updated = 0
    if not dry_run and rows:
        batch = []
        for r in rows:
            prong = C.prong_for_relpath(r["path"] or "")
            batch.append((prong, r["id"]))
            if len(batch) >= BATCH:
                conn.executemany("UPDATE nodes SET prong = ? WHERE id = ?", batch)
                conn.commit()
                updated += len(batch)
                batch = []
        if batch:
            conn.executemany("UPDATE nodes SET prong = ? WHERE id = ?", batch)
            conn.commit()
            updated += len(batch)

    after = _dist(conn)
    conn.close()
    return {
        "dry_run": dry_run,
        "renamed_universe_to_hq": universe_n,
        "null_prong_rows": null_n,
        "backfilled": updated,
        "before": before,
        "after": after,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    res = run(dry_run=args.dry_run)
    print(f"prong_backfill (dry_run={res['dry_run']}):")
    print(f"  universe→hq : {res['renamed_universe_to_hq']}")
    print(f"  NULL prongs : {res['null_prong_rows']}  backfilled={res['backfilled']}")
    print(f"  before      : {res['before']}")
    print(f"  after       : {res['after']}")
    # success when no NULL/universe remain (after a real run)
    bad = res["after"].get("NULL", 0) + res["after"].get("universe", 0)
    if not args.dry_run and bad:
        print(f"  WARNING: {bad} rows still NULL/universe", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
