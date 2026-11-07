"""Journal answer resolution at the relevance-floor hook (v11 pattern, real journals).

Ported from the staged v11 search (staging/graph_context_engineering/rollout/staging_snapshots/
search-recall-v11/evidence_fields.py + engine_patch.py). v11's 1,450 lines of supplier-specific
word rules are NOT carried over: they were tuned to one synthetic referral journal. What carries
over is the architecture:

  * the hook sits on the same line of `brain._query_impl` (the relevance-floor gate);
  * only VERIFIED journals are eligible (id `journal:*`, provenance block present, and the
    source --supports--> journal edge exists with the journal's pinned SHA-256);
  * a whole-question verdict from the judge (GPT-6.1 Terra, high, via Codex OAuth) decides
    whether the journal answers the question, and REPLACES word rules;
  * with no verdict the search falls back to the current behaviour unchanged.

Hot path rule: this module NEVER calls Codex inline. It reads a persistent verdict cache. A
missing verdict is queued; a single background worker (capped per day, hard timeout per call)
fills the cache, so the same question is judged-informed from its next ask onward.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path

from . import config as C

ENABLED = os.environ.get("ARCTURION_BRAIN_ANSWER_JUDGE", "0") == "1"
ASYNC = os.environ.get("ARCTURION_BRAIN_ANSWER_JUDGE_ASYNC", "0") == "1"
DAILY_CAP = int(os.environ.get("ARCTURION_BRAIN_ANSWER_JUDGE_DAILY_CAP", "150"))
MAX_JOURNALS = 3          # journals per question that may be judged
SCAN_DEPTH = 40           # how far down the ranked candidates a journal may sit
STATE_DIR = Path(os.environ.get("ARCTURION_BRAIN_JOURNAL_STATE", str(C.GRAPH_DIR / "journals")))
CACHE_PATH = STATE_DIR / "answer_verdicts.json"

_LOCK = threading.RLock()
_CACHE = {"mtime": None, "data": None}
_QUEUE: deque = deque()
_QUEUED: set = set()
_WORKER: dict = {"thread": None}


def _load() -> dict:
    with _LOCK:
        try:
            mtime = CACHE_PATH.stat().st_mtime
        except OSError:
            mtime = None
        if _CACHE["data"] is None or mtime != _CACHE["mtime"]:
            data = None
            if mtime is not None:
                try:
                    data = json.loads(CACHE_PATH.read_text())
                except (OSError, ValueError):
                    data = None
            _CACHE["data"] = data or {"verdicts": {}, "failures": [], "calls_by_day": {}}
            _CACHE["mtime"] = mtime
        return _CACHE["data"]


def _save(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0, sort_keys=True) + "\n")
    tmp.replace(CACHE_PATH)
    _CACHE["mtime"] = CACHE_PATH.stat().st_mtime


def _meta(node: dict) -> dict:
    m = node.get("metadata")
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except ValueError:
            return {}
    return m if isinstance(m, dict) else {}


def verified_journal(node: dict, conn) -> bool:
    if not str(node.get("id") or "").startswith("journal:"):
        return False
    j = _meta(node).get("journal")
    if not isinstance(j, dict) or not j.get("source_node") or not j.get("source_sha256"):
        return False
    row = conn.execute(
        "SELECT 1 FROM edges e JOIN nodes s ON s.id = e.from_id WHERE e.from_id=? AND e.to_id=? "
        "AND e.predicate='supports' AND s.content_hash=?",
        (j["source_node"], node["id"], j["source_sha256"])).fetchone()
    return bool(row)


_CURRENT_MARKERS = ("/memory/projects/", "/memory/decisions/", "arcturion_brain/journals/")


def _current_record(node: dict) -> bool:
    p = (node.get("path") or "").lower()
    return any(m in p for m in _CURRENT_MARKERS)


def _date(node: dict) -> str:
    j = _meta(node).get("journal") if str(node.get("id") or "").startswith("journal:") else None
    return str((j or {}).get("as_of") or node.get("last_modified") or "")[:10]


def record_text(node: dict) -> str:
    return "\n".join(x for x in (f"title: {node.get('title') or ''}", f"path: {node.get('path') or ''}",
                                  node.get("summary") or "", node.get("body_excerpt") or "") if x)


def _enqueue(topic: str, node: dict, key: str) -> None:
    if not ENABLED or not ASYNC:
        return
    with _LOCK:
        if key in _QUEUED:
            return
        _QUEUED.add(key)
        _QUEUE.append((topic, node["id"], node.get("content_hash") or "", record_text(node), key))
        t = _WORKER["thread"]
        if t is None or not t.is_alive():
            t = threading.Thread(target=_drain, name="brain-answer-judge", daemon=True)
            _WORKER["thread"] = t
            t.start()


def judge_one(topic: str, node_id: str, content_hash: str, text: str, key: str) -> str:
    """Judge one (question, journal) pair synchronously and persist. Returns ok|failed|capped."""
    from . import judge as JD
    day = time.strftime("%Y-%m-%d")
    try:
        JD.codex_bin()
    except FileNotFoundError as exc:  # nothing launched, so nothing counts against the cap
        with _LOCK:
            data = _load()
            data["failures"] = (data["failures"] + [{"key": key, "node": node_id, "error": str(exc),
                                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}])[-200:]
            _save(data)
        return "failed"
    with _LOCK:
        data = _load()
        if key in data["verdicts"]:
            return "ok"
        if data["calls_by_day"].get(day, 0) >= DAILY_CAP:
            return "capped"
        data["calls_by_day"][day] = data["calls_by_day"].get(day, 0) + 1
        _save(data)
    try:
        v = JD.ask_answer(topic, text)
        rec = {"question": topic[:2000], "node": node_id, "content_hash": content_hash,
               "answer": v["answer"]["answer"], "reason": v["answer"]["reason"],
               "model": v["model"], "effort": v["effort"], "tool_calls": v["tool_calls"],
               "rollout": v.get("rollout"), "thread_id": v.get("thread_id"), "seconds": v.get("seconds"),
               "prompt_version": JD.ANSWER_PROMPT_VERSION, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        with _LOCK:
            data = _load()
            data["verdicts"][key] = rec
            _save(data)
        return "ok"
    except Exception as exc:  # recorded; no verdict means current behaviour
        with _LOCK:
            data = _load()
            data["failures"] = (data["failures"] + [{"key": key, "node": node_id,
                                "error": f"{type(exc).__name__}: {exc}"[:300],
                                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}])[-200:]
            _save(data)
        return "failed"


def _drain() -> None:
    while True:
        with _LOCK:
            if not _QUEUE:
                return
            item = _QUEUE.popleft()
        try:
            judge_one(*item)
        finally:
            with _LOCK:
                _QUEUED.discard(item[-1])


def resolve(topic: str, nodes: list, conn, ok: bool):
    """Return (nodes, ok). A cached YES puts that journal first (and admits a pack the floor
    rejected). Anything else returns the inputs unchanged."""
    if not ENABLED or not nodes or not isinstance(topic, str) or not topic.strip() or len(topic) > 4000:
        return nodes, ok
    try:
        from . import judge as JD
        data = _load()
        picked = []
        for n in nodes[:SCAN_DEPTH]:
            if len(picked) >= MAX_JOURNALS:
                break
            if verified_journal(n, conn):
                picked.append(n)
        for n in picked:
            key = JD.answer_key(topic, n["id"], n.get("content_hash") or "")
            v = data["verdicts"].get(key)
            if v is None:
                _enqueue(topic, n, key)
            elif v.get("answer") == "YES":
                # A YES says the record is relevant, not that it is the newest account. It may
                # not jump a NEWER maintained record that already outranks it.
                as_of = str((_meta(n).get("journal") or {}).get("as_of") or "")
                k = nodes.index(n)
                if any(_current_record(m) and _date(m) > as_of for m in nodes[:k]):
                    return nodes, True
                rest = [m for m in nodes if m is not n]
                return [n] + rest, True
        return nodes, ok
    except Exception:  # an unavailable secondary signal never fails the query
        return nodes, ok


def pending() -> int:
    with _LOCK:
        return len(_QUEUE)


def wait_idle(timeout: float = 3600) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        t = _WORKER["thread"]
        if (t is None or not t.is_alive()) and not pending():
            return True
        time.sleep(1)
    return False
