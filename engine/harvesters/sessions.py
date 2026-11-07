"""Cross-agent session + temporal layer (Brain v2 Phase 4).

Reads each constellation agent's session transcripts at
`<AGENT>/000 *Agent*/Memory/Sessions/raw/*.jsonl` (the canonical raw-JSONL routing,
decision session-jsonl-canonical-routing-2026-06-10) and emits the event nodes +
temporal edges that realize two previously-unanswerable queries:

  - "what did ARCTURUS and I discuss"  → Session nodes, owned_by their agent,
    with `discusses`/`discussed_in` edges to the other agents mentioned.
  - "what recently happened to X"       → Session/Edit nodes carrying who+when,
    linked to the files they touched via `touched_by` / `modified_by`.

Nodes (family `event`):
  Session  session_<agent>_<sid8>   — one transcript; carries first prompt + when.
  Edit     edit_<sid8>_<hash>       — one file modified within a session.
  (Handoff is reserved in the taxonomy; not emitted yet — see _NOTE below.)

Edges (family `temporal`, asserted_by='HARVEST_SESSIONS', asserted_in_session set):
  session --owned_by--> agent           (reuses the structure predicate)
  file    --touched_by--> session       (file read/written during the session)
  file    --modified_by--> edit         (file changed by a write/edit tool call)
  edit    --changed_in_session--> session
  agent   --discussed_in--> session     (another agent discussed in the session)

Idempotent: nodes upsert, edges are INSERT OR IGNORE. Re-running refreshes
last_modified and adds only newly-resolvable edges. Reversible:
  DELETE FROM edges WHERE asserted_by='HARVEST_SESSIONS';
  DELETE FROM nodes WHERE type IN ('Session','Edit','Handoff');

Run standalone:  python3 -m engine.harvesters.sessions
"""
from __future__ import annotations

import hashlib
import inspect
import itertools
import json
import os
import re
import sys
import datetime
import time
from pathlib import Path

from .. import config as C
from .. import db as DB
from .. import resolver as RES
from .. import util as _U
from ..util import slugify, short_hash, now_iso

# Tools whose calls mean "this file was written/changed in the session".
_WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# Tools whose calls mean "this file was read/touched in the session".
_TOUCH_TOOLS = _WRITE_TOOLS | {"Read"}

# Cap per session so one pathological transcript can't explode the graph.
_MAX_FILES_PER_SESSION = 200
# A mentioned agent must appear at least this many times to count as "discussed".
_MENTION_THRESHOLD = 2

# Word-boundary matcher for constellation agent tokens, built from config so the
# active roster is the single source of truth.
_AGENT_TOKEN_RE = re.compile(
    r"\b(" + "|".join(sorted(C.CONSTELLATION_AGENTS, key=len, reverse=True)) + r")\b"
)


def _iter_session_files(refused_out: list | None = None, cache: "_HarvestCache | None" = None):
    'Yield (agent, abs_path, rel_path) for every raw session JSONL under a'
    for abs_path in sorted(C.DESKTOP_ROOT.glob("*/000 */Memory/Sessions/raw/*.jsonl")):
        try:
            rel = abs_path.relative_to(C.DESKTOP_ROOT)
        except ValueError:
            continue
        agent = rel.parts[0]
        # Belt and braces. The roster test below already excludes these — an
        # EXCLUDED_AGENTS key is never in CONSTELLATION_AGENTS — but that is a
        # coincidence of two lists, not an exclusion, and this route is one of the two
        # that feed Session nodes. Stated explicitly so it stays true if the roster
        # relationship ever changes.
        if agent in getattr(C, "EXCLUDED_AGENTS", {}):
            continue
        if agent not in C.CONSTELLATION_AGENTS:
            continue
        reason = _exclusion_reason(scan=_scan(abs_path, rel.as_posix(), cache))
        if reason:
            # Names the agent, the rule and the file — never any transcript CONTENT.
            print(f"[harvest:sessions] refusing {abs_path.name}: {reason} "
                  f"(config.EXCLUDED_AGENTS)", file=sys.stderr)
            if refused_out is not None:
                refused_out.append(rel.as_posix())
            continue
        yield agent, abs_path, rel.as_posix()


# The universe-level session catch-all. `agent-session-autosave.sh` copies EVERY
# live session here (`<BRAIN>/memory/0608_SESSION_DISTILLATIONS/<platform>/<date>_<sid>.jsonl`),
# regardless of cwd — so sessions that ran at HQ/automation/worktree/root level (and
# thus never landed under a constellation agent's Memory/Sessions/raw/) live ONLY here.
# Founder directive 2026-06-16: "the harvester should be indexing everything in
# ARCTURION_Brain." This generator closes that gap; agent-located dups dedupe by the
# deterministic session node id (canonical agent-raw copy is processed first and wins).
_DISTILL_ROOT = C.BRAIN_ROOT / "memory" / "0608_SESSION_DISTILLATIONS"
# Matches the autosave's own `agent_from_cwd`: a constellation home directly under Desktop.
_CWD_AGENT_RE = re.compile(r"/Desktop/([A-Z][A-Z0-9_]+)(?:/|$)")
# Owner assigned when a transcript ran outside any constellation home (cwd = /, HQ,
# a worktree, ARCTURION_HQ/ARCTURION_Brain, etc.). 'HQ' is the prong, not an agent —
# R.resolve('HQ') returns nothing, so no owned_by edge is minted (guarded below),
# which is correct: these are universe-infra sessions, owned by no single agent.
_DISTILL_FALLBACK_OWNER = "HQ"


# ── in-band persona assignment ───────────────────────────────────────────────
# The MATCHER itself now lives in `engine.util.persona_agent_for_text` — deliberately, and
# not for tidiness. It existed here AND in the un-versioned distiller in
# ARCTURION_EXTERNAL_RUNTIME as two hand-maintained copies, and by 2026-08-05 they had
# drifted NINE ways in both directions, each holding defects the other had fixed. Two
# independently-maintained privacy rules is the drift class that caused the original leak.
# util is the only place both sides can reach: those scripts import the Brain, and the
# Brain's walker skip-list excludes their directory, so the dependency cannot be reversed.
# Read that function's comment before changing anything about the rule.

# Roles whose own prose is the MODEL TALKING, not an instruction to it.
_ASSISTANT_ROLES = {"assistant", "model"}


def _instruction_texts(obj, in_assistant: bool = False):
    """Handle source records using the configured exclusion policy."""
    if isinstance(obj, str):
        if not in_assistant:
            yield obj
        return
    if isinstance(obj, list):
        for v in obj:
            yield from _instruction_texts(v, in_assistant)
        return
    if not isinstance(obj, dict):
        return
    role = str(obj.get("role") or "")
    kind = str(obj.get("type") or "")
    scope = in_assistant or role in _ASSISTANT_ROLES or kind in _ASSISTANT_ROLES
    for key, val in obj.items():
        # A tool_use input re-enters instruction scope even inside an assistant record.
        yield from _instruction_texts(
            val, False if (scope and kind == "tool_use" and key == "input") else scope)


def scan_transcript(abs_path: Path) -> dict:
    """One pass over a transcript for everything the exclusion decision needs.

    Returns `{"cwd_agents": [...], "saw_cwd": bool, "persona": str | None}`.

    Reads the WHOLE file rather than stopping at the first match — a session can start in
    ALTAIR and `cd` into an excluded tree later, and an early exit would report ALTAIR and
    never look further. `saw_cwd` is tracked separately from `cwd_agents` because "the cwd
    was the home directory" and "there was no cwd at all" are different facts, and only
    the second is a fail-closed trigger."""
    agents: list[str] = []
    persona: str | None = None
    saw_cwd = False
    prx = _U.persona_re()
    try:
        with abs_path.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                has_cwd = '"cwd"' in line
                # Apply the configured exclusion policy.
                has_escape = "\\u" in line
                if not has_cwd and not has_escape and (
                        prx is None or not prx.search(line)):
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(d, dict):
                    continue
                # Top-level `cwd` is the Claude Code shape; Codex `rollout-*.jsonl`
                # wraps every record as {timestamp, type, payload} and carries it at
                # `payload.cwd`. Reading only the top level made 283 Codex transcripts
                # look like "provenance unknown", which under a fail-closed rule would
                # have de-indexed real work — the fix is to READ the field, not to refuse
                # the file.
                cwd = d.get("cwd")
                if not isinstance(cwd, str) or not cwd:
                    payload = d.get("payload")
                    if isinstance(payload, dict):
                        cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd:
                    saw_cwd = True
                    m = _CWD_AGENT_RE.search(cwd)
                    if m and m.group(1) not in agents:
                        agents.append(m.group(1))
                if persona is None and prx is not None:
                    # Leg 5 / N-2: read EVERY record shape, subtracting assistant prose,
                    # rather than the three shapes the old gate could reach. See
                    # `_instruction_texts` for why this is a subtraction, not a list.
                    for text in _instruction_texts(d):
                        hit = prx.search(text)
                        if hit:
                            persona = hit.group(1)
                            break
    except OSError:
        pass
    return {"cwd_agents": agents, "saw_cwd": saw_cwd, "persona": persona}


def _cwd_agents(abs_path: Path) -> list[str]:
    """Every distinct Desktop-level home name in a transcript's `cwd` fields, in
    first-seen order. Thin wrapper over `scan_transcript` for callers that only care
    about the working directory."""
    return scan_transcript(abs_path)["cwd_agents"]


def _agent_from_distillation(abs_path: Path) -> str:
    """Derive the owning constellation agent from a 0608 transcript by scanning its
    `cwd` fields. Returns the first cwd that resolves to a known constellation agent,
    else `_DISTILL_FALLBACK_OWNER` ('HQ'). Mirrors agent-session-autosave.sh routing."""
    for name in _cwd_agents(abs_path):
        if name in C.CONSTELLATION_AGENTS:
            return name
    return _DISTILL_FALLBACK_OWNER


def _excluded_agent_in_transcript(names) -> str | None:
    """The `config.EXCLUDED_AGENTS` key a transcript ran under, if any."""
    for name in names:
        if name in getattr(C, "EXCLUDED_AGENTS", {}):
            return name
    return None


def _exclusion_reason(abs_path: Path | None = None, *,
                      scan: dict | None = None) -> str | None:
    """The single exclusion rule for BOTH session shelves, or None to admit.

    Leg 5 made this one function because it was two. The 0608 catch-all asked three
    questions and the agent-raw shelf asked none, which is how the shelf that runs first
    and wins deduplication came to be the only unscanned route into the index (N-1). A
    rule that lives in one place cannot be enforced on one caller and forgotten on
    another — the failure this workstream has now repeated at four different scales.

    The three questions, in order:

      1. WHERE did it run?       — an excluded agent's home in any `cwd` in the file.
      2. WHAT was it told to be? — in-band persona assignment, which no cwd can reveal.
      3. Do we actually KNOW?    — no working directory anywhere means no evidence, and
         absence of evidence must not read as innocence. The condition is `saw_cwd`, not
         "cwd resolved to an agent": the home directory IS a determinable working
         directory, and treating it as unknown would refuse most legitimate sessions.

    Pass an already-computed `scan` when the caller also needs the cwd list — a scan reads
    the whole file, and scanning twice to ask two questions about one file would double a
    cost Leg 4 already had to justify.

    The reason string names the agent, the rule and the file. It never carries transcript
    CONTENT: logging what a privacy filter removed would defeat the filter."""
    if scan is None:
        scan = scan_transcript(abs_path)
    excluded = _excluded_agent_in_transcript(scan["cwd_agents"])
    if excluded:
        return f"session ran under excluded agent {excluded!r}"
    if scan["persona"]:
        return (f"session was assigned the {scan['persona']!r} persona in-band "
                f"(cwd shows no agent home)")
    if not scan["saw_cwd"]:
        return ("no working directory could be determined from the transcript — "
                "failing closed")
    return None


# Where a DERIVED, human-readable distillation of a session can live. The purge scopes
# its session-id match to these so an 8-hex id cannot collide its way into deleting an
# unrelated node.
#
# Cover both configured distillation shelf layouts.
_DISTILLED_SHELF_PATTERNS = (
    "%SESSION_DISTILLATIONS%",          # the Brain's 0608 universe catch-all
    "%/Memory/Sessions/distilled/%",    # the distiller's primary per-agent shelf
)


def purge_excluded_sessions(conn, refused_rels, verbose: bool = True) -> dict:
    """Handle source records using the configured exclusion policy."""
    refused = [r for r in refused_rels if r]
    if not refused:
        return {"removed": 0}
    ids: list[str] = []
    for rel in refused:
        ids += [r[0] for r in conn.execute(
            "SELECT id FROM nodes WHERE path = ? OR path LIKE ?", (rel, rel + "#%"))]
        # Route 3 — derived distillations keyed by the session id in the filename.
        stem = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        sid8 = (stem.split("_", 1)[1] if "_" in stem else stem).replace("-", "")[:8]
        if len(sid8) >= 8:
            for shelf in _DISTILLED_SHELF_PATTERNS:
                ids += [r[0] for r in conn.execute(
                    "SELECT id FROM nodes WHERE path LIKE ? AND path LIKE ?",
                    (shelf, f"%{sid8}%"))]
    ids = sorted(set(ids))
    if not ids:
        return {"removed": 0}
    conn.execute("DROP TABLE IF EXISTS _exclsess")
    conn.execute("CREATE TEMP TABLE _exclsess (id TEXT PRIMARY KEY)")
    conn.executemany("INSERT OR IGNORE INTO _exclsess (id) VALUES (?)",
                     [(i,) for i in ids])
    conn.execute("DELETE FROM edges WHERE from_id IN (SELECT id FROM _exclsess) "
                 "OR to_id IN (SELECT id FROM _exclsess)")
    conn.execute("DELETE FROM nodes WHERE id IN (SELECT id FROM _exclsess)")
    for table in ("fts_nodes", "embeddings"):
        try:
            conn.execute(f"DELETE FROM {table} WHERE node_id IN "
                         f"(SELECT id FROM _exclsess)")
        except Exception as exc:  # noqa: BLE001
            if "no such table" not in str(exc).lower():
                raise
    conn.execute("DROP TABLE IF EXISTS _exclsess")
    conn.commit()
    if verbose:
        # Counts and ids only. Never the summaries — reporting the content of a privacy
        # purge into a log defeats the purge.
        print(f"[harvest:sessions] purged {len(ids)} previously-indexed rows from "
              f"{len(refused)} excluded-agent transcripts", file=sys.stderr)
    return {"removed": len(ids)}


def _iter_distillation_files(refused_out: list | None = None, cache: "_HarvestCache | None" = None):
    """Handle source records using the configured exclusion policy."""
    if not _DISTILL_ROOT.is_dir():
        return
    for abs_path in _DISTILL_ROOT.rglob("*.jsonl"):
        try:
            rel = abs_path.relative_to(C.DESKTOP_ROOT)
        except ValueError:
            continue
        # The three questions — where it ran, what it was told to be, and whether we
        # actually know — now live in `_exclusion_reason`, shared with the agent-raw
        # shelf, so they cannot be enforced here and forgotten there. One scan serves
        # both: this route also needs `cwd_agents` to choose the owner.
        scan = _scan(abs_path, rel.as_posix(), cache)
        names = scan["cwd_agents"]
        reason = _exclusion_reason(scan=scan)

        if reason:
            # Names the agent, the reason and the file. NEVER any transcript content —
            # logging what a privacy purge removed would defeat the purge.
            print(f"[harvest:sessions] refusing {abs_path.name}: {reason} "
                  f"(config.EXCLUDED_AGENTS)", file=sys.stderr)
            if refused_out is not None:
                refused_out.append(rel.as_posix())
            continue

        agent = next((n for n in names if n in C.CONSTELLATION_AGENTS),
                     _DISTILL_FALLBACK_OWNER)
        yield agent, abs_path, rel.as_posix()


def _message_text_blocks(msg: dict):
    """Yield plain-text strings from a Claude Code message envelope's content."""
    content = msg.get("content")
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                yield b["text"]


def _parse_session(abs_path: Path) -> dict:
    """One pass over a transcript → the facts the harvester needs."""
    first_prompt = None
    first_ts = None
    last_ts = None
    n_messages = 0
    edited: dict[str, set] = {}   # abs file path -> set of write tool names
    touched: set[str] = set()     # abs file paths read or written
    mentions: dict[str, int] = {}

    def _note_ts(ts):
        nonlocal first_ts, last_ts
        if not ts:
            return
        if first_ts is None or ts < first_ts:
            first_ts = ts
        if last_ts is None or ts > last_ts:
            last_ts = ts

    with abs_path.open(encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue
            _note_ts(d.get("timestamp"))
            dtype = d.get("type")

            # The enqueue that started the session carries the founder's prompt.
            if dtype == "queue-operation" and d.get("operation") == "enqueue":
                c = d.get("content")
                if isinstance(c, str) and first_prompt is None:
                    first_prompt = c

            msg = d.get("message") if isinstance(d.get("message"), dict) else None
            if dtype in ("user", "assistant") or msg:
                n_messages += 1
            if not msg:
                # non-message lines still contribute mention text via top-level content
                top = d.get("content")
                if isinstance(top, str):
                    for m in _AGENT_TOKEN_RE.finditer(top):
                        mentions[m.group(1)] = mentions.get(m.group(1), 0) + 1
                continue

            for text in _message_text_blocks(msg):
                if first_prompt is None and msg.get("role") == "user":
                    first_prompt = text
                for m in _AGENT_TOKEN_RE.finditer(text):
                    mentions[m.group(1)] = mentions.get(m.group(1), 0) + 1

            content = msg.get("content")
            if isinstance(content, list):
                for b in content:
                    if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                        continue
                    name = b.get("name")
                    if name not in _TOUCH_TOOLS:
                        continue
                    inp = b.get("input") or {}
                    fp = inp.get("file_path") or inp.get("notebook_path")
                    if not fp:
                        continue
                    touched.add(fp)
                    if name in _WRITE_TOOLS:
                        edited.setdefault(fp, set()).add(name)

    return {
        "first_prompt": first_prompt,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "n_messages": n_messages,
        "edited": edited,
        "touched": touched,
        "mentions": mentions,
    }


def _build_path_index(conn) -> tuple[dict, dict]:
    """Two indexes for resolving an edited file's absolute path to a node id:
    exact Desktop-relative path → id, and basename → id (live preferred, unique only)."""
    by_path: dict[str, str] = {}
    by_base: dict[str, str | None] = {}   # basename -> id, or None if ambiguous
    by_base_archive: dict[str, bool] = {}
    for r in conn.execute("SELECT id, path FROM nodes WHERE path IS NOT NULL").fetchall():
        path = r["path"]
        nid = r["id"]
        by_path.setdefault(path, nid)
        base = path.rsplit("/", 1)[-1]
        archive = RES.is_archive_path(path)
        if base not in by_base:
            by_base[base] = nid
            by_base_archive[base] = archive
        elif by_base[base] is not None:
            # collision: keep a live winner over an archive one; otherwise mark ambiguous
            if by_base_archive.get(base) and not archive:
                by_base[base] = nid
                by_base_archive[base] = archive
            elif archive:
                pass  # incoming archive loses; keep current
            else:
                by_base[base] = None  # two live copies → ambiguous, don't guess
    return by_path, by_base


def _resolve_file(abs_fp: str, by_path: dict, by_base: dict) -> str | None:
    """Absolute edit-tool path → node id, or None if outside the indexed prongs."""
    try:
        rel = os.path.relpath(abs_fp, str(C.DESKTOP_ROOT))
    except ValueError:
        return None
    if rel.startswith(".."):
        return None  # outside Desktop (e.g. ~/.claude/...) — not indexed
    hit = by_path.get(rel)
    if hit:
        return hit
    base = rel.rsplit("/", 1)[-1]
    return by_base.get(base)  # unique-basename fallback (None if ambiguous/missing)


# ── incremental cache (2026-11-07) ──────────────────────────────────────────
# The nightly reindex re-read every transcript twice — once for the privacy scan, once to
# parse — about 21 GB and 38k files, of which ~20 change on a given day. By mid-September
# that one step took ~9,100 s, and from 2026-09-18 the reindex hit its 3-hour ceiling every
# night before getting past this harvester. The cache below skips work whose answer cannot
# have changed:
#
#   * SCAN verdicts are a pure function of the file's bytes and the exclusion rules. A
#     cached scan is reused only when the file's (size, mtime_ns, inode) AND the rules
#     fingerprint both match. The fingerprint hashes the source of every function in the
#     privacy decision plus the excluded-agent and roster lists, so ANY change to the rule,
#     the persona pattern or EXCLUDED_AGENTS throws the whole scan cache away. A refused
#     file therefore stays refused, and `purge_excluded_sessions` sees the same list.
#   * HARVEST work is skipped only when the file is unchanged, the harvest code is
#     unchanged, and the Session node is still in the DB. A rotating 1/7 of the unchanged
#     sessions is re-harvested each night anyway, so `touched_by` edges to files indexed
#     after a session was first harvested still arrive within a week.
#
# Opt out for one run with ARCTURION_SESSIONS_NO_CACHE=1 (full rescan, cache rewritten).
# Lives beside the DB it describes (index/, which the walker never indexes). Resolved at
# call time so a test that repoints C.BRAIN_DB never touches the live cache.
_CACHE_NAME = "session_harvest_cache.json"
_CACHE_VERSION = 1
_REFRESH_BUCKETS = 7
_WRITE_SLICE_S = 2.0   # hold the write lock at most this long ...
_WRITE_YIELD_S = 0.25  # ... then pause so other writers get in


def _fingerprint(*parts) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(repr(part).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:24]


def _rules_fingerprint() -> str:
    rx = _U.persona_re()
    return _fingerprint(
        _CACHE_VERSION,
        inspect.getsource(scan_transcript), inspect.getsource(_instruction_texts),
        inspect.getsource(_exclusion_reason), inspect.getsource(_excluded_agent_in_transcript),
        inspect.getsource(_U.persona_re), _CWD_AGENT_RE.pattern, _ASSISTANT_ROLES,
        rx.pattern if rx is not None else None,
        sorted(getattr(C, "EXCLUDED_AGENTS", {}) or {}), sorted(C.CONSTELLATION_AGENTS),
    )


def _harvest_fingerprint(rules: str) -> str:
    return _fingerprint(
        rules, inspect.getsource(_parse_session), inspect.getsource(_message_text_blocks),
        inspect.getsource(harvest), _AGENT_TOKEN_RE.pattern, _MAX_FILES_PER_SESSION,
        _MENTION_THRESHOLD, sorted(_TOUCH_TOOLS), sorted(_WRITE_TOOLS),
    )


def _file_sig(abs_path: Path) -> list | None:
    try:
        st = abs_path.stat()
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns, st.st_ino]


class _HarvestCache:
    """On-disk memo of scan verdicts and harvested sessions. See the block comment above."""

    def __init__(self, path: Path | None = None, enabled: bool = True):
        self.path = Path(path) if path else Path(C.BRAIN_DB).parent / _CACHE_NAME
        self.rules = _rules_fingerprint()
        self.harvest_fp = _harvest_fingerprint(self.rules)
        self.scans: dict = {}
        self.harvested: dict = {}
        self.hits = {"scan": 0, "harvest": 0}
        self.misses = {"scan": 0, "harvest": 0}
        if not enabled:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("version") != _CACHE_VERSION:
            return
        if data.get("rules") == self.rules:
            self.scans = data.get("scans") or {}
            if data.get("harvest") == self.harvest_fp:
                self.harvested = data.get("harvested") or {}

    def scan(self, abs_path: Path, rel: str) -> dict:
        sig = _file_sig(abs_path)
        hit = self.scans.get(rel)
        if sig is not None and hit and hit.get("sig") == sig:
            self.hits["scan"] += 1
            return hit["scan"]
        self.misses["scan"] += 1
        result = scan_transcript(abs_path)
        if sig is not None:
            self.scans[rel] = {"sig": sig, "scan": result}
        return result

    def can_skip_harvest(self, conn, abs_path: Path, rel: str, sess_node: str) -> bool:
        hit = self.harvested.get(rel)
        if not hit or hit.get("node") != sess_node or hit.get("sig") != _file_sig(abs_path):
            return False
        # Rotating refresh: 1/7 of unchanged sessions re-harvest each night.
        bucket = int(hashlib.sha256(rel.encode("utf-8", "replace")).hexdigest(), 16)
        if bucket % _REFRESH_BUCKETS == datetime.date.today().toordinal() % _REFRESH_BUCKETS:
            return False
        if conn.execute("SELECT 1 FROM nodes WHERE id = ?", (sess_node,)).fetchone() is None:
            return False
        return True

    def mark_harvested(self, abs_path: Path, rel: str, sess_node: str) -> None:
        sig = _file_sig(abs_path)
        if sig is not None:
            self.harvested[rel] = {"sig": sig, "node": sess_node}

    def save(self, live_rels: set | None = None) -> None:
        if live_rels is not None:  # drop entries for transcripts that no longer exist
            self.scans = {k: v for k, v in self.scans.items() if k in live_rels}
            self.harvested = {k: v for k, v in self.harvested.items() if k in live_rels}
        tmp = self.path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps({
                "version": _CACHE_VERSION, "rules": self.rules, "harvest": self.harvest_fp,
                "saved_at": now_iso(), "scans": self.scans, "harvested": self.harvested,
            }, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            print(f"[harvest:sessions] could not save cache ({exc}); next run rescans",
                  file=sys.stderr)


def _scan(abs_path: Path, rel: str, cache: "_HarvestCache | None") -> dict:
    return cache.scan(abs_path, rel) if cache is not None else scan_transcript(abs_path)


def harvest(conn=None, verbose: bool = True) -> dict:
    t0 = time.time()
    own = conn is None
    if own:
        conn = DB.connect()
    DB.init_db(conn)  # ensures the asserted_in_session column exists

    R = RES.build_resolution_index(conn)
    by_path, by_base = _build_path_index(conn)
    asserted_at = now_iso()

    sessions = edits = edges = files = skipped = 0
    last_commit = time.time()
    cache = _HarvestCache(enabled=os.environ.get("ARCTURION_SESSIONS_NO_CACHE") != "1")
    live_rels: set[str] = set()

    # Canonical agent-raw sessions FIRST, then the Brain's 0608 universe catch-all.
    # `seen` dedupes by deterministic session node id so an agent-located session that
    # also has a 0608 mirror is processed once (the agent-raw copy, with its proper
    # owner + `<AGENT>/...` path, wins); 0608-only sessions are added net-new.
    seen: set[str] = set()
    refused_rels: list[str] = []   # transcripts refused for EXCLUDED_AGENTS — purged below
    for agent, abs_path, rel_path in itertools.chain(
        _iter_session_files(refused_out=refused_rels, cache=cache),
        _iter_distillation_files(refused_out=refused_rels, cache=cache),
    ):
        # session id = the uuid embedded in the filename "<date>_<uuid>.jsonl"
        stem = abs_path.stem
        sid = stem.split("_", 1)[1] if "_" in stem else stem
        sid8 = sid.replace("-", "")[:8] or short_hash(rel_path)
        date = stem.split("_", 1)[0]

        sess_node = f"session_{slugify(agent)}_{sid8}"
        live_rels.add(rel_path)  # keeps this file's scan verdict cached, dup or not
        if sess_node in seen:
            continue  # already harvested from the canonical agent-raw location
        seen.add(sess_node)

        if cache.can_skip_harvest(conn, abs_path, rel_path, sess_node):
            skipped += 1
            continue

        info = _parse_session(abs_path)
        summary = (info["first_prompt"] or f"{agent} session {date}").strip()[:300]

        DB.upsert_node(conn, {
            "id": sess_node, "type": "Session", "family": "event",
            "title": f"{agent} session {date} {sid8}",
            "path": rel_path, "owner": agent, "status": "completed",
            "governance_tier": 0, "content_hash": None,
            "summary": summary, "body_excerpt": None, "centrality": 0.0,
            "created": info["first_ts"], "last_modified": info["last_ts"] or now_iso(),
            "last_reviewed": None,
            "metadata": {
                "session_id": sid, "agent": agent, "date": date,
                "n_messages": info["n_messages"],
                "n_edited_files": len(info["edited"]),
                "edited_basenames": sorted({p.rsplit("/", 1)[-1] for p in info["edited"]})[:30],
            },
        })
        sessions += 1

        # session --owned_by--> agent
        agent_node = R.resolve(agent)
        if agent_node:
            if DB.insert_edge(conn, {
                "from": sess_node, "to": agent_node, "predicate": "owned_by",
                "family": "structure", "weight": 1.0, "confidence": 1.0,
                "asserted_by": "HARVEST_SESSIONS", "asserted_at": asserted_at,
                "evidence_span": rel_path, "asserted_in_session": sess_node,
            }):
                edges += 1

        # files touched / modified
        seen_files = 0
        for fp in sorted(info["touched"]):
            if seen_files >= _MAX_FILES_PER_SESSION:
                break
            fid = _resolve_file(fp, by_path, by_base)
            if not fid or fid == sess_node:
                continue
            seen_files += 1
            files += 1
            # file --touched_by--> session
            if DB.insert_edge(conn, {
                "from": fid, "to": sess_node, "predicate": "touched_by",
                "family": "temporal", "weight": 1.0, "confidence": 0.9,
                "asserted_by": "HARVEST_SESSIONS", "asserted_at": asserted_at,
                "evidence_span": fp, "asserted_in_session": sess_node,
            }):
                edges += 1
            # if it was written, mint an Edit change-event node + its edges
            tools = info["edited"].get(fp)
            if tools:
                rel_fp = os.path.relpath(fp, str(C.DESKTOP_ROOT))
                eid = f"edit_{sid8}_{short_hash(rel_fp)}"
                DB.upsert_node(conn, {
                    "id": eid, "type": "Edit", "family": "event",
                    "title": f"edit: {fp.rsplit('/', 1)[-1]}",
                    "path": f"{rel_path}#{fp.rsplit('/', 1)[-1]}",
                    "owner": agent, "status": "completed", "governance_tier": 0,
                    "content_hash": None,
                    "summary": f"{agent} {'/'.join(sorted(tools))} {fp.rsplit('/', 1)[-1]} in session {sid8}",
                    "body_excerpt": None, "centrality": 0.0,
                    "created": info["last_ts"], "last_modified": info["last_ts"] or now_iso(),
                    "last_reviewed": None,
                    "metadata": {"session_id": sid, "agent": agent,
                                 "file": rel_fp, "tools": sorted(tools)},
                })
                edits += 1
                # file --modified_by--> edit ; edit --changed_in_session--> session
                if DB.insert_edge(conn, {
                    "from": fid, "to": eid, "predicate": "modified_by",
                    "family": "temporal", "weight": 1.0, "confidence": 0.9,
                    "asserted_by": "HARVEST_SESSIONS", "asserted_at": asserted_at,
                    "evidence_span": fp, "asserted_in_session": sess_node,
                }):
                    edges += 1
                if DB.insert_edge(conn, {
                    "from": eid, "to": sess_node, "predicate": "changed_in_session",
                    "family": "temporal", "weight": 1.0, "confidence": 1.0,
                    "asserted_by": "HARVEST_SESSIONS", "asserted_at": asserted_at,
                    "evidence_span": rel_path, "asserted_in_session": sess_node,
                }):
                    edges += 1

        # cross-agent: other agents discussed in this session
        for token, count in info["mentions"].items():
            if token == agent or count < _MENTION_THRESHOLD:
                continue
            aid = R.resolve(token)
            if not aid or aid == agent_node:
                continue
            if DB.insert_edge(conn, {
                "from": aid, "to": sess_node, "predicate": "discussed_in",
                "family": "temporal", "weight": min(1.0, count / 10.0), "confidence": 0.7,
                "asserted_by": "HARVEST_SESSIONS", "asserted_at": asserted_at,
                "evidence_span": f"mentioned x{count}", "asserted_in_session": sess_node,
            }):
                edges += 1

        cache.mark_harvested(abs_path, rel_path, sess_node)
        # Commit on time and yield the write lock, whoever owns the connection: called from
        # graph_builder (own=False) this loop used to hold one transaction for the whole
        # harvest — hours — blocking every other Brain writer. Idempotent writes, so a
        # partial harvest that is committed is safe.
        if (own and sessions % 25 == 0) or (time.time() - last_commit) > _WRITE_SLICE_S:
            conn.commit()
            time.sleep(_WRITE_YIELD_S)
            last_commit = time.time()
        if sessions % 2000 == 0:
            conn.commit()
            cache.save()  # progress survives a kill; entries are only written after commit

    conn.commit()
    live_rels.update(refused_rels)
    cache.save(live_rels)

    # Retire rows an EARLIER harvest created from transcripts we now refuse. Runs every
    # pass, not as a migration: a filter that only stops the next row leaves the ones
    # already indexed in place forever, which is the 2026-07-31 DB-purge mistake wearing
    # a different hat.
    purged = purge_excluded_sessions(conn, refused_rels, verbose=verbose)
    conn.commit()

    elapsed = time.time() - t0
    if own:
        conn.close()
    if verbose:
        print(f"[harvest:sessions] {sessions} sessions, {edits} edits, "
              f"{files} file-touches, {edges} edges in {elapsed:.1f}s "
              f"({skipped} unchanged skipped; scan cache {cache.hits['scan']} hit / "
              f"{cache.misses['scan']} read)")
    return {"family": "session", "nodes": sessions + edits, "sessions": sessions,
            "edits": edits, "edges": edges, "excluded_transcripts": len(refused_rels),
            "unchanged_skipped": skipped,
            "excluded_rows_purged": purged["removed"],
            "elapsed_sec": round(elapsed, 2)}


if __name__ == "__main__":
    print(harvest())
