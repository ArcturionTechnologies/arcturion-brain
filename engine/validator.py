"""Brain schema validator (Item 7 of 17).

Implements the checks specified in
`06_MEMORY_SYSTEM/0611_LLM_WIKI/maintenance/VALIDATOR_SPEC.md`:

Node-level (markdown frontmatter):
  - Required frontmatter fields present
  - status ∈ {draft, active, deprecated, superseded, archived}
  - confidence ∈ {low, medium, high, verified}  (when present)
  - sensitivity ∈ {public, internal, confidential, secret-reference}  (when present)
  - status='active' invalid when superseded_by non-empty
  - secret-like strings rejected (regex)

Graph-level (JSONL triples / JSON edges):
  - Predicate (verb) in canon
  - Valid JSON per line
  - to/from refs are non-empty strings (existence is checked async by post-write)

Public API:
  Violation                 — dataclass
  validate_node(path)       — checks one markdown file's frontmatter + body
  validate_triple(d)        — checks one parsed edge/triple dict
  validate_jsonl(path)      — checks all lines in a JSONL file
  validate_text(text, path) — generic secrets-only check
  validate_path(path)       — dispatches on file extension
  validate_all()            — sweep canonical store for compliance
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Iterable, Iterator

# ── Canon ──────────────────────────────────────────────────────────────────

VALID_STATUS = {"draft", "active", "deprecated", "superseded", "archived"}
VALID_CONFIDENCE = {"low", "medium", "high", "verified"}
VALID_SENSITIVITY = {"public", "internal", "confidential", "secret-reference"}

# 42 forward predicates + their inverses (canonical full set from
# 06_MEMORY_SYSTEM/0611_LLM_WIKI/0006_RELATIONSHIP_TAXONOMY.md).
PREDICATE_CANON: frozenset[str] = frozenset({
    # Evidence
    "supports", "contradicts", "derived_from", "cites", "measured_by", "evidenced_by",
    "source_of", "cited_by",
    # Reasoning
    "depends_on", "implies", "assumes", "tests", "answers", "raises_question",
    "depended_on_by", "answered_by", "tested_by",
    # Lifecycle
    "supersedes", "superseded_by", "preceded_by", "followed_by",
    "revises", "archived_to", "promoted_from", "promoted_to",
    # Structure
    "part_of", "contains", "related_to", "tagged_with",
    "owned_by", "owns", "authored_by", "authored",
    "maintained_by", "maintains", "affects",
    # Operational
    "blocks", "unblocks", "routes_to", "requires_approval_from",
    "implemented_by", "validated_by",
    # Self-improvement
    "learned_from", "regressed_against", "checked_by_eval",
    "triggered_reflex", "requires_refresh",
    # Code
    "imports", "imported_by", "calls", "called_by",
    "executes", "executed_by", "configures", "configured_by",
    # Runtime
    "runs_on", "hosts", "installs", "installed_by", "exposes", "exposed_by",
    # Semantic
    "semantically_similar_to", "co_occurs_with",
    "instance_of", "class_of", "synonymous_with",
    # External
    "bookmarked_in", "contains_bookmark", "mentioned_in", "mentions",
    # Social
    "participates_in", "has_participant",
    # Temporal / session (Brain v2 Phase 4) — cross-agent session + change-event layer.
    # Endpoints are Session/Edit/Handoff event nodes + the files/concepts they touched.
    "touched_by", "touches",
    "modified_by", "modifies",
    "changed_in_session", "changed",
    "occurred_at", "time_of",
    "discussed_in", "discusses",
})

# Required frontmatter keys for "active"-promotable nodes.
REQUIRED_FM_KEYS = {"created", "owner"}
RECOMMENDED_FM_KEYS = {"last_modified", "status", "ttl_tier"}

# Item 16: formally retired predicates. New writes using these are rejected;
# existing edges retain them (lifecycle preserved) until a future compactor pass.
# Retire by appending to this set + adding rationale to 0006_RELATIONSHIP_TAXONOMY.md.
RETIRED_PREDICATES: frozenset[str] = frozenset({
    "archived_to",            # subsumed by `superseded_by` + status='archived'
    "regressed_against",      # niche; use `tests` + status='regressed' on the test node
    "triggered_reflex",       # internal observability — not graph-worthy
    "co_occurs_with",         # subsumed by `semantically_similar_to`
})

# Active canon = full canon minus retired
ACTIVE_PREDICATE_CANON: frozenset[str] = PREDICATE_CANON - RETIRED_PREDICATES

# Applies to the configured graph and source inventory.

KNOWN_FAMILIES: frozenset[str] = frozenset({
    "document", "code", "event", "application", "concept",
    "capability", "external", "automation", "agent",
})

_AGENT = frozenset({"agent"})
_EVENT = frozenset({"event"})
_APP = frozenset({"application"})
_CONTAINER = frozenset({"document", "event", "external"})

# Concepts remain eligible owners until a dedicated person family is available.
_OWNER = frozenset({"agent", "concept"})

DOMAIN_RANGE: dict[str, dict[str, frozenset[str] | None]] = {
    # Stewardship — 49,978 edges, 100% land on agent.
    "maintained_by":          {"domain": None,   "range": _AGENT},
    "maintains":              {"domain": _AGENT, "range": None},

    # Temporal / session layer — 100% of the session side is an event node.
    "touched_by":             {"domain": None,   "range": _EVENT},
    "touches":                {"domain": _EVENT, "range": None},
    "modified_by":            {"domain": None,   "range": _EVENT},
    "modifies":               {"domain": _EVENT, "range": None},
    "discussed_in":           {"domain": None,   "range": _EVENT},
    "discusses":              {"domain": _EVENT, "range": None},
    "changed_in_session":     {"domain": _EVENT, "range": _EVENT},
    "changed":                {"domain": _EVENT, "range": _EVENT},

    # Ownership / authorship — see TAXONOMY-GAP above for the 'concept' member.
    "owned_by":               {"domain": None,   "range": _OWNER},
    "owns":                   {"domain": _OWNER, "range": None},
    "authored_by":            {"domain": None,   "range": _OWNER},
    "authored":               {"domain": _OWNER, "range": None},

    # Runtime surface — application <-> exposed interface (170 / 85, tight).
    "exposes":                {"domain": _APP,  "range": None},
    "exposed_by":             {"domain": None,  "range": _APP},

    # Reference — the mention always lands in a container-ish node.
    "mentioned_in":           {"domain": None,       "range": _CONTAINER},
    "mentions":               {"domain": _CONTAINER, "range": None},

    # Governance boundaries. requires_approval_from has 409 measured edges,
    # all -> agent. routes_to has 0 edges today (Brain Gravity flags it as
    # high-value-unused) — encoded now so the first writer gets it right.
    "requires_approval_from": {"domain": None, "range": _AGENT},
    "routes_to":              {"domain": None, "range": _AGENT},
}

# Predicates whose two endpoints must share a family. Measured: document->
# document 8,666 / code->code 570 / concept->concept 472, plus exactly 2
# genuine errors (ArcturionIntelligence (capability) superseded_by GEMINI
# (agent), emitted by maintenance/entity_dedup + its derived inverse).
SAME_FAMILY_PREDICATES: frozenset[str] = frozenset({"supersedes", "superseded_by"})

# Secret-like patterns. Conservative (low false-positive) to avoid blocking
# legitimate content discussions. Each match should be a hard reject.
SECRET_PATTERNS = [
    (re.compile(r"\bsk-(?:proj|live|test|ant)-[A-Za-z0-9_\-]{16,}"), "OpenAI/Anthropic-style API key"),
    (re.compile(r"\b[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{20,}\b"), "JWT-like token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"), "GitHub fine-grained PAT"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}"), "GitHub classic PAT"),
    (re.compile(r"\bxox[bpoa]-[A-Za-z0-9-]{20,}"), "Slack token"),
    (re.compile(r"\bgcal-key-[A-Za-z0-9]{20,}"), "Google API key"),
    (re.compile(r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"), "Private key block"),
]

# Brain-write paths the hook should enforce on (in PreToolUse).
# Brain v2 (2026-06-16): the brain relocated to the lean sibling ~/Desktop/ARCTURION_Brain.
# Old HQ-relative prefixes kept (they still resolve via compat symlinks) so legacy
# write targets stay guarded during the transition.
WRITE_PROTECTED_PREFIXES = (
    "ARCTURION_Brain/",
    "06_MEMORY_SYSTEM/0611_LLM_WIKI/",
    "11_INFRASTRUCTURE/1107_LOCAL_LLM/brain/",
)


# ── Data types ─────────────────────────────────────────────────────────────

@dataclass
class Violation:
    code: str
    severity: str  # "error" blocks, "warn" surfaces
    message: str
    path: str | None = None
    line: int | None = None
    detail: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


# ── Frontmatter parsing ────────────────────────────────────────────────────

def _split_frontmatter(text: str) -> tuple[dict | None, str]:
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    raw = text[4:end]
    try:
        import yaml
    except ImportError:
        # A missing PyYAML silently skipped EVERY frontmatter schema check —
        # keep the fail-open contract but make it loud (review 2026-07-20).
        import sys as _sys
        print("[validator] WARN PyYAML unavailable — frontmatter checks skipped", file=_sys.stderr)
        return None, text[end + 5 :]
    try:
        data = yaml.safe_load(raw) or {}
        if not isinstance(data, dict):
            return None, text[end + 5 :]
        return data, text[end + 5 :]
    except Exception:
        return None, text[end + 5 :]


# ── Ontology-backed checks (canon/0007_ONTOLOGY.yaml) ──────────────────────
# The ontology owns the vocabulary (families, node types, predicates) and the
# inverse/transitive/functional/disjoint axioms. Endpoint typing is a separate
# layer — see DOMAIN_RANGE above.
#
# The import is DEFERRED and FAIL-SOFT, never module-level. `engine/validator.py`
# is loaded two ways: as a package module (the hook, mcp_server, edge_inference,
# maintenance) and by ABSOLUTE PATH with no package context via
# spec_from_file_location (ALTAIR's infra-regression suite,
# tests/test_brain_domain_range.py). A module-level `from . import ontology`
# would raise ImportError under the second and break that suite. Deferring it
# means the checks run under a normal import and silently no-op otherwise —
# degrading to exactly today's behavior rather than failing.
#
# All ontology violations are severity="warn". Nothing here blocks a write.

def _ontology():
    """Return the ontology module, or None when it cannot be loaded."""
    try:
        from . import ontology as _ont
        _ont.load()
        return _ont
    except Exception:
        # ImportError (no package context) / OntologyError (malformed or missing
        # canon file) / anything else — the check is skipped, never fatal.
        return None


def _check_ontology_node(fm: dict, path: str | None = None) -> list[Violation]:
    """ONT_UNKNOWN_NODE_TYPE + ONT_DISJOINT_VIOLATION for one node's frontmatter."""
    ont = _ontology()
    if ont is None:
        return []
    out: list[Violation] = []
    # ONLY `node_type`. canon/0005 is explicit that `node_type` is the field
    # declaring a node's ontology type. Frontmatter `type` is a DIFFERENT, older
    # namespace across this corpus — document-kind markers like 'session-log'
    # (1,645), 'reference' (416), 'identity' (190), 'boot-memory-packet' (84).
    # Checking `type` too produced 2,824 false positives against 2 real hits;
    # a check that noisy just teaches everyone to ignore the code.
    labels: list[str] = []
    node_type = fm.get("node_type")
    if isinstance(node_type, str) and node_type.strip():
        labels.append(node_type.strip())
    known = ont.node_types()
    for label in labels:
        if label not in known:
            out.append(Violation(
                code="ONT_UNKNOWN_NODE_TYPE",
                severity="warn",
                message=f"node type {label!r} is not in the ontology",
                path=path,
                detail=f"{len(known)} types declared in canon/0007_ONTOLOGY.yaml",
            ))
    # A node must not assert two mutually-exclusive labels. `family` participates
    # because disjoint sets range over type AND family names.
    fam = fm.get("family")
    if isinstance(fam, str) and fam.strip():
        labels.append(fam.strip())
    for i, a in enumerate(labels):
        for b in labels[i + 1:]:
            if ont.are_disjoint(a, b):
                out.append(Violation(
                    code="ONT_DISJOINT_VIOLATION",
                    severity="warn",
                    message=f"node asserts mutually-exclusive labels {a!r} and {b!r}",
                    path=path,
                ))
    return out


# ── Public API ─────────────────────────────────────────────────────────────

def scan_secrets(text: str, path: str | None = None) -> list[Violation]:
    out: list[Violation] = []
    for pat, label in SECRET_PATTERNS:
        m = pat.search(text)
        if m:
            out.append(Violation(
                code="SECRET_LEAK",
                severity="error",
                message=f"Secret-like string detected: {label}",
                path=path,
                detail=f"matched {len(m.group(0))} chars",
            ))
    return out


def validate_node(path: Path) -> list[Violation]:
    out: list[Violation] = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        return [Violation(code="READ_ERROR", severity="error",
                          message=f"cannot read: {e}", path=str(path))]
    out.extend(scan_secrets(text, str(path)))
    fm, body = _split_frontmatter(text)
    if fm is None:
        return out  # no frontmatter — no schema checks beyond secrets
    status = str(fm.get("status") or "").strip().lower() if fm.get("status") is not None else ""
    confidence = str(fm.get("confidence") or "").strip().lower() if fm.get("confidence") is not None else ""
    sensitivity = str(fm.get("sensitivity") or "").strip().lower() if fm.get("sensitivity") is not None else ""
    superseded_by = fm.get("superseded_by")

    if status and status not in VALID_STATUS:
        out.append(Violation(
            code="BAD_STATUS",
            severity="error",
            message=f"status={status!r} not in {sorted(VALID_STATUS)}",
            path=str(path),
        ))
    if confidence and confidence not in VALID_CONFIDENCE:
        # Numeric confidences (0.0-1.0) are also acceptable; only string mismatches error.
        try:
            float(confidence)
        except ValueError:
            out.append(Violation(
                code="BAD_CONFIDENCE",
                severity="error",
                message=f"confidence={confidence!r} not in {sorted(VALID_CONFIDENCE)} and not numeric",
                path=str(path),
            ))
    if sensitivity and sensitivity not in VALID_SENSITIVITY:
        out.append(Violation(
            code="BAD_SENSITIVITY",
            severity="error",
            message=f"sensitivity={sensitivity!r} not in {sorted(VALID_SENSITIVITY)}",
            path=str(path),
        ))
    if status == "active" and superseded_by and str(superseded_by).strip() not in {"", "—", "-", "None"}:
        out.append(Violation(
            code="STATUS_VS_SUPERSEDED",
            severity="error",
            message="status='active' is invalid when superseded_by is set",
            path=str(path),
        ))
    for k in REQUIRED_FM_KEYS:
        if k not in fm or fm[k] in (None, "", "—"):
            out.append(Violation(
                code="MISSING_REQUIRED_FM",
                severity="warn",
                message=f"required frontmatter field missing: {k}",
                path=str(path),
            ))
    out.extend(_check_ontology_node(fm, str(path)))
    return out


def validate_triple(
    d: dict,
    *,
    family_of: "Callable[[str], str | None] | None" = None,
) -> list[Violation]:
    """Validate one edge/triple dict.

    `family_of` is an OPTIONAL resolver mapping a node id -> its `nodes.family`
    (or None when the id is not a known node). When it is omitted the function
    stays pure and DB-free, which is how every existing caller uses it.

    Domain/range checking is tri-state: pass, violation, or unknown endpoint.
    An unresolved endpoint is not itself a domain/range violation.
    """
    out: list[Violation] = []
    # Only treat the line as a triple if it has both endpoints. JSONL files
    # also hold pure node records (id, family, title, …) which we skip here.
    has_endpoints = any(k in d for k in ("from", "to", "from_id", "to_id", "subject", "object"))
    if not has_endpoints:
        return out
    verb = d.get("verb") or d.get("predicate")
    if not verb:
        out.append(Violation(code="MISSING_PREDICATE", severity="error",
                             message="triple has no `verb`/`predicate` field"))
        return out
    if verb in RETIRED_PREDICATES:
        out.append(Violation(
            code="RETIRED_PREDICATE",
            severity="error",
            message=f"predicate {verb!r} is retired (see 0006_RELATIONSHIP_TAXONOMY.md ## Retired Predicates)",
        ))
    elif verb not in PREDICATE_CANON:
        out.append(Violation(
            code="UNKNOWN_PREDICATE",
            severity="error",
            message=f"predicate {verb!r} not in canon (see 0006_RELATIONSHIP_TAXONOMY.md)",
            detail=f"{len(ACTIVE_PREDICATE_CANON)} predicates in active canon",
        ))
    from_id = d.get("from") or d.get("from_id") or d.get("subject")
    to_id = d.get("to") or d.get("to_id") or d.get("object")
    if not isinstance(from_id, str) or not from_id.strip():
        out.append(Violation(code="MISSING_FROM", severity="error",
                             message="triple `from`/`from_id`/`subject` empty or non-string"))
    if not isinstance(to_id, str) or not to_id.strip():
        out.append(Violation(code="MISSING_TO", severity="error",
                             message="triple `to`/`to_id`/`object` empty or non-string"))

    # ── Domain / range ─────────────────────────────────────────────────────
    # No resolver, or the triple is already malformed, means nothing to type.
    if family_of is None or out:
        return out
    try:
        fam_from = family_of(from_id)
        fam_to = family_of(to_id)
    except Exception:
        # A broken resolver must never break validation.
        return out
    if fam_from is None or fam_to is None:
        return out  # unresolvable endpoint -> silent skip (tri-state)

    pair = f"{fam_from} -> {fam_to}"
    rule = DOMAIN_RANGE.get(verb)
    if rule:
        domain = rule.get("domain")
        rng = rule.get("range")
        if domain is not None and fam_from not in domain:
            out.append(Violation(
                code="DOMAIN_MISMATCH",
                severity="warn",
                message=(f"{verb!r} expects subject family in {sorted(domain)}, "
                         f"got {fam_from!r}"),
                detail=pair,
            ))
        if rng is not None and fam_to not in rng:
            out.append(Violation(
                code="RANGE_MISMATCH",
                severity="warn",
                message=(f"{verb!r} expects object family in {sorted(rng)}, "
                         f"got {fam_to!r}"),
                detail=pair,
            ))
    if verb in SAME_FAMILY_PREDICATES and fam_from != fam_to:
        out.append(Violation(
            code="SAME_FAMILY_MISMATCH",
            severity="warn",
            message=f"{verb!r} requires both endpoints in the same family",
            detail=pair,
        ))
    return out


def validate_jsonl(path: Path) -> list[Violation]:
    out: list[Violation] = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        return [Violation(code="READ_ERROR", severity="error",
                          message=f"cannot read: {e}", path=str(path))]
    out.extend(scan_secrets(text, str(path)))
    # Functional predicates are single-valued: one subject may assert at most one
    # object. This is the only place with cross-triple state, so the check lives
    # here and is scoped to the file. Fail-soft: no ontology -> no check.
    ont = _ontology()
    functional = ont.functional_predicates() if ont else frozenset()
    seen: dict[tuple[str, str], str] = {}
    for i, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError as e:
            out.append(Violation(code="BAD_JSON", severity="error",
                                 message=f"line {i}: {e}", path=str(path), line=i))
            continue
        if not isinstance(d, dict):
            continue
        for v in validate_triple(d):
            v.path = str(path)
            v.line = i
            out.append(v)
        verb = d.get("verb") or d.get("predicate")
        if verb in functional:
            subj = d.get("from") or d.get("from_id") or d.get("subject")
            obj = d.get("to") or d.get("to_id") or d.get("object")
            if isinstance(subj, str) and isinstance(obj, str) and subj and obj:
                prior = seen.setdefault((subj, verb), obj)
                if prior != obj:
                    out.append(Violation(
                        code="ONT_FUNCTIONAL_VIOLATION",
                        severity="warn",
                        message=(f"{verb!r} is functional but {subj!r} already asserts "
                                 f"{prior!r}; got {obj!r}"),
                        path=str(path),
                        line=i,
                    ))
    return out


def validate_text(text: str, path: str | None = None) -> list[Violation]:
    return scan_secrets(text, path)


def validate_desk_pack(scope: str, pack: dict):
    """Explicit desk-only validation; does not expand the global canon."""
    if scope != "VEGA":
        raise ValueError("unknown desk scope")
    from .vega_index import validate_pack
    return validate_pack(pack)


def validate_path(path: Path) -> list[Violation]:
    suffix = path.suffix.lower()
    if suffix == ".md":
        return validate_node(path)
    if suffix in (".jsonl",):
        return validate_jsonl(path)
    if path.exists() and path.is_file():
        try:
            return scan_secrets(path.read_text(encoding="utf-8"), str(path))
        except Exception:
            return []
    return []


def is_brain_protected(file_path: str) -> bool:
    p = str(file_path)
    return any(prefix in p for prefix in WRITE_PROTECTED_PREFIXES)


def validate_all() -> dict:
    """Sweep canonical wiki + brain code; return summary dict."""
    from . import config as C
    # Brain v2: WIKI_ROOT == BRAIN_ROOT (the lean sibling), which contains engine/,
    # canon/, index/ — so one root now covers the whole brain (no stale 1107 dup).
    roots = [C.WIKI_ROOT]
    total_files = 0
    violations: list[Violation] = []
    for root in roots:
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in (".md", ".jsonl"):
                continue
            if "/99_ARCHIVE/" in p.as_posix() or "/__pycache__/" in p.as_posix() or "/staging_snapshots/" in p.as_posix():
                continue
            total_files += 1
            violations.extend(validate_path(p))
    by_code: dict[str, int] = {}
    for v in violations:
        by_code[v.code] = by_code.get(v.code, 0) + 1
    return {
        "files_scanned": total_files,
        "violations": len(violations),
        "by_code": by_code,
        "errors_only": [v.to_dict() for v in violations if v.severity == "error"][:20],
    }


def _print_summary(summary: dict) -> None:
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="sweep canonical store")
    ap.add_argument("path", nargs="?", help="single file path to validate")
    args = ap.parse_args()
    if args.all:
        _print_summary(validate_all())
    elif args.path:
        viols = validate_path(Path(args.path))
        for v in viols:
            print(json.dumps(v.to_dict()))
        sys.exit(2 if any(v.severity == "error" for v in viols) else 0)
    else:
        ap.print_help()
