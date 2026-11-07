"""skiplist_gc safety rails (no pytest).

`--apply` deletes nodes AND every edge attached to them in one irreversible transaction —
21,144 nodes and 216,605 edges on its first real run. It was gated by a flag and an advisory
comment. A flag records that someone typed it; it does not establish that a backup exists,
and it leaves nothing behind saying what this particular run decided to remove.

Run:  python3 -m engine.tests.test_skiplist_gc_safety
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from ..maintenance import skiplist_gc as G

REPO = Path(__file__).resolve().parents[2]


def _mkdb(path: Path) -> None:
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, path TEXT)")
    c.execute("CREATE TABLE edges (from_id TEXT, to_id TEXT)")
    c.executemany("INSERT INTO nodes VALUES (?,?)", [
        ("keep", "ALTAIR/000 x/Memory/real.md"),
        ("junk1", "05 T/clis/.claude/worktrees/w/dup.md"),
        ("junk2", "ARCTURION_Brain/engine/eval.py"),
    ])
    c.executemany("INSERT INTO edges VALUES (?,?)", [("keep", "junk1"), ("junk1", "junk2")])
    c.commit()
    c.close()


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "engine.maintenance.skiplist_gc", *args],
                          capture_output=True, text=True, cwd=REPO)


def _nodes(db: Path) -> set[str]:
    c = sqlite3.connect(db)
    try:
        return {r[0] for r in c.execute("SELECT id FROM nodes")}
    finally:
        c.close()


def test_apply_refuses_without_a_backup() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="gc_nobk_"))
    try:
        db = tmp / "t.sqlite"
        _mkdb(db)
        r = _run("--apply", "--db", str(db))
        assert r.returncode == 2, f"expected refusal exit 2, got {r.returncode}"
        assert "REFUSING" in r.stderr, r.stderr
        assert _nodes(db) == {"keep", "junk1", "junk2"}, "refused run still deleted"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_apply_refuses_an_unusable_backup() -> None:
    """The realistic failure is not carelessness — it is a backup that was never taken, went
    to the wrong path, or is a 0-byte file from a copy that ran out of disk. Each of those
    satisfies a bare acknowledgement flag and restores nothing, so the backup is VERIFIED."""
    tmp = Path(tempfile.mkdtemp(prefix="gc_badbk_"))
    try:
        db = tmp / "t.sqlite"
        _mkdb(db)
        empty = tmp / "empty.sqlite"
        empty.touch()
        garbage = tmp / "garbage.sqlite"
        garbage.write_text("this is not a database")
        schema_only = tmp / "schema.sqlite"
        c = sqlite3.connect(schema_only)
        c.execute("CREATE TABLE nodes (id TEXT, path TEXT)")   # valid, but empty
        c.commit()
        c.close()

        for bad, why in ((tmp / "nope.sqlite", "does not exist"),
                         (empty, "0 bytes"),
                         (garbage, "not a database"),
                         (schema_only, "empty nodes table")):
            r = _run("--apply", "--db", str(db), "--backup", str(bad))
            assert r.returncode == 2, f"{why}: expected refusal, got {r.returncode}"
            assert "REFUSING" in r.stderr, f"{why}: {r.stderr}"
        assert _nodes(db) == {"keep", "junk1", "junk2"}, "a refused run deleted anyway"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_apply_with_a_good_backup_deletes_and_logs_what_it_removed() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="gc_ok_"))
    try:
        db = tmp / "t.sqlite"
        _mkdb(db)
        bk = tmp / "bk.sqlite"
        shutil.copy(db, bk)
        log = tmp / "recovery.json"

        r = _run("--apply", "--db", str(db), "--backup", str(bk), "--recovery-log", str(log))
        assert r.returncode == 0, r.stderr
        assert _nodes(db) == {"keep"}, _nodes(db)

        payload = json.loads(log.read_text())
        assert payload["total"] == 2
        assert payload["counts"] == {"brain_fixture": 1, "worktree_twin": 1}
        removed = {x["id"]: x["path"] for v in payload["removed"].values() for x in v}
        assert set(removed) == {"junk1", "junk2"}
        # Paths, not just IDs — an ID alone does not tell you what was lost.
        assert removed["junk2"] == "ARCTURION_Brain/engine/eval.py"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_recovery_log_is_written_before_any_delete() -> None:
    """Ordering is the whole point: a log written after the transaction is worthless in the
    one case it matters, a crash mid-sweep. Proven by making the log unwritable — the sweep
    must abort with the DB untouched."""
    tmp = Path(tempfile.mkdtemp(prefix="gc_order_"))
    try:
        db = tmp / "t.sqlite"
        _mkdb(db)
        blocker = tmp / "blocker"
        blocker.write_text("not a directory")

        conn = sqlite3.connect(db)
        try:
            raised = False
            try:
                G.sweep(conn, apply=True, verbose=False, recovery_log=blocker / "log.json")
            except OSError:
                raised = True
            assert raised, "sweep continued despite being unable to record what it removed"
        finally:
            conn.close()
        assert _nodes(db) == {"keep", "junk1", "junk2"}, "deleted before the log existed"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_dry_run_is_still_the_default_and_writes_nothing() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="gc_dry_"))
    try:
        db = tmp / "t.sqlite"
        _mkdb(db)
        r = _run("--db", str(db))
        assert r.returncode == 0, r.stderr
        assert "DRY RUN" in r.stdout
        assert _nodes(db) == {"keep", "junk1", "junk2"}
        # A dry run announces the same blast radius the real run would delete.
        assert "2 nodes would be removed" in r.stdout, r.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  ❌ {t.__name__}: {e}")
    print(f"\n{'PASS' if not failures else 'FAIL'} — skiplist_gc safety: "
          f"{len(tests) - failures}/{len(tests)} tests")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
