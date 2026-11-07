"""Shared enforcement gate — ONE implementation behind BOTH transports.

Rung 2 of the universe's three-rung enforcement floor (Cross-Harness Universe
Contract, Class 3) is the *universal* floor: `brain_validate_write` and
`brain_check_risk` must be reachable by any harness that speaks MCP **or has a
shell**. Until 2026-08-04 this logic lived inline in `mcp_server.py` and there
was no HTTP route at all — so the floor was in practice a Claude-and-Codex gate
wearing a floor's name (Antigravity, Gemini, Hermes and any bare shell could
recall from the Brain but could not be gated by it).

This module holds the logic exactly once:

    mcp_server.py   → MCP tools  brain_validate_write / brain_check_risk
    http_server.py  → POST /validate / POST /check_risk

Neither transport owns a copy, so the two verdicts cannot drift. The argument
alias table lives here too, for the same reason: an HTTP caller passing `path`
instead of `file_path` must get the *identical* verdict an MCP caller gets.

Both entry points return the shapes the MCP tool contract promises:

    validate_write → {decision, errors[]}                       (+ raw_stderr on block)
    check_risk     → {decision, rule_id, message, amber_warnings[]} (+ raw_stderr on block)

An unavailable policy hook is a degraded governance state, not permission. Both
gates fail closed and add ``infrastructure_failure`` so clients and health probes
can distinguish policy denial from missing, timed-out, crashed, or errored
enforcement infrastructure.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from . import config as C

# The canonical PreToolUse hooks. These ARE the policy; this module is only a
# transport-neutral way to invoke them. Do not reimplement their rules here —
# a second copy of the policy is how a floor silently becomes two floors.
BAN_RISK_HOOK = C.POLICY_HOOK_ROOT / "ban-risk-guardrail.py"
BRAIN_WRITE_VALIDATOR_HOOK = C.POLICY_HOOK_ROOT / "brain-write-validator.py"


# ── Argument normalization (shared with mcp_server; see its ARG_ALIASES note) ──
# Callers pass reasonable synonyms — `path` for `file_path`, `content`/`new_string`
# for the body, `cmd` for `command`. The MCP transport has normalized these since
# 2026-07-31; the HTTP transport must apply the SAME table or the same logical
# input yields different verdicts on different harnesses, which is precisely the
# drift Rung 2 exists to prevent.
ARG_ALIASES: dict[str, dict[str, str]] = {
    "brain_validate_write": {
        "path": "file_path", "file": "file_path",
        "body": "content", "new_string": "content", "text": "content",
        "tool": "tool_name",
    },
    "brain_check_risk": {"cmd": "command", "shell": "command", "action": "command"},
}

# Declared fields per tool. Kept in lock-step with mcp_server.TOOLS by
# `assert_schema_parity()` below, which mcp_server calls at import time.
KNOWN_FIELDS: dict[str, set[str]] = {
    "brain_validate_write": {"file_path", "content", "tool_name"},
    "brain_check_risk": {"command", "tool_name", "file_path"},
}

REQUIRED_ARGS: dict[str, list[str]] = {
    "brain_validate_write": ["file_path", "content"],
    "brain_check_risk": ["command"],
}


def normalize_args(tool: str, args: dict) -> tuple[dict, list, list]:
    """Resolve synonym arguments onto canonical fields.

    Returns `(args, aliased, unknown)`. `aliased` lists the "given → canonical"
    remappings applied; `unknown` lists arguments that matched nothing and would
    otherwise have been discarded in silence. Aliases never clobber a value the
    caller supplied under the canonical name.
    """
    aliases = ARG_ALIASES.get(tool, {})
    known = KNOWN_FIELDS.get(tool, set())
    out: dict = {}
    aliased: list[str] = []
    unknown: list[str] = []
    for key, value in args.items():
        if key in known:
            out[key] = value
            continue
        canonical = aliases.get(key)
        if canonical:
            if args.get(canonical) not in (None, ""):
                unknown.append(f"{key} (ignored — {canonical} was also supplied)")
            else:
                out[canonical] = value
                aliased.append(f"{key} → {canonical}")
        else:
            unknown.append(key)
    return out, aliased, unknown


def missing_required(tool: str, args: dict) -> list:
    return [f for f in REQUIRED_ARGS.get(tool, []) if args.get(f) in (None, "")]


def missing_args_error(tool: str, args: dict, missing: list,
                       aliased: list, unknown: list) -> dict:
    """The readable required-field contract both transports return."""
    return {
        "error": "missing_required_arguments",
        "tool": tool,
        "missing": missing,
        "required": REQUIRED_ARGS.get(tool, []),
        "accepted": sorted(KNOWN_FIELDS.get(tool, set())),
        "received": sorted(args),
        "aliased": aliased,
        "unrecognized": unknown,
    }


def assert_schema_parity(tool_schemas: dict[str, set[str]]) -> None:
    """Fail loudly (to stderr) if a transport's declared schema drifts from KNOWN_FIELDS.

    Presence is not parity: if mcp_server's TOOLS entry grows a field this module
    does not know about, the HTTP transport would silently discard it as
    'unrecognized' while MCP accepted it — the same class of silent divergence
    that made an MCP-only gate look like a floor. This does not raise: a schema
    mismatch must never take the MCP server down, but it must never be quiet.
    """
    for tool, declared in tool_schemas.items():
        shared = KNOWN_FIELDS.get(tool)
        if shared is None:
            continue
        if declared != shared:
            print(
                f"[enforcement] SCHEMA DRIFT on {tool}: transport declares "
                f"{sorted(declared)}, shared floor knows {sorted(shared)}. "
                f"Update engine/enforcement.KNOWN_FIELDS.",
                file=sys.stderr,
            )


# ── Hook invocation ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class HookResult:
    exit_code: int | None
    stderr: str
    infrastructure_failure: str | None = None


def run_hook(hook_path: Path, payload: dict, timeout: float = 5.0) -> HookResult:
    """Invoke one policy hook and classify infrastructure failure explicitly."""
    if not hook_path.exists():
        detail = f"hook missing: {hook_path}"
        print(f"[enforcement] DEGRADED {detail}; fail-closed", file=sys.stderr)
        return HookResult(None, detail, "missing")
    try:
        proc = subprocess.run(
            [sys.executable, str(hook_path)],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            timeout=timeout,
        )
        stderr = proc.stderr.decode("utf-8", errors="replace")
        if proc.returncode not in (0, 2):
            detail = stderr.strip() or f"hook exited {proc.returncode} without diagnostics"
            print(
                f"[enforcement] DEGRADED unexpected hook exit {proc.returncode}: "
                f"{hook_path}; fail-closed",
                file=sys.stderr,
            )
            return HookResult(proc.returncode, detail, "unexpected_exit")
        return HookResult(proc.returncode, stderr)
    except subprocess.TimeoutExpired:
        detail = f"hook timeout after {timeout}s: {hook_path}"
        print(f"[enforcement] DEGRADED {detail}; fail-closed", file=sys.stderr)
        return HookResult(None, detail, "timeout")
    except Exception as e:
        detail = f"hook error {type(e).__name__}: {e}: {hook_path}"
        print(f"[enforcement] DEGRADED {detail}; fail-closed", file=sys.stderr)
        return HookResult(None, detail, "exception")


def _infrastructure_failure(result: HookResult, hook_path: Path) -> dict:
    return {
        "kind": result.infrastructure_failure,
        "hook_path": str(hook_path),
        "exit_code": result.exit_code,
        "message": result.stderr.strip() or "governance hook unavailable",
    }


# ── The two gates ─────────────────────────────────────────────────────────────

def validate_write(args: dict) -> dict:
    """brain_validate_write / POST /validate → {decision, errors[]}."""
    file_path = args.get("file_path") or ""
    content = args.get("content") or ""
    tool_name = args.get("tool_name") or "Write"
    if tool_name == "Edit":
        tool_input = {"file_path": file_path, "new_string": content}
    elif tool_name == "MultiEdit":
        tool_input = {"file_path": file_path, "edits": [{"new_string": content}]}
    else:
        tool_input = {"file_path": file_path, "content": content}
    result = run_hook(BRAIN_WRITE_VALIDATOR_HOOK,
                      {"tool_name": tool_name, "tool_input": tool_input})
    if result.infrastructure_failure:
        failure = _infrastructure_failure(result, BRAIN_WRITE_VALIDATOR_HOOK)
        return {
            "decision": "block",
            "errors": ["Governance write validation is unavailable: " + failure["message"]],
            "infrastructure_failure": failure,
            "raw_stderr": result.stderr,
        }
    if result.exit_code == 2:
        errors = [line for line in result.stderr.splitlines() if line.strip().startswith("•")]
        return {
            "decision": "block",
            "errors": errors or [result.stderr.strip() or "validator returned block"],
            "raw_stderr": result.stderr,
        }
    return {"decision": "allow", "errors": []}


def check_risk(args: dict) -> dict:
    """brain_check_risk / POST /check_risk → {decision, rule_id, message, amber_warnings[]}."""
    command = args.get("command") or ""
    tool_name = args.get("tool_name") or "Bash"
    file_path = args.get("file_path") or ""
    if tool_name in ("Write", "Edit"):
        tool_input = {"file_path": file_path, "content": command, "new_string": command}
    else:
        tool_input = {"command": command}
    result = run_hook(BAN_RISK_HOOK, {"tool_name": tool_name, "tool_input": tool_input})
    if result.infrastructure_failure:
        failure = _infrastructure_failure(result, BAN_RISK_HOOK)
        return {
            "decision": "block",
            "rule_id": "governance-infrastructure-failure",
            "message": "Governance risk validation is unavailable: " + failure["message"],
            "amber_warnings": [],
            "infrastructure_failure": failure,
            "raw_stderr": result.stderr,
        }
    if result.exit_code == 2:
        rule_id = ""
        message = ""
        for line in result.stderr.splitlines():
            line = line.strip()
            if line.startswith("\U0001f6d1") or line.startswith("BAN-RISK GUARDRAIL blocked"):
                # Format: "🛑 BAN-RISK GUARDRAIL blocked: <rule_id>"
                rule_id = line.split(":", 1)[-1].strip() if ":" in line else line
            elif line.startswith("Reason:"):
                message = line[len("Reason:"):].strip()
        return {
            "decision": "block",
            "rule_id": rule_id,
            "message": message,
            "amber_warnings": [],
            "raw_stderr": result.stderr,
        }
    amber = [line.strip() for line in result.stderr.splitlines() if line.strip().startswith("⚠️")]
    return {"decision": "allow", "rule_id": "", "message": "", "amber_warnings": amber}


# Dispatch table so a transport never hardcodes which function a tool name means.
GATES = {
    "brain_validate_write": validate_write,
    "brain_check_risk": check_risk,
}


def run_gate(tool: str, raw_args: dict) -> dict:
    """Normalize → require → dispatch. The single entry point a transport should use.

    Any transport calling this gets identical alias handling, identical
    required-field errors, and identical verdicts.

    Deliberately does NOT decorate the verdict with `aliased_arguments` /
    `unrecognized_arguments`: the MCP transport attaches those notes only to the
    `brain_propose_*` staging tools (where a dropped argument is data loss), not
    to the gates. The gate result must be byte-identical across transports, so
    HTTP does not add a key MCP would not have added.
    """
    if tool not in GATES:
        return {"error": "unknown_gate", "tool": tool, "known": sorted(GATES)}
    args, aliased, unknown = normalize_args(tool, raw_args)
    missing = missing_required(tool, args)
    if missing:
        return missing_args_error(tool, args, missing, aliased, unknown)
    return GATES[tool](args)
