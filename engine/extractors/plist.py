"""LaunchAgent/LaunchDaemon plist extractor — emits 'executes' edges."""

from __future__ import annotations
import plistlib
import re
import shlex
from pathlib import Path
from datetime import datetime
from .. import config as C
from ..util import make_node_id, content_hash, truncate, now_iso, slugify, short_hash
from .markdown import _rel_to_universe


_INTERPRETER_RE = re.compile(r"^(?:python(?:\d+(?:\.\d+)*)?|bash|zsh|sh)$")


def _is_interpreter(value: str) -> bool:
    return bool(_INTERPRETER_RE.fullmatch(Path(value).name))


def _declared_path(value: str) -> str | None:
    """Return literal absolute paths only; environment expansion is runtime-dependent."""
    return value if value.startswith("/") else None


def _interpreter_script(argv: list[str]) -> str | None:
    """Return only the interpreter's direct script operand under a bounded grammar."""
    no_value_options = {"-B", "-E", "-I", "-O", "-OO", "-P", "-S", "-s", "-u"}
    pos = 0
    while pos < len(argv) and argv[pos] in no_value_options:
        pos += 1
    if pos >= len(argv) or argv[pos].startswith("-"):
        # -c/-m and unknown/value-taking options have no statically provable script slot.
        return None
    return _declared_path(argv[pos])


def extract_plist(path: Path, default_type: str = "Automation") -> tuple[dict, list[dict]]:
    try:
        raw = path.read_bytes()
    except Exception as exc:
        return _bare_plist_node(path, b"", f"read:{type(exc).__name__}: {exc}"), []

    try:
        pl = plistlib.loads(raw)
    except Exception as exc:
        return _bare_plist_node(path, raw, f"parse:{type(exc).__name__}: {exc}"), []

    if not isinstance(pl, dict) or not any(k in pl for k in ('Label','Program','ProgramArguments')):
        # Property lists also carry application metadata. Their existence proves
        # neither a launchd service nor a configured executable.
        stat=path.stat()
        return {'id':make_node_id(path,path.stem),'type':'Config','family':'config',
                'title':path.stem,'path':_rel_to_universe(path),'owner':'FOUNDER',
                'status':'reference','governance_tier':0,'content_hash':content_hash(raw),
                'summary':f'Property-list metadata; root type {type(pl).__name__}.',
                'body_excerpt':None,'centrality':0.0,'created':None,
                'last_modified':datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds'),
                'last_reviewed':None,'metadata':{'plist_root_type':type(pl).__name__,
                    'entry_count':len(pl) if isinstance(pl,(list,dict)) else None,
                    'runtime_observed':False}}, []

    label = pl.get("Label") or path.stem
    program_args = pl.get("ProgramArguments") or []
    program = pl.get("Program") or (program_args[0] if program_args else None)
    run_at_load = bool(pl.get("RunAtLoad"))
    start_interval = pl.get("StartInterval")
    start_calendar = pl.get("StartCalendarInterval")
    keep_alive = pl.get("KeepAlive")
    user = pl.get("UserName")

    rel = _rel_to_universe(path)
    stat = path.stat()
    node_id = make_node_id(path, label)
    summary_bits = [f"LaunchAgent {label}"]
    if start_interval:
        summary_bits.append(f"every {start_interval}s")
    if start_calendar:
        summary_bits.append("calendar-scheduled")
    if run_at_load:
        summary_bits.append("RunAtLoad")
    if keep_alive:
        summary_bits.append("KeepAlive")

    node = {
        "id": node_id,
        "type": default_type,
        "family": "automation",
        "title": label,
        "path": rel,
        "owner": user or "FOUNDER",
        "status": "configured",
        "governance_tier": 1,
        "content_hash": content_hash(raw),
        "summary": ", ".join(summary_bits),
        "body_excerpt": truncate(str({k: v for k, v in pl.items() if k != "EnvironmentVariables"}), 2000),
        "centrality": 0.0,
        "created": None,
        "last_modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "last_reviewed": None,
        "metadata": {
            "label": label,
            "program": program,
            "program_args": [str(x) for x in program_args],
            "run_at_load": run_at_load,
            "start_interval": start_interval,
            "keep_alive": bool(keep_alive),
        },
    }

    edges: list[dict] = []
    asserted_at = now_iso()
    service_id = f"service_{slugify(str(label))}"
    service_decl = {
        "id": service_id, "type": "Service", "family": "application",
        "title": str(label), "path": None, "owner": user or "FOUNDER",
        "status": "configured", "governance_tier": 1, "content_hash": None,
        "summary": f"Service declared by LaunchAgent {label}", "body_excerpt": None,
        "centrality": 0.0, "created": None, "last_modified": node["last_modified"],
        "last_reviewed": None,
        "metadata": {"identity_basis": "launchd_label", "label": str(label),
                     "source_manifest": rel, "runtime_observed": False},
    }
    edges.append({
        "from": node_id, "to": service_id, "predicate": "configures", "family": "code",
        "weight": 1.0, "confidence": 1.0, "asserted_by": "STATIC_PLIST",
        "asserted_at": asserted_at, "evidence_span": "plist:Label",
        "metadata": {"endpoint_node": service_decl},
    })

    # Proven execution grammar only: Program/argv[0], plus one script consumed by a
    # recognized interpreter. Other absolute arguments may be state, input, output, or log
    # paths and remain opaque metadata rather than invented executable dependencies.
    argv = [str(x) for x in program_args]
    targets: list[str] = [str(program)] if program else []
    executable = str(program or (argv[0] if argv else ""))
    tail = argv[1:] if argv and argv[0] == executable else argv
    interpreter = _is_interpreter(executable)
    if interpreter and tail:
        if Path(executable).name in {"bash", "zsh", "sh"} and tail[0] in {"-c", "-lc"}:
            try:
                command = shlex.split(tail[1]) if len(tail) > 1 else []
            except ValueError:
                command = []
            if command and command[0] == "exec":
                command = command[1:]
            command_program = _declared_path(command[0]) if command else None
            if command_program:
                targets.append(command_program)
                if _is_interpreter(command_program):
                    script = _interpreter_script(command[1:])
                    if script:
                        targets.append(script)
        else:
            script = _interpreter_script(tail)
            if script:
                targets.append(script)

    seen_targets: set[str] = set()
    for target in targets:
        if target in seen_targets:
            continue
        seen_targets.add(target)
        target_id = f"executable_{slugify(Path(target).name)}_{short_hash(target)}"
        endpoint = {
            "id": target_id, "type": "Executable", "family": "application",
            "title": Path(target).name or target, "path": target,
            "owner": "FOUNDER", "status": "configured", "governance_tier": 1,
            "content_hash": None, "summary": f"Executable declared by {label}",
            "body_excerpt": None, "centrality": 0.0, "created": None,
            "last_modified": node["last_modified"], "last_reviewed": None,
            "metadata": {"identity_basis": "launchd_program_argument",
                         "source_manifest": rel, "path_exists": Path(target).exists(),
                         "runtime_observed": False},
        }
        edges.append({
            "from": service_id, "to": target_id,
            "predicate": "executes", "family": "code",
            "weight": 1.0, "confidence": 0.98,
            "asserted_by": "STATIC_PLIST", "asserted_at": asserted_at,
            "evidence_span": "plist:ProgramArguments",
            "metadata": {"endpoint_node": endpoint,
                         "program_args": [str(x) for x in program_args]},
        })
    return node, edges


def _bare_plist_node(path: Path, raw: bytes, error: str) -> dict:
    stat = path.stat()
    rel = _rel_to_universe(path)
    return {
        "id": make_node_id(path, path.stem),
        "type": "Automation", "family": "automation",
        "title": path.stem, "path": rel, "owner": "FOUNDER",
        "status": "error", "governance_tier": 1,
        "content_hash": content_hash(raw), "summary": f"Invalid plist: {error}",
        "body_excerpt": None, "centrality": 0.0,
        "created": None,
        "last_modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "last_reviewed": None, "metadata": {"extract_error": error},
    }
