#!/usr/bin/env python3
"""engine.reindex — in-process orchestrator for the nightly Brain reindex.

WHY THIS EXISTS
---------------
The old nightly ran a Desktop-resident bash script (`brain-reindex.sh`) that
FORKED `python3` once per phase. Under a loaded LaunchAgent, macOS TCC taints the
whole process tree when launchd interprets a script file that lives in a protected
folder (~/Desktop) — the forked python is then DENIED read access to ~/Desktop
(`PermissionError: Operation not permitted`), so `import engine` fails with
`ModuleNotFoundError` and the reindex dies at Phase 1. This was masked while the
old homebrew python had Full Disk Access; the 2026-06-02 `brew upgrade python`
(→3.14.5) dropped that grant and silently broke every scheduled run since.

The fix (matches com.arcturion.brain-http / brain-memory-watch): the LaunchAgent
`exec`s python DIRECTLY via an inline `bash -c` — launchd never interprets a
Desktop script file, so there is no taint — and ALL phases run IN-PROCESS here.
See ALTAIR memory: memory-brain-live-ingest.md.

BEHAVIOUR — identical phase sequence + fatality to the old brain-reindex.sh:
  Inventory  engine.operating_inventory                     (non-fatal; before walk)
  Phase 1+2  engine.graph_builder [--full]                  (fatal)
  Phase 1b   engine.maintenance.prong_backfill              (non-fatal)
  Phase 2    engine.harvesters.run_all --concepts-limit 100 (source errors recorded;
                                                            watermark persistence fatal)
  Phase 2b   engine.maintenance.reciprocity_pass            (non-fatal)
  Phase 2c   engine.maintenance.entity_dedup                (non-fatal)
  Phase 2d   engine.journals refresh                        (non-fatal)
  Phase 2e   engine.journals precompute (bounded judge)     (non-fatal)
  Phase 3a   engine.embedder embed                          (fatal, skip: --skip-embed)
  Phase 3b   engine.embedder sim                            (fatal, skip: --skip-sim)
  Phase 5    engine.pagerank                                (fatal, skip: --skip-rank)
  Reports    engine.reports                                 (fatal)
  Phase 6    engine.coverage_check                           (non-fatal; alerts on FAIL)

Flags: --full --skip-embed --skip-sim --skip-rank
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import os
import runpy
import subprocess
import sys
import time
import warnings

from . import config as C

HOME = str(C.WORK_ROOT)
BRAIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(HOME, "logs")
SCRIPTS_DIR = os.path.join(HOME, "scripts")
LOCK = os.path.join(HOME, "brain-write.lock")


class _Tee:
    """Mirror writes to several streams (stdout + per-run logfile), like `| tee`.

    Proxies the file-like surface (isatty/fileno/encoding/…) to the primary
    stream so libraries that probe stdout (tqdm/huggingface call .isatty())
    don't choke. isatty() is forced False so progress bars render as plain
    lines instead of ANSI control codes in the logfile."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for st in self._streams:
            with contextlib.suppress(Exception):
                st.write(s)
                st.flush()

    def flush(self):
        for st in self._streams:
            with contextlib.suppress(Exception):
                st.flush()

    def isatty(self):
        return False

    def fileno(self):
        return self._streams[0].fileno()

    def writable(self):
        return True

    def __getattr__(self, name):
        # Delegate any other file attribute (encoding, errors, mode, buffer, …)
        # to the primary stream. _streams is set in __init__, so no recursion.
        return getattr(self._streams[0], name)


def _acquire_lock(pid: int, max_tries: int = 15) -> None:
    """Shared shlock with brain-ingest.sh — hold for the whole reindex so live
    ingests defer. Best-effort: press on after ~30s so a stuck ingest can't block
    the nightly (engine.db PRAGMA busy_timeout still serializes the actual writes)."""
    tries = 0
    while True:
        try:
            rc = subprocess.run(
                ["/usr/bin/shlock", "-f", LOCK, "-p", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ).returncode
        except OSError as e:
            # Best-effort by doctrine — a missing/unspawnable shlock must not
            # crash main() before the stdout/stderr restore (review 2026-07-20).
            print(f"[brain-reindex] shlock unavailable ({e}); proceeding unlocked")
            return
        if rc == 0:
            return
        tries += 1
        if tries >= max_tries:
            print("[brain-reindex] lock held >30s; proceeding")
            return
        time.sleep(2)


def _release_lock() -> None:
    with contextlib.suppress(Exception):
        os.remove(LOCK)


def _other_reindex_running() -> int | None:
    """PID of another live `python -m engine.reindex`, or None.

    The shared shlock is best-effort, so it does not stop two reindexes
    overlapping. Concurrent walks increase write-lock pressure and race
    garbage-collection sweeps.
    """
    try:
        out = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001 — a failed check must never block reindex
        return None
    me = os.getpid()
    for line in out.splitlines():
        pid_s, _, cmd = line.strip().partition(" ")
        if not pid_s.isdigit() or int(pid_s) in (me, os.getppid()):
            continue
        if "-m engine.reindex" in cmd and "python" in cmd.lower() and not any(
                sh in cmd.split(" ", 1)[0] for sh in ("zsh", "bash", "/bin/sh")):
            return int(pid_s)
    return None


def _thermal_gate() -> None:
    """Best-effort heartbeat/thermal gate — never fatal if missing or non-zero."""
    tc = os.path.join(SCRIPTS_DIR, "thermal-check.sh")
    if os.access(tc, os.X_OK):
        try:
            if subprocess.run([tc, "any"]).returncode != 0:
                print("[brain-reindex] thermal gate warning; continuing")
        except Exception as e:  # noqa: BLE001
            print(f"[brain-reindex] thermal gate error ({e}); continuing")


# A phase that dies on "database is locked" is retried after a wait. Another writer (the
# memory watcher's VEGA desk sync held the write lock ~80 s at a time on 2026-11-07) can
# outlast engine.db's 30 s busy_timeout; this killed the whole night on 2026-08-11. Every
# phase is idempotent (upserts + INSERT OR IGNORE), so re-running one is safe. Only this
# one error is retried — anything else keeps the original fatal/non-fatal policy.
LOCK_RETRIES = 2
LOCK_RETRY_WAIT_S = 90


def _is_lock_error(exc: BaseException) -> bool:
    return "database is locked" in str(exc).lower() or "database table is locked" in str(exc).lower()


def _run_phase(label: str, module: str, argv: list[str], fatal: bool) -> None:
    """Run a phase, retrying it if (and only if) it died on a SQLite write-lock timeout."""
    for attempt in range(LOCK_RETRIES + 1):
        try:
            return _run_phase_once(label, module, argv, fatal, reraise_lock=attempt < LOCK_RETRIES)
        except _LockRetry as e:
            print(f"[brain-reindex] {module} hit a locked database ({e}); "
                  f"retry {attempt + 1}/{LOCK_RETRIES} in {LOCK_RETRY_WAIT_S}s", flush=True)
            time.sleep(LOCK_RETRY_WAIT_S)


class _LockRetry(Exception):
    pass


def _run_phase_once(label: str, module: str, argv: list[str], fatal: bool,
                    reraise_lock: bool = False) -> None:
    """Run `python -m <module> <argv>` IN-PROCESS via runpy (identical to the CLI),
    honouring the same fatal/non-fatal policy the old bash script used."""
    print(f"--- {label} ---", flush=True)
    saved_argv = sys.argv
    sys.argv = [module, *argv]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        if code != 0:
            if fatal:
                raise RuntimeError(f"{module} exited {code}") from None
            print(f"[brain-reindex] {module} returned non-zero ({code}); continuing")
    except Exception as e:  # noqa: BLE001
        if reraise_lock and _is_lock_error(e):
            raise _LockRetry(f"{type(e).__name__}: {e}") from None
        if fatal:
            raise
        print(f"[brain-reindex] {module} failed non-fatally: {type(e).__name__}: {e}")
    finally:
        sys.argv = saved_argv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="engine.reindex",
                                 description="In-process nightly Brain reindex.")
    ap.add_argument("--full", action="store_true",
                    help="ignore content hashes; reprocess all")
    ap.add_argument("--skip-embed", action="store_true")
    ap.add_argument("--skip-sim", action="store_true")
    ap.add_argument("--skip-rank", action="store_true")
    args = ap.parse_args(argv)

    other = _other_reindex_running()
    if other:
        print(f"[brain-reindex] another reindex is already running (pid {other}); "
              "skipping this run so two walks never overlap")
        return 0

    os.makedirs(LOGS_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    with contextlib.suppress(Exception):
        os.chdir(BRAIN_DIR)

    ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    logpath = os.path.join(LOGS_DIR, f"reindex-{ts}.log")
    logf = open(logpath, "w", encoding="utf-8")  # noqa: SIM115
    real_stdout, real_stderr = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = _Tee(real_stdout, logf)

    rc = 0
    try:
        # Inside the try/finally so a lock-acquire raise still restores
        # stdout/stderr and closes logf (review 2026-07-20).
        _acquire_lock(os.getpid())
        now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        print(f"=== brain-reindex started {now} ===")
        _thermal_gate()

        # Generate before the static walk. Its markdown twin is on Brain/docs, a walked
        # source shelf, so the same reindex makes the current expiring facts searchable.
        _run_phase("Expiring operating inventory", "engine.operating_inventory", [], fatal=False)
        _run_phase("Phase 1+2: static walker + edges", "engine.graph_builder",
                   (["--full"] if args.full else []), fatal=True)
        _run_phase("Phase 1b: prong backfill", "engine.maintenance.prong_backfill",
                   [], fatal=False)
        # Source-read failures are recorded and reported as degraded by run_all. An
        # inability to persist that failure proof raises and is fatal: continuing would
        # leave a previous success looking fresh.
        _run_phase("Phase 2: agent/app/event/external/concept harvest",
                   "engine.harvesters.run_all", ["--concepts-limit", "100"], fatal=True)
        _run_phase("Phase 2b: hub reciprocity", "engine.maintenance.reciprocity_pass",
                   [], fatal=False)
        _run_phase("Phase 2c: entity dedup", "engine.maintenance.entity_dedup",
                   [], fatal=False)
        # Verified journals: re-derive from current decision/project/handoff files, ingest the
        # changed ones and link source --supports--> journal. Then spend a bounded, cached
        # judge budget (Terra via Codex) on new or changed journals; its verdicts reach the
        # journals' `supersedes` frontmatter on the next refresh. Both non-fatal.
        _run_phase("Phase 2d: journal refresh", "engine.journals", ["refresh"], fatal=False)
        _run_phase("Phase 2e: journal judge (bounded)", "engine.journals",
                   ["precompute", "--workers", "2", "--max-calls", "60", "--deadline-s", "1200"],
                   fatal=False)
        if not args.skip_embed:
            _run_phase("Phase 3a: embeddings", "engine.embedder", ["embed"], fatal=True)
        if not args.skip_sim:
            _run_phase("Phase 3b: semantic neighbors", "engine.embedder", ["sim"], fatal=True)
        if not args.skip_rank:
            _run_phase("Phase 5: PageRank centrality", "engine.pagerank", [], fatal=True)
        _run_phase("Reports", "engine.reports", [], fatal=True)
        # Phase 6 — standing coverage assertion: every agent that distills memory is
        # wired into the Brain + watched + freshly indexed. Non-fatal so a coverage
        # regression never aborts the reindex, but it writes index/coverage_assertion.md
        # and appends a COVENANT-inbox alert on FAIL (so onboarding drift surfaces next
        # morning instead of at the next manual audit).
        _run_phase("Phase 6: coverage assertion", "engine.coverage_check", [], fatal=False)

        end = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        print(f"=== brain-reindex finished {end} ===")
    except Exception as e:  # noqa: BLE001
        rc = 1
        print(f"=== brain-reindex FAILED: {type(e).__name__}: {e} ===")
    finally:
        _release_lock()
        sys.stdout, sys.stderr = real_stdout, real_stderr
        with contextlib.suppress(Exception):
            logf.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
