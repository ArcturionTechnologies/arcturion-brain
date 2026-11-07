"""Brain query metrics (Item 13).

Logs every `brain.query()` invocation to a `metrics` table in brain.sqlite.
HTTP `/metrics` endpoint surfaces last-7-day aggregates: total queries,
p50/p95 latency, tokens served, estimated tokens-saved vs raw-read baseline.

Public API:
  ensure_table()                — idempotent CREATE
  log_query(topic, source) → CM — context manager that wraps a query call
  aggregate(window_days=7)      — return summary dict for the window
"""
from __future__ import annotations

import atexit
import contextlib
import json
import queue
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Iterator

from . import db as DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  topic TEXT,
  source TEXT,
  latency_ms REAL,
  nodes_returned INTEGER,
  tokens_returned INTEGER,
  truncated INTEGER DEFAULT 0,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts);
"""

# Heuristic: raw-read baseline assumes the brain saved you from reading every
# referenced file at ~1500 tokens per file. Tuneable.
RAW_FILE_TOKENS = 1500


def ensure_table(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    if own:
        conn = DB.connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        if own:
            conn.close()


_INSERT_SQL = (
    "INSERT INTO metrics (ts, topic, source, latency_ms, nodes_returned, "
    "tokens_returned, truncated, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)

# Async metrics writer (perf, 2026-06-20). Every brain.query() logs one row. Doing
# that write inline on the request thread put a per-query INSERT+commit on the hot
# path — and on the ~3 GB WAL DB it can block up to busy_timeout (30 s) whenever the
# live engine.watch ingest daemon holds the single WAL writer lock, producing the
# unpredictable multi-second /query spikes. We hand each row to a single daemon thread
# that owns one connection and batches commits, so the request thread never waits on
# the write lock. Metrics are best-effort: a full queue drops rather than blocks, and
# the table is created once (not per query).
_QUEUE: "queue.Queue | None" = None
_WORKER: "threading.Thread | None" = None
_WORKER_LOCK = threading.Lock()
_SENTINEL = object()
_QUEUE_MAX = 20000


def _drain_loop() -> None:
    conn = None
    try:
        conn = DB.connect()
        ensure_table(conn)
    except Exception:
        conn = None
    while True:
        item = _QUEUE.get()  # type: ignore[union-attr]
        if item is _SENTINEL:
            break
        batch = [item]
        # Opportunistically coalesce a burst into one commit.
        stop = False
        try:
            while len(batch) < 500:
                nxt = _QUEUE.get_nowait()  # type: ignore[union-attr]
                if nxt is _SENTINEL:
                    stop = True
                    break
                batch.append(nxt)
        except queue.Empty:
            pass
        try:
            if conn is None:
                conn = DB.connect()
                ensure_table(conn)
            conn.executemany(_INSERT_SQL, batch)
            conn.commit()
        except Exception:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            conn = None  # force reconnect next round
        if stop:
            break
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


def _ensure_worker() -> bool:
    """Lazily start the writer thread. Returns False if it couldn't start."""
    global _QUEUE, _WORKER
    if _WORKER is not None and _WORKER.is_alive():
        return True
    with _WORKER_LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return True
        try:
            _QUEUE = queue.Queue(maxsize=_QUEUE_MAX)
            _WORKER = threading.Thread(target=_drain_loop, name="brain-metrics-writer", daemon=True)
            _WORKER.start()
            atexit.register(_flush)
            return True
        except Exception:
            _WORKER = None
            return False


def _flush(timeout: float = 2.0) -> None:
    """Best-effort drain on interpreter exit (so one-shot CLI runs persist their row)."""
    if _WORKER is None or _QUEUE is None or not _WORKER.is_alive():
        return
    try:
        _QUEUE.put_nowait(_SENTINEL)
    except queue.Full:
        return
    _WORKER.join(timeout=timeout)


@contextlib.contextmanager
def log_query(topic: str, source: str = "unknown") -> Iterator[dict]:
    """Wrap a query call; enqueues timing + result counts to the async writer on exit.

    Usage:
        with log_query(topic, source) as ctx:
            pack = brain.query(topic=topic, ...)
            ctx["nodes"] = len(pack.nodes)
            ctx["tokens"] = pack.used_tokens_est
            ctx["truncated"] = pack.truncated
    """
    t0 = time.time()
    ctx = {"nodes": 0, "tokens": 0, "truncated": False, "error": None}
    err: str | None = None
    try:
        yield ctx
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        raise
    finally:
        latency_ms = (time.time() - t0) * 1000.0
        row = (
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            topic,
            source,
            round(latency_ms, 1),
            int(ctx.get("nodes") or 0),
            int(ctx.get("tokens") or 0),
            1 if ctx.get("truncated") else 0,
            err or ctx.get("error"),
        )
        # Hand off to the background writer — never block the request thread on the
        # WAL write lock. If the writer can't start or the queue is full, drop the
        # metric (best-effort) rather than fall back to a blocking inline write.
        try:
            if _ensure_worker() and _QUEUE is not None:
                _QUEUE.put_nowait(row)
        except queue.Full:
            pass
        except Exception:
            pass


def retrieval_quality() -> dict:
    """The last golden-gate score, for `/metrics` to show beside the throughput numbers.

    `savings_pct_est` is `nodes_returned * RAW_FILE_TOKENS - tokens_served`. It goes UP
    when the Brain returns MORE nodes and has no term for whether any of them answered the
    question — a measure of text avoided, displayed where a reader looks for how well
    retrieval works.

    So: read the gate's sidecar (written by `retrieval_eval.write_score` on every scoring
    run, i.e. nightly at 03:30). Cheap — one small file read, no queries.

    `stale_hours` is reported rather than hidden. A quality number with no age is worse
    than none: a green figure that quietly stopped meaning anything is the exact failure
    this programme was convened to remove."""
    from datetime import datetime as _dt
    try:
        # Late import: retrieval_eval reaches brain, which reaches this module.
        from . import retrieval_eval as _RE
        data = json.loads(_RE.SCORE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"available": False,
                "why": "no scoring run has published a score yet "
                       "(python3 -m engine.retrieval_eval)"}
    except Exception as e:  # noqa: BLE001 — a metrics read must never hard-fail
        return {"available": False, "why": f"{type(e).__name__}: {e}"}
    pos = data.get("positive") or {}
    guards = data.get("guards") or {}
    out = {
        "available": True,
        "measured": data.get("generated"),
        "status": data.get("status"),
        "questions": data.get("questions"),
        "recall_at_5": pos.get("recall5_rate"),
        "top1": pos.get("top1_rate"),
        # precision@5 is the Goodhart pair for `savings_pct_est` below (Leg 1 expansion,
        # 2026-08-06): savings_pct_est rewards returning MORE nodes with no term for
        # whether any answered the question, so it cannot be read alone without inviting
        # exactly that optimization. precision@5 sits in the SAME struct on purpose — a
        # reader who quotes one now sees the other in the same JSON blob.
        "precision_at_5": pos.get("precision5_rate"),
        "mrr": pos.get("mrr"),
        "superseded_in_top5_rate": pos.get("superseded_in_top5_rate"),
        "wrong_context_rate": pos.get("wrong_context_rate"),
        "guards_pass_rate": guards.get("pass_rate"),
        "stale_fixture_ids": data.get("unresolved_fixture_ids") or [],
        "_note": ("Relevance, from engine.retrieval_eval's golden set. Read this beside "
                  "savings_pct_est, which counts tokens avoided and cannot tell a "
                  "correct pack from an irrelevant one. precision_at_5 in particular is "
                  "the direct counterweight: quote savings_pct_est without it and you are "
                  "quoting a number that goes up when the Brain returns more wrong things."),
    }
    try:
        age = (datetime.now(timezone.utc)
               - _dt.fromisoformat(data["generated"]).astimezone(timezone.utc))
        out["stale_hours"] = round(age.total_seconds() / 3600, 1)
    except Exception:  # noqa: BLE001
        out["stale_hours"] = None
    return out


def aggregate(window_days: int = 7) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat(timespec="seconds")
    conn = DB.connect()
    try:
        ensure_table(conn)
        rows = conn.execute(
            "SELECT latency_ms, nodes_returned, tokens_returned FROM metrics "
            "WHERE ts >= ? AND error IS NULL",
            (cutoff,),
        ).fetchall()
        # Query throughput says how hard the Brain is working. It says nothing about
        # whether the graph underneath still has edges you can walk — /metrics carried
        # the first number and not the second for six weeks. Carry both.
        try:
            graph_health = DB.edge_health(conn)
        except Exception as e:  # noqa: BLE001 — a metrics read must never hard-fail
            graph_health = {"error": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()

    quality = retrieval_quality()

    n = len(rows)
    if n == 0:
        return {
            "window_days": window_days,
            "queries": 0,
            "since": cutoff,
            "graph_health": graph_health,
            "retrieval_quality": quality,
            "p50_latency_ms": None,
            "p95_latency_ms": None,
            "avg_nodes": 0,
            "tokens_served": 0,
            "tokens_saved_est": 0,
            "savings_pct_est": None,
        }
    latencies = sorted(r["latency_ms"] or 0 for r in rows)
    tokens_served = sum(r["tokens_returned"] or 0 for r in rows)
    nodes_total = sum(r["nodes_returned"] or 0 for r in rows)
    raw_read_estimate = nodes_total * RAW_FILE_TOKENS
    saved = max(0, raw_read_estimate - tokens_served)

    def pct(arr, p):
        idx = max(0, min(len(arr) - 1, int(round(p * (len(arr) - 1)))))
        return arr[idx]

    savings_pct = round(100.0 * saved / max(raw_read_estimate, 1), 1) if raw_read_estimate else None
    return {
        "window_days": window_days,
        "queries": n,
        "since": cutoff,
        "graph_health": graph_health,
        # Relevance sits directly above the throughput block on purpose: whoever scans
        # for "how is the Brain doing" must meet the honest number first.
        "retrieval_quality": quality,
        "p50_latency_ms": round(pct(latencies, 0.5), 1),
        "p95_latency_ms": round(pct(latencies, 0.95), 1),
        "avg_nodes": round(nodes_total / n, 1),
        "tokens_served": tokens_served,
        "tokens_saved_est": saved,
        "savings_pct_est": savings_pct,
        "raw_read_baseline_per_node": RAW_FILE_TOKENS,
    }


if __name__ == "__main__":
    import argparse, json, sys
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--init", action="store_true", help="create table only")
    args = ap.parse_args()
    if args.init:
        ensure_table()
        print("metrics table ready")
        sys.exit(0)
    print(json.dumps(aggregate(window_days=args.days), indent=2))
