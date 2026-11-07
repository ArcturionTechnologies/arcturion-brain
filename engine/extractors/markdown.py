"""Markdown extractor — frontmatter + wikilinks + body excerpt."""

from __future__ import annotations
from pathlib import Path
from datetime import datetime
from .. import config as C
from ..util import (
    parse_frontmatter, extract_wikilinks, make_node_id,
    content_hash, first_paragraph, truncate, now_iso, valid_date,
)


# Predicates that can appear directly in node frontmatter as list-valued keys.
# Template placeholders are not identities. Suppress only the scalar `owner`
# fallback below; explicit `owned_by` declarations retain their typed-edge semantics.
_OWNER_PLACEHOLDERS = {
    "agent_name", "<agent name>", "<agent>", "<agent_name>", "<owner>",
}


_FM_EDGE_KEYS = {
    "depends_on", "depended_on_by", "supersedes", "superseded_by",
    "preceded_by", "followed_by", "supports", "contradicts", "derived_from",
    "cites", "cited_by", "measured_by", "evidenced_by", "implies", "assumes",
    "tests", "tested_by", "answers", "answered_by", "raises_question",
    "revises", "archived_to", "part_of", "contains", "related_to",
    "tagged_with", "owned_by", "authored_by", "maintained_by",
    "affects", "blocks", "unblocks", "routes_to", "requires_approval_from",
    "implemented_by", "validated_by", "learned_from", "promoted_from",
    "promoted_to", "regressed_against", "checked_by_eval", "triggered_reflex",
    "requires_refresh", "imports", "imported_by", "calls", "called_by",
    "executes", "executed_by", "configures", "configured_by", "runs_on",
    "hosts", "installs", "installed_by", "exposes", "exposed_by",
    "bookmarked_in", "mentioned_in", "participates_in", "instance_of",
    "class_of", "synonymous_with",
}


_PREDICATE_FAMILY = {
    "supports": "evidence", "contradicts": "evidence", "derived_from": "evidence",
    "cites": "evidence", "cited_by": "evidence", "measured_by": "evidence",
    "evidenced_by": "evidence",
    "depends_on": "reasoning", "depended_on_by": "reasoning",
    "implies": "reasoning", "assumes": "reasoning", "tests": "reasoning",
    "tested_by": "reasoning", "answers": "reasoning", "answered_by": "reasoning",
    "raises_question": "reasoning",
    "supersedes": "lifecycle", "superseded_by": "lifecycle",
    "preceded_by": "lifecycle", "followed_by": "lifecycle", "revises": "lifecycle",
    "archived_to": "lifecycle", "promoted_from": "lifecycle", "promoted_to": "lifecycle",
    "part_of": "structure", "contains": "structure", "related_to": "structure",
    "tagged_with": "structure", "owned_by": "structure", "authored_by": "structure",
    "maintained_by": "structure", "affects": "structure",
    "blocks": "operational", "unblocks": "operational", "routes_to": "operational",
    "requires_approval_from": "operational", "implemented_by": "operational",
    "validated_by": "operational",
    "learned_from": "self_improvement", "regressed_against": "self_improvement",
    "checked_by_eval": "self_improvement", "triggered_reflex": "self_improvement",
    "requires_refresh": "self_improvement",
    "imports": "code", "imported_by": "code", "calls": "code", "called_by": "code",
    "executes": "code", "executed_by": "code", "configures": "code",
    "configured_by": "code",
    "runs_on": "runtime", "hosts": "runtime", "installs": "runtime",
    "installed_by": "runtime", "exposes": "runtime", "exposed_by": "runtime",
    "semantically_similar_to": "semantic", "co_occurs_with": "semantic",
    "instance_of": "semantic", "class_of": "semantic", "synonymous_with": "semantic",
    "bookmarked_in": "external", "contains_bookmark": "external",
    "mentioned_in": "external", "mentions": "external",
    "participates_in": "social", "has_participant": "social",
}


def predicate_family(pred: str) -> str:
    return _PREDICATE_FAMILY.get(pred, "structure")


def endpoint_reference_issue(value: str) -> str | None:
    """Reject prose-shaped frontmatter relations while preserving short labels/IDs.

    Relationship fields are references, not scheduling notes.  The known bad rows use
    sentences such as ``Cards 1 ✅ ... Independent of Card 0`` as an endpoint.  They can
    never resolve to a node and make inverse derivation double the rubble.
    """
    value = value.strip()
    if not value:
        return "empty_endpoint"
    if "\n" in value or len(value) > 160:
        return "prose_frontmatter_reference"
    prose_markers = (". ", " — ", " · ", "✅", "❌")
    if len(value.split()) >= 5 and any(marker in value for marker in prose_markers):
        return "prose_frontmatter_reference"
    if value.lower().startswith(("none.", "independent of ")):
        return "prose_frontmatter_reference"
    return None


def _rel_to_universe(p: Path) -> str:
    try:
        return str(p.relative_to(C.UNIVERSE_ROOT))
    except ValueError:
        pass
    try:
        return str(p.relative_to(C.DESKTOP_ROOT))
    except ValueError:
        pass
    try:
        # ~/.claude/... (engineering plans + hook scripts) → '.claude/...' so the
        # stored path stays relative + readable instead of an absolute home path.
        return str(p.relative_to(C.WORK_ROOT))
    except ValueError:
        return str(p)


def ownership_metadata(fm: dict) -> tuple[object, dict]:
    """Explicit ownership wins. Agent is a compatibility fallback, never an ACL."""
    key = next((k for k in ("owner", "authored_by", "agent") if fm.get(k)), None)
    owner = fm.get(key) if key else None
    diagnostic = {"ownership_source": key}
    agent = fm.get("agent")
    if agent and owner and str(agent).strip().casefold() != str(owner).strip().casefold():
        diagnostic["ownership_conflict"] = {"selected_field": key, "selected": owner,
                                           "agent": agent}
    if owner is not None and not isinstance(owner, str):
        diagnostic["ownership_invalid"] = key
        owner = None
    return owner, diagnostic


def bare_node(path: Path, family: str, node_type: str) -> dict:
    try:
        data = path.read_bytes()
    except Exception:
        data = b""
    stat = path.stat()
    rel = _rel_to_universe(path)
    title = path.stem.replace("_", " ").replace("-", " ").strip()
    return {
        "id": make_node_id(path, title),
        "type": node_type,
        "family": family,
        "title": title,
        "path": rel,
        "owner": None,
        "status": "active",
        "governance_tier": 0,
        "content_hash": content_hash(data),
        "summary": None,
        "body_excerpt": None,
        "centrality": 0.0,
        "created": None,
        "last_modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "last_reviewed": None,
        "metadata": {"size_bytes": stat.st_size},
    }


def extract_markdown(path: Path, default_type: str = "Source") -> tuple[dict, list[dict]]:
    try:
        raw = path.read_bytes()
    except Exception:
        return bare_node(path, "document", default_type), []
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        text = ""
    fm, body = parse_frontmatter(text)
    rel = _rel_to_universe(path)
    title = fm.get("title") or fm.get("name") or path.stem.replace("_", " ").replace("-", " ").strip()
    node_type = fm.get("node_type") or fm.get("entity_type") or default_type
    if isinstance(node_type, str):
        node_type = node_type.strip()
        # Capitalize first letter to match taxonomy
        if node_type and node_type[0].islower():
            node_type = node_type[:1].upper() + node_type[1:]
    # Fleet memory files carry their retrieval hook in `description:`; honor it before
    # falling back to a first-paragraph scrape (ranking weights summary 2.0/term).
    summary = fm.get("summary") or fm.get("description")
    if summary and not isinstance(summary, str):
        summary = str(summary)
    if not summary:
        summary = first_paragraph(body, 300)

    stat = path.stat()
    node_id = fm.get("id") or make_node_id(path, title)

    owner, ownership = ownership_metadata(fm)
    node = {
        "id": node_id,
        "type": node_type,
        "family": "document",
        "title": title if isinstance(title, str) else str(title),
        "path": rel,
        "owner": owner,
        "status": fm.get("status") or "active",
        "governance_tier": int(fm.get("governance_tier") or 0),
        "content_hash": content_hash(raw),
        "summary": summary,
        "body_excerpt": truncate(body, C.MAX_BODY_CHARS),
        "centrality": 0.0,
        # Validate frontmatter dates — reject template placeholders like
        # '<YYYY-MM-DDTHH:MM:SS-04:00>' that would corrupt temporal range queries.
        # last_modified always falls back to the real file mtime so it's never null.
        "created": valid_date(fm.get("created")),
        "last_modified": (valid_date(fm.get("last_modified"))
                          or datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")),
        "last_reviewed": valid_date(fm.get("last_reviewed")),
        "metadata": {
            **ownership,
            "subject": fm.get("subject"),
            "size_bytes": stat.st_size,
            "confidence": fm.get("confidence"),
            "sensitivity": fm.get("sensitivity"),
            "ttl_tier": fm.get("ttl_tier"),
            "tags": fm.get("tags") or fm.get("keywords") or [],
        },
    }
    # Verified journals (engine.journals) carry their provenance block: source path, source
    # node, SHA-256 and judge attestation. Only accepted from the journal shelf itself.
    if (isinstance(fm.get("journal"), dict) and str(node_id).startswith("journal:")
            and "ARCTURION_Brain/journals/" in str(rel)):
        node["metadata"]["journal"] = fm["journal"]

    edges: list[dict] = []
    asserted_at = now_iso()

    # 1) Frontmatter typed edges (canonical, high confidence).
    for key in _FM_EDGE_KEYS:
        val = fm.get(key)
        if not val:
            continue
        if isinstance(val, str):
            targets = [val]
        elif isinstance(val, (list, tuple)):
            targets = [str(x) for x in val if x and str(x).strip() not in {"—", "-"}]
        else:
            continue
        for t in targets:
            t = t.strip()
            if not t or t in {"—", "-", "None", "none"}:
                continue
            issue = endpoint_reference_issue(t)
            edges.append({
                "from": node_id,
                "to": t,
                "predicate": key,
                "family": predicate_family(key),
                "weight": 1.0,
                "confidence": 0.95,
                "asserted_by": "STATIC_FRONTMATTER",
                "asserted_at": asserted_at,
                "evidence_span": f"frontmatter:{key}",
                "metadata": ({"invalid_endpoint_reason": issue,
                              "raw_endpoint": t} if issue else {}),
            })

    # 2) Wikilinks → related_to (weight lower, since untyped).
    for link in extract_wikilinks(body):
        edges.append({
            "from": node_id,
            "to": link.strip(),
            "predicate": "related_to",
            "family": "structure",
            "weight": 0.6,
            "confidence": 0.7,
            "asserted_by": "STATIC_WIKILINK",
            "asserted_at": asserted_at,
            "evidence_span": "wikilink",
            "metadata": {},
        })

    # 3) Owned-by/Authored-by from frontmatter (if not already declared via _FM_EDGE_KEYS).
    owner = fm.get("owner") or (fm.get("agent") if not fm.get("authored_by") else None)
    owner_reference = str(owner).strip() if owner else ""
    if (owner_reference and "owned_by" not in fm
            and owner_reference.casefold() not in _OWNER_PLACEHOLDERS):
        edges.append({
            "from": node_id, "to": str(owner),
            "predicate": "owned_by", "family": "structure",
            "weight": 1.0, "confidence": 0.95,
            "asserted_by": "STATIC_FRONTMATTER", "asserted_at": asserted_at,
            "evidence_span": "frontmatter:owner", "metadata": {},
        })

    return node, edges
