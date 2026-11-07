"""Hermetic test for the session harvester's incremental cache (2026-11-07).

The cache exists so the nightly reindex stops re-reading a large transcript collection. What
must stay true while it saves that time:

  1. an unchanged, already-harvested session is skipped on the next run;
  2. a refused (excluded-agent) transcript stays refused when its verdict comes from
     the cache, so `purge_excluded_sessions` sees the same list every night;
  3. a changed file is rescanned and re-harvested;
  4. a change to the exclusion rules (EXCLUDED_AGENTS) throws the scan cache away;
  5. a test that repoints C.BRAIN_DB never touches the live cache file.

Run:  python3 -m engine.tests.test_session_harvest_cache
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from .. import config as C
from .. import db as DB
from ..harvesters import sessions as S

LIVE_CACHE = Path(C.BRAIN_DB).parent / S._CACHE_NAME


def _transcript(path: Path, cwd: str, prompt: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "type": "queue-operation", "operation": "enqueue", "cwd": cwd,
        "timestamp": "2026-11-01T10:00:00.000Z", "content": prompt}) + "\n",
        encoding="utf-8")


def run() -> int:
    failures: list[str] = []
    live_before = LIVE_CACHE.stat().st_mtime_ns if LIVE_CACHE.exists() else None
    tmp = Path(tempfile.mkdtemp(prefix="brain_sess_cache_"))
    orig = (C.DESKTOP_ROOT, C.BRAIN_DB, dict(C.EXCLUDED_AGENTS), S._DISTILL_ROOT)
    orig_today = S.datetime.date
    C.DESKTOP_ROOT, C.BRAIN_DB = tmp, tmp / "brain.sqlite"
    S._DISTILL_ROOT = tmp / "ARCTURION_Brain/memory/0608_SESSION_DISTILLATIONS"

    # Pin "today" to a day whose rotating-refresh bucket matches neither test file, so
    # the skip assertions are deterministic.
    class _Day(datetime.date):
        pick = None

        @classmethod
        def today(cls):
            return cls.pick
    S.datetime.date = _Day
    try:
        ok_rel = "ALTAIR/000 Altair/Memory/Sessions/raw/2026-11-01_aaaa1111-0000-0000-0000-000000000000.jsonl"
        bad_rel = "ARCTURION_Brain/memory/0608_SESSION_DISTILLATIONS/claude/2026-11-01_bbbb2222-0000-0000-0000-000000000000.jsonl"
        _transcript(tmp / ok_rel, str(tmp / "ALTAIR"), "Altair, tidy the notes.")
        _transcript(tmp / bad_rel, "/Users/example/Desktop/THERAPIST", "private session")

        import hashlib
        buckets = {int(hashlib.sha256(r.encode()).hexdigest(), 16) % S._REFRESH_BUCKETS
                   for r in (ok_rel, bad_rel)}
        base = datetime.date(2026, 11, 1)
        _Day.pick = next(base + datetime.timedelta(days=i) for i in range(7)
                         if (base + datetime.timedelta(days=i)).toordinal()
                         % S._REFRESH_BUCKETS not in buckets)

        conn = DB.connect(C.BRAIN_DB)
        DB.init_db(conn)
        conn.commit()

        r1 = S.harvest(conn, verbose=False)
        if r1["sessions"] != 1 or r1["excluded_transcripts"] != 1:
            failures.append(f"run 1 should harvest 1 and refuse 1: {r1}")
        cache_file = tmp / S._CACHE_NAME
        if not cache_file.exists():
            failures.append("cache file not written beside the test DB")

        r2 = S.harvest(conn, verbose=False)
        if r2["sessions"] != 0 or r2["unchanged_skipped"] != 1:
            failures.append(f"run 2 should skip the unchanged session: {r2}")
        if r2["excluded_transcripts"] != 1:
            failures.append(f"cached verdict must keep the THERAPIST file refused: {r2}")
        n = conn.execute("SELECT count(*) FROM nodes WHERE type='Session'").fetchone()[0]
        if n != 1:
            failures.append(f"exactly one Session node expected, got {n}")

        time.sleep(0.01)
        _transcript(tmp / ok_rel, str(tmp / "ALTAIR"), "Altair, tidy the notes again.")
        r3 = S.harvest(conn, verbose=False)
        if r3["sessions"] != 1:
            failures.append(f"run 3 should re-harvest the changed file: {r3}")

        cache = S._HarvestCache()
        if not cache.scans:
            failures.append("scan cache should be loaded under unchanged rules")
        C.EXCLUDED_AGENTS["SOMEONE_NEW"] = "test"
        if S._HarvestCache().scans:
            failures.append("a change to EXCLUDED_AGENTS must invalidate the scan cache")
        del C.EXCLUDED_AGENTS["SOMEONE_NEW"]
        conn.close()
    finally:
        S.datetime.date = orig_today
        C.DESKTOP_ROOT, C.BRAIN_DB = orig[0], orig[1]
        C.EXCLUDED_AGENTS.clear()
        C.EXCLUDED_AGENTS.update(orig[2])
        S._DISTILL_ROOT = orig[3]

    live_after = LIVE_CACHE.stat().st_mtime_ns if LIVE_CACHE.exists() else None
    if live_after != live_before:
        failures.append("the live cache file was touched by a hermetic test")

    if failures:
        print("FAIL — session harvest cache:")
        for f in failures:
            print("   -", f)
        return 1
    print("PASS — session harvest cache: skip unchanged, keep refusals, rescan changed, "
          "invalidate on rule change, live cache untouched")
    return 0


if __name__ == "__main__":
    sys.exit(run())
