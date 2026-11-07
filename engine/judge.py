"""Supersession judge for verified journals: GPT-6.1 Terra at high effort via Codex OAuth.

Ported from the staged v11 whole-question judge (staging/graph_context_engineering/rollout/
staging_snapshots/search-recall-v11/judge.py) and generalized from the synthetic supplier
journal to real record journals. It answers ONE question per (journal, candidates) batch:
does this current record supersede each older look-alike as the account of the same subject?

Never on the query hot path. `precompute` runs offline (nightly reindex phase or by hand) and
writes a persistent verdict cache; ranking reads only the cache (via journal `supersedes`
frontmatter). Each verdict carries Codex's own record of model, effort and tool use. Any
failure (timeout, bad output, unattested run) leaves that pair without a verdict, which means
current behaviour: no supersession.

No local models, no OpenRouter. Codex is called with stdin=/dev/null (it otherwise waits on
stdin) and a hard timeout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config as C

MODEL = "gpt-6.1-terra"
EFFORT = "high"
PROMPT_VERSION = "live-supersession-v1"
TIMEOUT_S = int(os.environ.get("ARCTURION_BRAIN_JUDGE_TIMEOUT", "240"))
STATE_DIR = Path(os.environ.get("ARCTURION_BRAIN_JOURNAL_STATE", str(C.GRAPH_DIR / "journals")))
# Outside ~/Desktop: Codex spawned from a launchd service must not need Desktop access.
WORKDIR = Path(os.environ.get("ARCTURION_BRAIN_JUDGE_WORKDIR", str(C.WORK_ROOT / ".arcturion" / "brain-judge-workdir")))
LOCK = threading.Lock()

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["verdicts"],
          "properties": {"verdicts": {"type": "array", "items": {
              "type": "object", "additionalProperties": False,
              "required": ["candidate", "reason", "answer"],
              "properties": {"candidate": {"type": "string"}, "reason": {"type": "string"},
                             "answer": {"type": "string", "enum": ["YES", "NO"]}}}}}}

PROMPT = """You are the supersession judge for a knowledge base. Do not run commands, open files or use tools. Use only the text below.

CURRENT RECORD (a {record_type} record, as of {as_of}):
{current}

OLDER CANDIDATES (each is a separate document in the same knowledge base):
{candidates}

For EACH candidate decide whether the CURRENT RECORD supersedes it: answer YES only if the candidate is an earlier account of the SAME specific subject (the same project, decision, system, incident or handoff) and someone asking about the present state of that subject should read the current record instead, because the candidate's status, plan or claims are older or replaced (this includes an archived or backup copy of an earlier version). Answer NO if the candidate is about a different or broader subject, is a standing rule, doctrine, reference, method or lesson that is still valid on its own, adds facts the current record does not cover, or if you are unsure.

Reply with JSON only: {{"verdicts": [{{"candidate": "<candidate id>", "reason": "<one short sentence>", "answer": "YES" or "NO"}}, ...]}} with exactly one entry per candidate."""


def template_sha256() -> str:
    return hashlib.sha256(PROMPT.encode()).hexdigest()


def batch_key(journal: dict, candidates: list[dict]) -> str:
    """Changes whenever the prompt, the source bytes or any candidate's bytes change."""
    return hashlib.sha256(json.dumps(
        [PROMPT_VERSION, template_sha256(), journal["source_sha256"],
         sorted((c["id"], c.get("content_hash") or "") for c in candidates)]).encode()).hexdigest()


def rollout_attestation(thread_id: str):
    """Model, effort, sandbox and tool use from Codex's own session record."""
    root = C.WORK_ROOT / ".codex/sessions"
    for f in sorted(root.rglob(f"*{thread_id}.jsonl"), reverse=True):
        turn, tools, cli = None, 0, None
        for line in f.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            t, p = e.get("type"), e.get("payload") or {}
            if t == "session_meta":
                cli = p.get("cli_version")
            elif t == "turn_context":
                turn = {"model": p.get("model"), "effort": p.get("effort"),
                        "sandbox": (p.get("sandbox_policy") or {}).get("type")}
            elif t == "response_item" and p.get("type") in ("function_call", "local_shell_call", "custom_tool_call"):
                tools += 1
        if turn:
            return {**turn, "tool_calls": tools, "cli_version": cli, "rollout": f.name}
    return None


def codex_bin() -> str:
    """Return an explicitly configured absolute executable; never auto-discover one."""
    configured = os.environ.get("ARCTURION_BRAIN_CODEX_BIN", "")
    if not configured or not os.path.isabs(configured):
        raise FileNotFoundError("ARCTURION_BRAIN_CODEX_BIN must name an absolute executable")
    if not os.path.isfile(configured) or not os.access(configured, os.X_OK):
        raise FileNotFoundError("configured Codex executable is unavailable")
    return configured


def ask(prompt: str, timeout: int = TIMEOUT_S, schema_obj: dict | None = None) -> dict:
    WORKDIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="judge-") as tmp:
        schema = Path(tmp) / "schema.json"
        schema.write_text(json.dumps(schema_obj or SCHEMA))
        last = Path(tmp) / "last.json"
        exe = codex_bin()
        cmd = [exe, "exec", "-m", MODEL, "-c", f'model_reasoning_effort="{EFFORT}"',
               "--ignore-user-config", "--disable", "apps", "--disable", "plugins", "--disable", "memories",
               "-c", "mcp_servers={}", "-c", "project_doc_max_bytes=0", "--skip-git-repo-check",
               "-s", "read-only", "-C", str(WORKDIR), "--output-schema", str(schema), "-o", str(last),
               "--json", prompt]
        t0 = time.time()
        # Codex is a Node script: its own bin dir (which holds `node`) must be on PATH.
        env = {**os.environ, "PATH": os.path.dirname(exe) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin")}
        run = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, env=env)
        thread = usage = None
        for line in run.stdout.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "thread.started":
                thread = e.get("thread_id")
            elif e.get("type") == "turn.completed":
                usage = e.get("usage")
        if run.returncode or not last.exists():
            raise RuntimeError(f"codex exit {run.returncode}: {run.stderr[-300:]}")
        answer = json.loads(last.read_text())
    att = rollout_attestation(thread) if thread else None
    if not att or att["model"] != MODEL or att["effort"] != EFFORT or att["tool_calls"]:
        raise RuntimeError(f"judge run not attested as {MODEL}/{EFFORT} without tools: {att}")
    return {"answer": answer, "thread_id": thread, "usage": usage, "seconds": round(time.time() - t0, 1), **att}


def build_prompt(journal: dict, current_text: str, candidates: list[dict]) -> str:
    cands = "\n\n".join(
        f"[{c['id']}] path: {c['path']} (last modified {str(c.get('last_modified') or '')[:10]})\n"
        f"title: {c.get('title') or ''}\nsummary: {(c.get('summary') or '')[:500]}\n"
        f"excerpt: {(c.get('body_excerpt') or '')[:900]}" for c in candidates)
    return PROMPT.format(record_type=journal["record_type"].replace("_", " "), as_of=journal["as_of"],
                         current=current_text[:4000], candidates=cands)


def cache_path() -> Path:
    return STATE_DIR / "supersession_cache.json"


def load_cache() -> dict:
    p = cache_path()
    if not p.exists():
        return {"prompt_version": PROMPT_VERSION, "template_sha256": template_sha256(), "model": MODEL,
                "effort": EFFORT, "batches": {}, "failures": []}
    data = json.loads(p.read_text())
    if data.get("prompt_version") != PROMPT_VERSION or data.get("template_sha256") != template_sha256():
        raise ValueError("verdict cache belongs to a different judge prompt")
    return data


def save_cache(data: dict) -> None:
    p = cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0, sort_keys=True) + "\n")
    tmp.replace(p)


def judge_batches(batches: list[dict], workers: int = 3, max_calls: int | None = None,
                  deadline_s: float | None = None) -> dict:
    """batches: [{journal, current_text, candidates}]. Judges only uncached batches."""
    data = load_cache()
    todo = [b for b in batches if batch_key(b["journal"], b["candidates"]) not in data["batches"]]
    if max_calls is not None:
        todo = todo[:max_calls]
    start = time.time()

    def work(b):
        if deadline_s is not None and time.time() - start > deadline_s:
            return "skipped"
        k = batch_key(b["journal"], b["candidates"])
        ids = {c["id"] for c in b["candidates"]}
        try:
            v = ask(build_prompt(b["journal"], b["current_text"], b["candidates"]))
            got = {x["candidate"]: x for x in v["answer"]["verdicts"] if x.get("candidate") in ids}
            if set(got) != ids:
                raise ValueError(f"judge returned {len(got)} of {len(ids)} candidate verdicts")
            rec = {"journal": b["journal"]["id"], "source_sha256": b["journal"]["source_sha256"],
                   "verdicts": [{"id": c["id"], "path": c["path"], "content_hash": c.get("content_hash"),
                                 "answer": got[c["id"]]["answer"], "reason": got[c["id"]]["reason"]}
                                for c in b["candidates"]],
                   "attestation": {k2: v[k2] for k2 in ("model", "effort", "sandbox", "tool_calls",
                                                        "cli_version", "rollout", "thread_id", "seconds")}}
            with LOCK:
                data["batches"][k] = rec
                save_cache(data)
            return "ok"
        except Exception as exc:  # recorded; no verdict means no supersession
            with LOCK:
                data["failures"].append({"key": k, "journal": b["journal"]["id"],
                                         "error": f"{type(exc).__name__}: {exc}"[:400],
                                         "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
                save_cache(data)
            return "failed"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(work, todo))
    save_cache(data)
    return {"requested": len(todo), "ok": results.count("ok"), "failed": results.count("failed"),
            "skipped": results.count("skipped")}


def verdicts_by_journal() -> dict:
    """{journal_id: {"supersedes": [...YES...], "attestation": {...}}} for journals.build()."""
    out: dict = {}
    for rec in load_cache()["batches"].values():
        yes = [v for v in rec["verdicts"] if v["answer"] == "YES"]
        cur = out.setdefault(rec["journal"], {"supersedes": [], "attestation": rec["attestation"],
                                              "source_sha256": rec["source_sha256"]})
        if cur["source_sha256"] != rec["source_sha256"]:
            continue
        cur["supersedes"].extend(yes)
    return out


# ---------------------------------------------------------------- answer judge (v11 pattern)
ANSWER_PROMPT_VERSION = "live-answer-v1"
ANSWER_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["reason", "answer"],
                 "properties": {"reason": {"type": "string"}, "answer": {"type": "string", "enum": ["YES", "NO"]}}}
ANSWER_PROMPT = """You are the relevance judge for a search engine. Decide whether one stored record answers a user's question. Do not run commands, open files or use tools. Use only the text below.

STORED RECORD:
{record}

The user asked: "{question}"

Answer YES if this record is the right document to give the user: it is about the specific subject they ask about and states the facts, status, decision or rule they want (partial answers count; extra wording about format does not matter). Answer NO if the record is about a different subject, only mentions the words in passing, or the user wants something the record does not contain. If unsure, answer NO.

Reply with JSON only: {{"reason": "<one short sentence>", "answer": "YES" or "NO"}}"""


def answer_template_sha256() -> str:
    return hashlib.sha256(ANSWER_PROMPT.encode()).hexdigest()


def answer_key(question: str, node_id: str, content_hash: str) -> str:
    q = " ".join((question or "").split()).casefold()
    return hashlib.sha256(json.dumps([ANSWER_PROMPT_VERSION, answer_template_sha256(), q, node_id,
                                      content_hash or ""]).encode()).hexdigest()


def ask_answer(question: str, record_text: str, timeout: int = TIMEOUT_S) -> dict:
    v = ask(ANSWER_PROMPT.format(record=record_text[:5000], question=question[:2000]), timeout=timeout,
            schema_obj=ANSWER_SCHEMA)
    if v["answer"].get("answer") not in ("YES", "NO"):
        raise ValueError("unexpected judge answer")
    return v
