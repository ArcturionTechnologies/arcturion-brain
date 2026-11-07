"""engine.maintenance.skiplist_gc — targeted removal of node classes that must never
be indexed (Leg 1, retrieval-honesty programme, 2026-08-04).

WHY A SECOND GC
---------------
`content_dedup.gc_stale_skiplisted` already sweeps nodes whose path became skip-listed —
but it runs ONLY on a `--full` reindex with a complete walk, and the nightly walker runs
`engine.reindex` without `--full`. So when a skip rule is tightened, the nodes it now
forbids linger indefinitely, even when they should no longer appear in retrieval.

It also cannot be reused as-is for a targeted sweep. Its predicate is
`util.should_skip_path(stored_path)`, and stored paths are universe-relative: the
deliberately-indexed `~/.claude/plans` + `~/.claude/hooks` nodes are stored as
`.claude/…`, which `should_skip_path` rejects on its leading-dot rule. `gc_stale_skiplisted`
survives that only because its `seen_paths` clause protects anything the walk yielded —
protection this module does not have, because it is meant to run WITHOUT a walk.

So this sweep is deliberately narrow and enumerated. It removes exactly three classes,
each of which is unconditionally junk regardless of any walk:

  1. **Brain eval fixtures** (`config.is_brain_fixture_path`) — `engine/eval.py`,
     `engine/retrieval_eval.py`, `engine/tests/**`. A corpus that indexes its own test
     fixtures cannot be tested by them: `eval.py` contained all six golden questions
     verbatim and was the top-1 answer for four of them.
  2. **Git worktree checkouts** (`.claude/worktrees/`) — byte-identical duplicate copies
     of an already-indexed repo.
  3. **Vendored dependency trees** (`.deps/`, `node_modules/`, `site-packages/`) — third
     party code that occupies node slots and matches lexically ("rank", "test", …).

Anything not on that list is left alone for the nightly full pass to handle.

Usage:
    python3 -m engine.maintenance.skiplist_gc            # dry run, prints the plan
    python3 -m engine.maintenance.skiplist_gc --apply    # delete (back up the DB first)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from .. import config as C
from ..util import now_iso

# Path COMPONENTS that mean "vendored third-party tree". Component match (not substring)
# so a legitimately-authored file named e.g. `deps.md` is never caught.
VENDORED_DIRS = {".deps", "node_modules", "site-packages"}

# Substring match — the worktree root can sit under any walked root, so the fragment has
# to match wherever it appears in the stored path.
WORKTREE_FRAGMENT = ".claude/worktrees/"


def classify(path: str | None) -> str | None:
    """Return the sweep class for a stored node path, or None to keep the node."""
    if not path:
        return None
    if C.is_brain_fixture_path(path):
        return "brain_fixture"
    s = str(path).replace("\\", "/")
    if WORKTREE_FRAGMENT in s:
        return "worktree_twin"
    if VENDORED_DIRS & set(Path(s).parts):
        return "vendored_dep"
    return None


def plan(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Group the node IDs to be removed by sweep class. Read-only."""
    buckets: dict[str, list[str]] = {}
    for node_id, path in conn.execute("SELECT id, path FROM nodes WHERE path IS NOT NULL"):
        cls = classify(path)
        if cls:
            buckets.setdefault(cls, []).append(node_id)
    return buckets


def write_recovery_log(conn: sqlite3.Connection, buckets: dict[str, list[str]],
                       path: Path | None = None, db_path: str | None = None) -> Path:
    """Record exactly what is about to be deleted, and flush it to disk BEFORE any DELETE.

    A node row is small; the edges attached to it are not, and the sweep removes both. If a
    sweep turns out to have been wrong, the DB backup is the real remedy — but a backup only
    tells you the state before, never *what this run decided to remove and why*. Reconstructing
    that from a large diff of a substantial storage file is not a recovery procedure anyone will
    actually follow at the time it matters.

    Written, flushed and fsync'd before the transaction opens, so it survives a crash
    mid-DELETE — the one case where knowing the intended set is most valuable."""
    path = Path(path or (C.BRAIN_ROOT / "backups" /
                         f"skiplist_gc_{time.strftime('%Y%m%dT%H%M%S')}.json"))
    # Positional indexing, not r["id"]: callers hand us whatever connection they have and
    # sqlite3's default row_factory returns plain tuples. `plan()` already iterates
    # positionally; this must not be the one place that silently assumes a Row factory.
    rows = {r[0]: r[1] for r in
            conn.execute("SELECT id, path FROM nodes WHERE path IS NOT NULL")}
    payload = {
        "swept": now_iso(),
        "db": str(db_path or C.BRAIN_DB),
        "counts": {k: len(v) for k, v in sorted(buckets.items())},
        "total": sum(len(v) for v in buckets.values()),
        "_note": "Nodes removed by engine.maintenance.skiplist_gc. Edges attached to these "
                 "IDs were removed with them and are NOT listed here — restore from the DB "
                 "backup taken before the sweep, then use this file to confirm the scope.",
        "removed": {cls: [{"id": i, "path": rows.get(i)} for i in sorted(ids)]
                    for cls, ids in sorted(buckets.items())},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(payload, indent=2, ensure_ascii=False))
        f.flush()
        os.fsync(f.fileno())
    return path


def sweep(conn: sqlite3.Connection, apply: bool = False, verbose: bool = True,
          recovery_log: Path | None = None, db_path: str | None = None) -> dict:
    """Delete the enumerated junk classes (nodes + their edges + their FTS rows).

    Returns a summary dict. With `apply=False` nothing is written — the same counting
    pass runs so a dry run and the real run can never disagree about the blast radius.
    Every applied sweep writes a recovery log naming every removed node first; see
    `write_recovery_log`."""
    buckets = plan(conn)
    losers = [i for ids in buckets.values() for i in ids]
    summary = {
        "by_class": {k: len(v) for k, v in sorted(buckets.items())},
        "nodes_removed": 0,
        "edges_removed": 0,
        "applied": bool(apply),
        "candidates": len(losers),
        "recovery_log": None,
    }
    if verbose:
        for cls, n in summary["by_class"].items():
            print(f"[skiplist_gc] {cls:16} {n:>7} nodes")
    if not losers or not apply:
        if verbose and not apply:
            print(f"[skiplist_gc] DRY RUN — {len(losers)} nodes would be removed")
        return summary

    log_path = write_recovery_log(conn, buckets, recovery_log, db_path)
    summary["recovery_log"] = str(log_path)
    if verbose:
        print(f"[skiplist_gc] recovery log → {log_path}")

    conn.execute("DROP TABLE IF EXISTS _leg1gc")
    conn.execute("CREATE TEMP TABLE _leg1gc (id TEXT PRIMARY KEY)")
    conn.executemany("INSERT OR IGNORE INTO _leg1gc (id) VALUES (?)", [(x,) for x in losers])
    cur = conn.execute("DELETE FROM edges WHERE from_id IN (SELECT id FROM _leg1gc) "
                       "OR to_id IN (SELECT id FROM _leg1gc)")
    summary["edges_removed"] = cur.rowcount
    cur = conn.execute("DELETE FROM nodes WHERE id IN (SELECT id FROM _leg1gc)")
    summary["nodes_removed"] = cur.rowcount
    for table, col in (("fts_nodes", "node_id"), ("embeddings", "node_id")):
        try:
            conn.execute(f"DELETE FROM {table} WHERE {col} IN (SELECT id FROM _leg1gc)")
        except sqlite3.Error:
            pass  # optional tables; absence is not an error
    conn.execute("DROP TABLE IF EXISTS _leg1gc")
    conn.commit()
    if verbose:
        print(f"[skiplist_gc] removed {summary['nodes_removed']} nodes, "
              f"{summary['edges_removed']} edges")
    return summary


def verify_backup(path: str) -> tuple[bool, str]:
    """Confirm `path` is a readable SQLite DB with a populated `nodes` table.

    `--apply` requires this rather than a bare "yes I have a backup" flag, because a flag
    only records that someone typed it. The failure this guards against is not carelessness
    in general — it is the specific, common case of a backup that was never actually taken,
    was written to the wrong path, or is a zero-byte file from a `cp` that ran out of disk.
    Each of those types the flag just fine and restores nothing."""
    p = Path(path)
    if not p.exists():
        return False, f"{p} does not exist"
    if p.stat().st_size == 0:
        return False, f"{p} is empty (0 bytes) — a failed copy, not a backup"
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            n = c.execute("SELECT count(*) FROM nodes").fetchone()[0]
        finally:
            c.close()
    except sqlite3.Error as exc:
        return False, f"{p} is not a readable SQLite DB with a nodes table ({exc})"
    if n == 0:
        return False, f"{p} has an empty nodes table — nothing to restore from"
    return True, f"{p} ok ({n} nodes)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default is a dry run). Requires --backup.")
    ap.add_argument("--backup", metavar="PATH",
                    help="path to a DB backup taken BEFORE this sweep. Required with "
                         "--apply and VERIFIED, not just acknowledged: it must be a "
                         "readable SQLite file with a non-empty nodes table.")
    ap.add_argument("--snapshot", action="store_true",
                    help="take the backup now (to backups/) and use it, instead of "
                         "passing --backup for one taken earlier")
    ap.add_argument("--recovery-log", metavar="PATH",
                    help="where to write the list of removed node IDs (default: "
                         "backups/skiplist_gc_<timestamp>.json)")
    ap.add_argument("--db", default=str(C.BRAIN_DB))
    args = ap.parse_args(argv)

    if args.apply:
        if args.snapshot:
            src = Path(args.db)
            dst = (C.BRAIN_ROOT / "backups" /
                   f"brain-pre-skiplist-gc-{time.strftime('%Y%m%dT%H%M%S')}.sqlite")
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Use SQLite's own backup API, not a file copy: the live DB is in WAL mode, so
            # `cp` of the main file alone can capture a torn state that omits committed
            # pages still sitting in the -wal.
            src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
            dst_conn = sqlite3.connect(str(dst))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
                src_conn.close()
            args.backup = str(dst)
            print(f"[skiplist_gc] snapshot → {dst}")
        if not args.backup:
            print("[skiplist_gc] REFUSING to --apply without --backup PATH (or --snapshot).\n"
                  "  This deletes nodes AND every edge attached to them, in one "
                  "irreversible transaction.\n"
                  "  Take a backup first, or pass --snapshot to take one now.",
                  file=sys.stderr)
            return 2
        ok, detail = verify_backup(args.backup)
        if not ok:
            print(f"[skiplist_gc] REFUSING to --apply: backup unusable — {detail}",
                  file=sys.stderr)
            return 2
        print(f"[skiplist_gc] backup verified: {detail}")

    conn = sqlite3.connect(args.db)
    try:
        before = conn.execute("SELECT count(*) FROM nodes").fetchone()[0]
        summary = sweep(conn, apply=args.apply, db_path=args.db,
                        recovery_log=Path(args.recovery_log) if args.recovery_log else None)
        after = conn.execute("SELECT count(*) FROM nodes").fetchone()[0]
        print(f"[skiplist_gc] nodes {before} → {after}")
        if summary["recovery_log"]:
            print(f"[skiplist_gc] recovery log: {summary['recovery_log']}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
