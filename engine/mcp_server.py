"""Stdio MCP server exposing the Infinite Brain to any MCP-capable LLM platform.

Speaks the Model Context Protocol over stdin/stdout (line-delimited JSON-RPC 2.0).
Compatible with Claude Code, Codex, Antigravity, Gemini CLI, Cursor, Cline,
and any other MCP host.

Tools exposed:
  - brain_query(topic, radius, budget, families, predicates, tier, since, before, order_by)
  - brain_status()
  - brain_search(text, limit)

Run:
    python3 -m engine.mcp_server
"""

from __future__ import annotations
import os
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from . import brain as B
from . import config as C
from . import db as DB
from . import enforcement as E
from . import validator as V
from .util import now_iso, short_hash

STAGING_DIR = C.STAGING_DIR  # canonical, registry-resolved (under BRAIN_ROOT); see config.py
# The two enforcement gates now live in `engine/enforcement.py` so the HTTP
# transport (POST /validate, POST /check_risk) executes the SAME code path.
# Re-exported here because callers/tests referenced these names. Rung 2 of the
# enforcement floor is only universal if there is exactly one implementation.
BAN_RISK_HOOK = E.BAN_RISK_HOOK
BRAIN_WRITE_VALIDATOR_HOOK = E.BRAIN_WRITE_VALIDATOR_HOOK
TEMPORAL_STATE = C.UNIVERSE_ROOT / "03_EXECUTIVE_CORE" / "0301_ACTIVE_CONTEXT" / "temporal_state.json"


# ── Argument normalization (added 2026-07-31, COVENANT) ──────────────────────
# Defect class this fixes:
#
#   1. SILENT CONTENT LOSS. Callers routinely pass a reasonable synonym for a
#      declared field — `content` for `body` (the name brain_validate_write
#      itself uses), `query` for `text`. The old code dropped the unknown key
#      without a word and read the declared one as empty, so a proposal could
#      be staged "successfully" with its entire body gone. Seven of the nine
#      proposals staged 2026-07-16 → 2026-07-29 lost their bodies exactly this
#      way, including Law L015 and a Law L011 companion — decisions reduced to
#      a one-line summary, reasoning recoverable only from transcripts.
#
#   2. UNHELPFUL REQUIRED-FIELD ERRORS. A bare `args["family"]` surfaced to the
#      caller as `Tool brain_propose_node failed: 'family'` — no statement of
#      what the tool wanted, so callers rediscovered the schema one KeyError at
#      a time.
#
# Fix: resolve known synonyms onto their canonical field, report any argument
# still unrecognized instead of discarding it in silence, and answer missing
# required fields with a readable contract. Aliases never overwrite a value the
# caller supplied under the canonical name.
ARG_ALIASES = {
    "brain_query": {"q": "topic", "query": "topic", "text": "topic", "subject": "topic"},
    "brain_search": {"q": "text", "query": "text", "topic": "text", "term": "text"},
    "brain_propose_node": {
        "content": "body", "markdown": "body", "full_text": "body", "node_body": "body",
        "type": "node_type", "kind": "node_type",
        "name": "title",
        "author": "asserted_by", "agent": "asserted_by", "proposed_by": "asserted_by",
        "tier": "governance_tier",
    },
    "brain_propose_edge": {
        "source": "from_id", "subject": "from_id", "from": "from_id",
        "target": "to_id", "object": "to_id", "to": "to_id",
        "verb": "predicate", "relation": "predicate", "relationship": "predicate",
        "author": "asserted_by", "agent": "asserted_by", "proposed_by": "asserted_by",
        "evidence": "evidence_span",
    },
    "brain_propose_node_promotion": {
        "id": "node_id", "node": "node_id",
        "tier": "target_tier", "to_tier": "target_tier",
        "reason": "rationale", "justification": "rationale",
        "author": "asserted_by", "agent": "asserted_by", "proposed_by": "asserted_by",
    },
    # The two gate tools share their alias table with the HTTP transport — a
    # reference, never a copy, so `path`→`file_path` cannot mean one thing over
    # MCP and another over curl.
    "brain_validate_write": E.ARG_ALIASES["brain_validate_write"],
    "brain_check_risk": E.ARG_ALIASES["brain_check_risk"],
}

REQUIRED_ARGS = {
    "brain_query": ["topic"],
    "brain_search": ["text"],
    "brain_capture_receipt": ["path"],
    "brain_propose_node": ["node_type", "family", "title", "summary", "asserted_by"],
    "brain_propose_edge": ["from_id", "to_id", "predicate", "asserted_by"],
    "brain_propose_node_promotion": ["node_id", "target_tier", "asserted_by"],
    "brain_validate_write": E.REQUIRED_ARGS["brain_validate_write"],
    "brain_check_risk": E.REQUIRED_ARGS["brain_check_risk"],
}


def _known_fields(tool: str) -> set:
    for t in TOOLS:
        if t["name"] == tool:
            return set(t["inputSchema"].get("properties", {}))
    return set()


def _normalize_args(tool: str, args: dict) -> tuple[dict, list, list]:
    """Resolve synonym arguments onto canonical fields.

    Returns `(args, aliased, unknown)` — `aliased` lists the "given → canonical"
    remappings applied, `unknown` lists arguments that matched nothing and would
    otherwise have been discarded in silence.
    """
    aliases = ARG_ALIASES.get(tool, {})
    known = _known_fields(tool)
    out, aliased, unknown = {}, [], []
    for key, value in args.items():
        if key in known:
            out[key] = value
            continue
        canonical = aliases.get(key)
        if canonical:
            # Never clobber a value the caller supplied under the real name.
            if args.get(canonical) not in (None, ""):
                unknown.append(f"{key} (ignored — {canonical} was also supplied)")
            else:
                out[canonical] = value
                aliased.append(f"{key} → {canonical}")
        else:
            unknown.append(key)
    return out, aliased, unknown


def _missing_required(tool: str, args: dict) -> list:
    return [f for f in REQUIRED_ARGS.get(tool, []) if args.get(f) in (None, "")]


def _with_arg_notes(result: dict, aliased: list, unknown: list) -> dict:
    """Attach argument-handling notes to a successful result.

    A staging call that quietly reinterpreted or discarded an argument must say
    so in its own response — the caller is usually an LLM that will never see
    the server log, and a proposal is exactly the place where losing an
    argument costs the most.
    """
    if aliased:
        result["aliased_arguments"] = aliased
    if unknown:
        result["unrecognized_arguments"] = unknown
        result["warning"] = (
            "These arguments matched no field on this tool and were NOT stored. "
            "Re-send under a valid field name if the content mattered."
        )
    return result


PROTOCOL_VERSION = "2024-11-05"

SERVER_INFO = {
    "name": "arcturion-infinite-brain",
    "version": "1.2.0",
}

# Shown by every MCP host as system context. Hosts without per-prompt hooks
# (notably Antigravity) rely on this + voluntary tool calls for parity with
# Claude/Codex/Gemini PreToolUse enforcement. See KNOWN_PLATFORM_EXCEPTIONS.md.
SERVER_INSTRUCTIONS = (
    "Arcturion Infinite Brain — the canonical knowledge graph for this user's universe.\n\n"
    "Mandatory protocol on every turn:\n"
    "  1. Before answering substantively, call `brain_query` with the user's topic to retrieve "
    "context. Skip only for pure greetings or sub-30-char prompts.\n"
    "  2. Before any file write or edit, call `brain_validate_write(file_path, content)` and honor "
    "its result. A non-empty `errors` array means DO NOT WRITE — surface the violations to the user.\n"
    "  3. Before any shell command or external API call (post/email/order/send), call "
    "`brain_check_risk(command)`. A `decision: \"block\"` means DO NOT EXECUTE — explain the rule.\n\n"
    "These tools are no-ops for safe inputs (~30ms) and are the only way to honor the universe's "
    "First Law (nothing gets worse) on hosts that lack PreToolUse hooks."
)

TOOLS = [
    {
        "name": "brain_query",
        "description": (
            "Query the Arcturion Infinite Brain knowledge graph. Returns a ranked, "
            "token-budgeted context pack of nodes + edges relevant to the topic, "
            "drawn from 20K+ universe + constellation files connected by 170K+ "
            "typed edges (depends_on, cites, semantically_similar_to, etc.). "
            "Use instead of reading raw files when you need cross-domain context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Natural-language topic or node id."},
                "desk": {"type": "string", "enum": ["VEGA"], "description": "Optional isolated VEGA desk; requires explicit desk enablement."},
                "desk_hops": {"type": "integer", "minimum": 1, "maximum": 3, "description": "Opt-in directed paths from an exact VEGA desk concept ID or name. Predicates filter the first hop only."},
                "format": {"type": "string", "enum": ["text", "json"], "default": "text"},
                "radius": {"type": "integer", "default": 2, "description": "BFS hops from seed nodes."},
                "budget": {"type": "integer", "default": 2000, "description": "Token budget for returned bundle. Default 2000 — this query is called every turn, so the returned pack compounds in-context across a session; pass a larger budget explicitly for deep one-off lookups."},
                "families": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Filter to node families: document, code, automation, agent, event, external, concept, data."
                },
                "predicates": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Filter edges to these predicates only."
                },
                "tier": {"type": "integer", "default": 0, "description": "Minimum governance_tier filter."},
                "since": {"type": "string", "description": "Temporal lower bound (last_modified). ISO date '2026-06-01', ISO datetime, or relative span '7d'/'2w'/'3mo'/'1y'/'24h'. Use with an empty topic to list recent activity in a window."},
                "before": {"type": "string", "description": "Temporal upper bound (last_modified). Same formats as `since`; a bare date includes the whole day."},
                "order_by": {"type": "string", "enum": ["relevance", "recency"], "default": "relevance", "description": "'recency' sorts results newest-first by last_modified; 'relevance' (default) by match score."},
                "owner": {"type": "string", "description": "Agent name (e.g. 'COVENANT') to scope recall to. Boosts that agent's own nodes to the top of the ranking; cross-agent context stays reachable unless owner_strict is set."},
                "owner_strict": {"type": "boolean", "default": False, "description": "Hard-filter seed nodes to this owner only (requires owner). Use when you want just this agent's own material, not cross-agent context — owner coverage is partial, so this can return an empty pack for sparsely-owned agents."},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "brain_capture_receipt",
        "description": "Exact indexed path/version receipt; does not imply action authority.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "brain_status",
        "description": "Return brain health metrics: node count, edge count, embeddings, orphans, top predicates, family breakdown.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "brain_search",
        "description": "Free-text search across node titles, summaries, and bodies. Returns top-N matching nodes (no edge expansion).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "include_telemetry": {"type": "boolean", "default": False, "description": "Include preserved machine evidence in this capacity."},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["text"],
        },
    },
    {
        "name": "brain_propose_node",
        "description": (
            "Stage a new node for COVENANT review. Writes to staging — does NOT modify "
            "the canonical store. Approved proposals get promoted in a separate step. "
            "Use this when an agent wants to suggest adding a fact, concept, or entity. "
            "Put the full reasoning in `body` — `summary` alone is not the proposal. "
            "Required: node_type, family, title, summary, asserted_by."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_type": {"type": "string", "description": "Node type per 0005_NODE_TAXONOMY.md."},
                "family": {"type": "string", "description": "Node family (document, code, concept, ...)."},
                "title": {"type": "string"},
                "summary": {"type": "string", "description": "One-paragraph abstract. REQUIRED."},
                "body": {
                    "type": "string",
                    "description": (
                        "Full markdown body — the reasoning, evidence, and relationships. "
                        "This is the field that carries the actual decision; a proposal staged "
                        "without it survives only as its summary. Aliases accepted: `content`, "
                        "`markdown`, `full_text`."
                    ),
                },
                "asserted_by": {"type": "string", "description": "Agent or harvester proposing. REQUIRED."},
                "governance_tier": {"type": "integer", "default": 0},
            },
            "required": ["node_type", "family", "title", "summary", "asserted_by"],
        },
    },
    {
        "name": "brain_propose_edge",
        "description": (
            "Stage a new edge for COVENANT review. Validates predicate against the "
            "42-predicate canon. Writes to staging — never directly into brain.sqlite."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_id": {"type": "string"},
                "to_id": {"type": "string"},
                "predicate": {"type": "string"},
                "confidence": {"type": "number", "default": 0.7},
                "evidence_span": {"type": "string"},
                "asserted_by": {"type": "string"},
            },
            "required": ["from_id", "to_id", "predicate", "asserted_by"],
        },
    },
    {
        "name": "brain_propose_node_promotion",
        "description": (
            "Submit an existing tier-0 node for COVENANT promotion to higher tier. "
            "Appends a queue entry to `02_GOVERNANCE/0201_COVENANT/inbox.md`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "target_tier": {"type": "integer", "default": 1},
                "rationale": {"type": "string"},
                "asserted_by": {"type": "string"},
            },
            "required": ["node_id", "target_tier", "asserted_by"],
        },
    },
    {
        "name": "brain_validate_write",
        "description": (
            "Validate a file write or edit against the universe's brain-write-validator policy "
            "BEFORE actually writing. Wraps the canonical PreToolUse hook so MCP-only hosts "
            "(Antigravity, Cursor, Cline) get equivalent enforcement. Always call immediately "
            "before any write to paths under `06_MEMORY_SYSTEM/0611_LLM_WIKI/` or "
            "`11_INFRASTRUCTURE/1107_LOCAL_LLM/brain/`. Returns `{decision, errors[]}`. "
            "`decision: 'allow'` means safe. `decision: 'block'` means DO NOT WRITE — surface "
            "the errors to the user."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path the write would target."},
                "content": {"type": "string", "description": "Full file content (Write) or new_string (Edit)."},
                "tool_name": {"type": "string", "default": "Write", "description": "One of: Write, Edit, MultiEdit."},
            },
            "required": ["file_path", "content"],
        },
    },
    {
        "name": "brain_check_risk",
        "description": (
            "Check a proposed shell command or external action against the universe's ban-risk "
            "policy. Wraps the canonical PreToolUse hook so MCP-only hosts get equivalent "
            "enforcement. Always call immediately before any shell command or external send. "
            "Returns `{decision, rule_id?, message?, amber_warnings[]}`. `decision: 'block'` means "
            "DO NOT EXECUTE — explain to the user which rule fired and ask for authorization."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command, code snippet, or message text to scan."},
                "tool_name": {"type": "string", "default": "Bash", "description": "Source tool name."},
                "file_path": {"type": "string", "description": "If applicable, the target path."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "temporal_pulse",
        "description": (
            "Query the COVENANT temporal intelligence Pulse — what is stale, overdue, "
            "expiring, or slipping across the universe, ranked by urgency. Watches 8 "
            "categories: ttl, automations, tasks, calendar, credentials, agents, "
            "business, projects. Call at the start of work to learn what needs action. "
            "Same data as MASTER_CLOCK's 'Needs Attention' section and the covenant-time CLI."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "view": {
                    "type": "string", "default": "now",
                    "description": "now (top priorities) | urgent | stale | all | agent | category",
                },
                "query": {
                    "type": "string",
                    "description": "agent name when view=agent; category name when view=category",
                },
            },
        },
    },
]


# Drift guard: the gate tools' MCP schemas and the shared floor's field table must
# agree, or an argument accepted over MCP would be silently discarded over HTTP.
# Warns on stderr; never raises — a schema mismatch must not take the server down.
E.assert_schema_parity({
    name: set(t["inputSchema"].get("properties", {}))
    for name in E.KNOWN_FIELDS
    for t in TOOLS if t["name"] == name
})


for _tool in TOOLS:
    if _tool["name"] in ("brain_query","brain_search","brain_capture_receipt"):
        _tool["inputSchema"]["properties"].update({
            "capacity": {"type":"string","enum":["business","personal"],"default":"business",
                         "description":"Explicit world; requires trusted admitted assignment scope."},
            "project_id": {"type":"string","description":"Narrow to one trusted admitted assignment ID."},
        })

def _rel(path: Path) -> str:
    """Display a staged path relative to the nearest known root (brain, then universe),
    falling back to the absolute path. Staging lives under BRAIN_ROOT (a Desktop sibling,
    NOT under UNIVERSE_ROOT), so a bare relative_to(UNIVERSE_ROOT) would raise ValueError."""
    for base in (C.BRAIN_ROOT, C.UNIVERSE_ROOT):
        try:
            return str(path.relative_to(base))
        except ValueError:
            continue
    return str(path)


def _write_proposal(proposals: list[dict], source: str) -> Path:
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # The filename hash MUST depend on proposal identity, not just (source, ts, count):
    # `ts` is second-resolution, so two distinct same-source proposals in the same second
    # would otherwise mint an identical name and the second write would silently clobber the
    # first (COVENANT-queue data loss under burst proposals). Fold in each proposal's unique
    # proposal_id (falling back to full content) to guarantee a distinct name per payload.
    identity = "|".join(
        p.get("proposal_id") or json.dumps(p, sort_keys=True, ensure_ascii=False)
        for p in proposals
    )
    h = short_hash(f"{source}:{ts}:{len(proposals)}:{identity}")[:8]
    path = STAGING_DIR / f"{ts}_{source}_{h}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for p in proposals:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    return path


def _append_covenant_queue(title: str, body_lines: list[str]) -> None:
    """Append a `## Proposal Queue` entry to the COVENANT inbox."""
    inbox = C.COVENANT_INBOX
    inbox.parent.mkdir(parents=True, exist_ok=True)
    if not inbox.exists() or inbox.stat().st_size == 0:
        inbox.write_text(
            "---\ncreated: " + now_iso() + "\nowner: COVENANT\nttl_tier: 1\nstatus: active\n---\n\n"
            "# COVENANT Inbox\n\n## Active\n\n",
            encoding="utf-8",
        )
    block = [f"### 📥 Proposal — {title}  ({now_iso()})", *body_lines, ""]
    with inbox.open("a", encoding="utf-8") as f:
        f.write("\n".join(block) + "\n")


def _validate_propose_edge(args: dict) -> list[str]:
    triple = {
        "from": args.get("from_id"),
        "to": args.get("to_id"),
        "verb": args.get("predicate"),
    }
    violations = V.validate_triple(triple)
    return [f"[{v.code}] {v.message}" for v in violations if v.severity == "error"]


def _handle_propose_node(args: dict) -> dict:
    proposal_id = f"prop_{short_hash(args['title'] + (args.get('asserted_by') or '') + now_iso())[:8]}"
    proposal = {
        "proposal_id": proposal_id,
        "kind": "node",
        "node_type": args["node_type"],
        "family": args["family"],
        "title": args["title"],
        "summary": args["summary"],
        "body": args.get("body") or "",
        "asserted_by": args["asserted_by"],
        "asserted_at": now_iso(),
        "governance_tier": int(args.get("governance_tier", 0)),
    }
    # Body secret scan
    body_violations = V.scan_secrets((args.get("body") or "") + "\n" + (args.get("summary") or ""))
    if body_violations:
        return {"error": "secret_detected", "details": [v.message for v in body_violations]}
    path = _write_proposal([proposal], source=args["asserted_by"])
    _append_covenant_queue(
        title=f"Node — {args['title']}",
        body_lines=[
            f"- proposal_id: `{proposal_id}`",
            f"- kind: node ({args['node_type']} · {args['family']})",
            f"- summary: {args['summary'][:160]}",
            f"- asserted_by: {args['asserted_by']}",
            f"- staged_at: `{_rel(path)}`",
        ],
    )
    return {"proposal_id": proposal_id, "staged_path": _rel(path)}


def _handle_propose_edge(args: dict) -> dict:
    errs = _validate_propose_edge(args)
    if errs:
        return {"error": "validation_failed", "details": errs}
    proposal_id = f"prop_{short_hash(args['from_id'] + args['to_id'] + args['predicate'])[:8]}"
    proposal = {
        "proposal_id": proposal_id,
        "kind": "edge",
        "from": args["from_id"],
        "to": args["to_id"],
        "predicate": args["predicate"],
        "confidence": float(args.get("confidence", 0.7)),
        "evidence_span": args.get("evidence_span") or "",
        "asserted_by": args["asserted_by"],
        "asserted_at": now_iso(),
    }
    # Same secret scan node bodies get — evidence_span was an unscanned channel
    # into the staging queue (review 2026-07-20).
    span_violations = V.scan_secrets(args.get("evidence_span") or "")
    if span_violations:
        return {"error": "secret_detected", "details": [v.message for v in span_violations]}
    path = _write_proposal([proposal], source=args["asserted_by"])
    _append_covenant_queue(
        title=f"Edge — {args['predicate']}",
        body_lines=[
            f"- proposal_id: `{proposal_id}`",
            f"- kind: edge",
            f"- {args['from_id']} —[{args['predicate']}]→ {args['to_id']}",
            f"- confidence: {args.get('confidence', 0.7)}",
            f"- asserted_by: {args['asserted_by']}",
            f"- staged_at: `{_rel(path)}`",
        ],
    )
    return {"proposal_id": proposal_id, "staged_path": _rel(path)}


def _handle_propose_promotion(args: dict) -> dict:
    ticket_id = f"prom_{short_hash(args['node_id'] + now_iso())[:8]}"
    proposal = {
        "proposal_id": ticket_id,
        "kind": "promotion",
        "node_id": args["node_id"],
        "target_tier": int(args.get("target_tier", 1)),
        "rationale": args.get("rationale") or "",
        "asserted_by": args["asserted_by"],
        "asserted_at": now_iso(),
    }
    path = _write_proposal([proposal], source=args["asserted_by"])
    _append_covenant_queue(
        title=f"Promotion — {args['node_id']} → tier {args.get('target_tier', 1)}",
        body_lines=[
            f"- proposal_id: `{ticket_id}`",
            f"- kind: promotion",
            f"- rationale: {args.get('rationale') or '(none)'}",
            f"- asserted_by: {args['asserted_by']}",
            f"- staged_at: `{_rel(path)}`",
        ],
    )
    return {"ticket_id": ticket_id, "staged_path": _rel(path)}


# Hook invocation and the two gate verdicts moved to `engine/enforcement.py`
# (2026-08-04, Leg B of the harness-parity build) so `http_server.py` can expose
# POST /validate and POST /check_risk over the SAME implementation. These names
# are thin aliases — there is no second copy of the policy to drift.
_run_hook = E.run_hook
_handle_validate_write = E.validate_write
_handle_check_risk = E.check_risk


def _handle_temporal_pulse(args: dict) -> dict:
    """Return a filtered view of the COVENANT temporal intelligence Pulse."""
    view = (args.get("view") or "now").lower()
    query = (args.get("query") or "").upper()
    if not TEMPORAL_STATE.exists():
        return {"error": "temporal_state.json not found — the Pulse has not run yet"}
    try:
        state = json.loads(TEMPORAL_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"error": f"could not read temporal_state.json: {e}"}
    items = state.get("items", [])
    if view == "urgent":
        sel = [i for i in items if i.get("band", "") in ("CRITICAL", "URGENT")]
    elif view == "stale":
        sel = [i for i in items if i.get("band", "") != "NOMINAL"]
    elif view == "all":
        sel = items
    elif view == "agent":
        sel = [i for i in items if query in str(i.get("owner", "")).upper()]
    elif view == "category":
        sel = [i for i in items if str(i.get("category", "")).upper() == query]
    else:  # now
        view = "now"
        sel = [i for i in items if i.get("band", "") in ("CRITICAL", "URGENT")][:12]
    return {
        "generated": state.get("generated"),
        "today": state.get("today"),
        "totals": state.get("totals"),
        "view": view,
        "items": sel,
    }


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(rid, value) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": value}


def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _text_content(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


# Hard ceiling on every brain MCP tool response body. brain_status alone once shipped a
# ~273 KB payload in one call (598 dangling-predicate entries + 573 fully-dangling-predicate
# names, see brain.status_summary()'s note) — enough to poison a meaningful slice of an
# agent's context window from a single tool call. brain.status_summary() bounds that specific
# case at the source; this is the safety net for every OTHER tool (present or future) that
# might someday return something unexpectedly large. No brain MCP response should ever leave
# this process larger than RESPONSE_CAP_BYTES, full stop.
RESPONSE_CAP_BYTES = 32 * 1024


def _bounded_content(text: str) -> dict:
    """Wrap `text` as MCP tool content, truncating with a visible marker if oversized.

    Applies uniformly to every tool's output — JSON tool results and the plain-text
    brain_query bundle alike — so no single call can hand an agent an unbounded blob.
    """
    encoded = text.encode("utf-8", errors="surrogatepass")
    if len(encoded) <= RESPONSE_CAP_BYTES:
        return _text_content(text)

    def _wrap(preview: str) -> str:
        return json.dumps({
            "_truncated": True,
            "_original_bytes": len(encoded),
            "_cap_bytes": RESPONSE_CAP_BYTES,
            "_note": (
                "Response exceeded the MCP response-size ceiling and was truncated to "
                "protect the caller's context window."
            ),
            "_preview": preview,
        }, indent=2)

    # Measure the wrapper's own overhead (keys, indent, JSON-escaping of the preview
    # string) empirically rather than guessing a fixed budget — an escaped preview
    # (quotes, backslashes, newlines) can expand well past a flat byte-for-byte slice,
    # and a fixed guess either overshoots the cap or wastes most of the budget on margin.
    overhead = len(_wrap("").encode("utf-8"))
    keep = max(0, RESPONSE_CAP_BYTES - overhead - 64)  # 64B slack for escaping growth
    preview = encoded[:keep].decode("utf-8", errors="ignore")
    out = _wrap(preview)
    # json.dumps can still expand a preview full of quotes/backslashes past the slack
    # margin; if so, shrink once more by the exact overshoot rather than re-guessing.
    overshoot = len(out.encode("utf-8")) - RESPONSE_CAP_BYTES
    if overshoot > 0:
        preview = preview[: max(0, len(preview) - overshoot)]
        out = _wrap(preview)
    return _text_content(out)


def _bounded_json(value) -> dict:
    text = json.dumps(value)
    size = len(text.encode('utf-8'))
    if size > RESPONSE_CAP_BYTES:
        return _text_content(json.dumps({'status':'error','error':'response_overflow',
            'overflow':True,'original_bytes':size,'cap_bytes':RESPONSE_CAP_BYTES,
            'message':'Reduce the requested scope or budget; no partial structured result returned.'}))
    return _text_content(text)


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return _result(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}, "logging": {}},
            "serverInfo": SERVER_INFO,
            "instructions": SERVER_INSTRUCTIONS,
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _result(rid, {})
    if method == "tools/list":
        return _result(rid, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        # Resolve synonym arguments and refuse incomplete calls with a readable
        # contract rather than a bare KeyError (see ARG_ALIASES rationale above).
        _aliased, _unknown = [], []
        if name in REQUIRED_ARGS:
            args, _aliased, _unknown = _normalize_args(name, args)
            missing = _missing_required(name, args)
            if missing:
                return _result(rid, _text_content(json.dumps({
                    "error": "missing_required_arguments",
                    "tool": name,
                    "missing": missing,
                    "required": REQUIRED_ARGS[name],
                    "accepted": sorted(_known_fields(name)),
                    "received": sorted(args),
                    "aliased": _aliased,
                    "unrecognized": _unknown,
                }, indent=2)))
        try:
            if name == "brain_query":
                pack = B.query(
                    topic=args["topic"],
                    capacity=args.get("capacity", "business"),
                    project_id=args.get("project_id"),
                    desk=args.get("desk"),
                    desk_hops=args.get("desk_hops"),
                    radius=max(1, min(int(args.get("radius", 2)), 5)),
                    token_budget=max(100, min(int(args.get("budget", 2000)), 8000)),
                    families=args.get("families"),
                    predicates=args.get("predicates"),
                    governance_tier_min=int(args.get("tier", 0)),
                    since=args.get("since"),
                    before=args.get("before"),
                    order_by=args.get("order_by", "relevance"),
                    owner=args.get("owner"),
                    owner_strict=bool(args.get("owner_strict", False)),
                )
                return _result(rid, _bounded_json(pack.to_dict()) if args.get("format") == "json" else _bounded_content(pack.text_bundle or "[empty pack]"))
            if name == "brain_capture_receipt":
                return _result(rid, _bounded_json(B.capture_receipt(args["path"], capacity=args.get("capacity","business"), project_id=args.get("project_id"))))
            if name == "brain_status":
                # Use a bounded summary for agent context; full details remain available
                # through the status CLI and HTTP endpoint.
                return _result(rid, _bounded_content(json.dumps(B.status_summary(), indent=2)))
            if name == "brain_propose_node":
                out = _with_arg_notes(_handle_propose_node(args), _aliased, _unknown)
                return _result(rid, _bounded_json(out))
            if name == "brain_propose_edge":
                out = _with_arg_notes(_handle_propose_edge(args), _aliased, _unknown)
                return _result(rid, _bounded_content(json.dumps(out, indent=2)))
            if name == "brain_propose_node_promotion":
                out = _with_arg_notes(_handle_propose_promotion(args), _aliased, _unknown)
                return _result(rid, _bounded_content(json.dumps(out, indent=2)))
            if name == "brain_validate_write":
                return _result(rid, _bounded_content(json.dumps(_handle_validate_write(args), indent=2)))
            if name == "brain_check_risk":
                return _result(rid, _bounded_content(json.dumps(_handle_check_risk(args), indent=2)))
            if name == "temporal_pulse":
                return _result(rid, _bounded_content(json.dumps(_handle_temporal_pulse(args), indent=2)))
            if name == "brain_search":
                conn = B.scoped_connection(args.get("capacity","business"), args.get("project_id"), include_telemetry=args.get("include_telemetry", False))
                try:
                    # Escape LIKE metacharacters + clamp limit (review 2026-07-20):
                    # a bare '%' matched every row and an unbounded limit could
                    # dump the whole node table into the caller's context.
                    esc = str(args["text"]).lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    q = f"%{esc}%"
                    lim = max(1, min(int(args.get("limit", 10)), 200))
                    rows = conn.execute(
                        "SELECT id, title, type, family, path, summary, body_excerpt, content_hash FROM nodes "
                        "WHERE lower(title) LIKE ? ESCAPE '\\' OR lower(summary) LIKE ? ESCAPE '\\' "
                        "ORDER BY centrality DESC LIMIT ?",
                        (q, q, lim),
                    ).fetchall()
                    from .source_integrity import filter_retrieval
                    out = filter_retrieval(conn, [dict(r) for r in rows])
                    for r in out:
                        r.pop("body_excerpt", None)
                        r.pop("content_hash", None)
                finally:
                    conn.close()
                result = _bounded_content(json.dumps(out, indent=2))
                result["_meta"]={"coverage":B.coverage_warning(args.get("capacity","business"),args.get("project_id"))}
                return _result(rid, result)
            return _error(rid, -32601, f"Unknown tool: {name}")
        except Exception as e:
            return _error(rid, -32000, f"Tool {name} failed: {e}")
    if method == "resources/list":
        return _result(rid, {"resources": []})
    if method == "prompts/list":
        return _result(rid, {"prompts": []})
    if method == "shutdown":
        return _result(rid, {})

    if rid is not None:
        return _error(rid, -32601, f"Method not found: {method}")
    return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            response = handle(msg)
            if response is not None:
                _send(response)
        except Exception as e:
            if msg.get("id") is not None:
                _send(_error(msg["id"], -32000, str(e)))


if __name__ == "__main__":
    main()
