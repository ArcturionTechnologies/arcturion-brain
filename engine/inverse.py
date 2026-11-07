"""Auto-derive inverse edges for the asymmetric portion of the predicate vocab."""

from __future__ import annotations
import sqlite3
from . import db as DB
from .util import now_iso

INVERSE_MAP = {
    "imports": "imported_by", "imported_by": "imports",
    "calls": "called_by", "called_by": "calls",
    "executes": "executed_by", "executed_by": "executes",
    "configures": "configured_by", "configured_by": "configures",
    "runs_on": "hosts", "hosts": "runs_on",
    "installs": "installed_by", "installed_by": "installs",
    "exposes": "exposed_by", "exposed_by": "exposes",
    "bookmarked_in": "contains_bookmark", "contains_bookmark": "bookmarked_in",
    "mentioned_in": "mentions", "mentions": "mentioned_in",
    "participates_in": "has_participant", "has_participant": "participates_in",
    "instance_of": "class_of", "class_of": "instance_of",
    "cites": "cited_by", "cited_by": "cites",
    "depends_on": "depended_on_by", "depended_on_by": "depends_on",
    "derived_from": "source_of", "source_of": "derived_from",
    "part_of": "contains", "contains": "part_of",
    "owned_by": "owns", "owns": "owned_by",
    "authored_by": "authored", "authored": "authored_by",
    "maintained_by": "maintains", "maintains": "maintained_by",
    "supersedes": "superseded_by", "superseded_by": "supersedes",
    "tests": "tested_by", "tested_by": "tests",
    "answers": "answered_by", "answered_by": "answers",
    "promoted_from": "promoted_to", "promoted_to": "promoted_from",
    "preceded_by": "followed_by", "followed_by": "preceded_by",
    # Temporal / session (Brain v2 Phase 4)
    "touched_by": "touches", "touches": "touched_by",
    "modified_by": "modifies", "modifies": "modified_by",
    "changed_in_session": "changed", "changed": "changed_in_session",
    "occurred_at": "time_of", "time_of": "occurred_at",
    "discussed_in": "discusses", "discusses": "discussed_in",
}

PREDICATE_FAMILY = {
    # carried in markdown.py too; duplicated here so this module is self-contained.
    "imports": "code", "imported_by": "code", "calls": "code", "called_by": "code",
    "executes": "code", "executed_by": "code", "configures": "code", "configured_by": "code",
    "runs_on": "runtime", "hosts": "runtime", "installs": "runtime", "installed_by": "runtime",
    "exposes": "runtime", "exposed_by": "runtime",
    "bookmarked_in": "external", "contains_bookmark": "external",
    "mentioned_in": "external", "mentions": "external",
    "participates_in": "social", "has_participant": "social",
    "instance_of": "semantic", "class_of": "semantic",
    "cites": "evidence", "cited_by": "evidence",
    "depends_on": "reasoning", "depended_on_by": "reasoning",
    "derived_from": "evidence", "source_of": "evidence",
    "part_of": "structure", "contains": "structure",
    "owned_by": "structure", "owns": "structure",
    "authored_by": "structure", "authored": "structure",
    "maintained_by": "structure", "maintains": "structure",
    "supersedes": "lifecycle", "superseded_by": "lifecycle",
    "tests": "reasoning", "tested_by": "reasoning",
    "answers": "reasoning", "answered_by": "reasoning",
    "promoted_from": "lifecycle", "promoted_to": "lifecycle",
    "preceded_by": "lifecycle", "followed_by": "lifecycle",
    "touched_by": "temporal", "touches": "temporal",
    "modified_by": "temporal", "modifies": "temporal",
    "changed_in_session": "temporal", "changed": "temporal",
    "occurred_at": "temporal", "time_of": "temporal",
    "discussed_in": "temporal", "discusses": "temporal",
}


def derive_inverses(conn: sqlite3.Connection) -> int:
    """For every existing edge whose predicate has an inverse, emit the inverse."""
    asserted_at = now_iso()
    rows = conn.execute(
        f"SELECT from_id, to_id, predicate FROM edges WHERE predicate IN ({','.join('?'*len(INVERSE_MAP))})",
        list(INVERSE_MAP.keys()),
    ).fetchall()
    inserted = 0
    for r in rows:
        inv = INVERSE_MAP.get(r["predicate"])
        if not inv:
            continue
        fam = PREDICATE_FAMILY.get(inv, "structure")
        edge = {
            "from": r["to_id"], "to": r["from_id"], "predicate": inv,
            "family": fam, "weight": 1.0, "confidence": 0.95,
            "asserted_by": "INVERSE_DERIVATION", "asserted_at": asserted_at,
            "evidence_span": f"inverse-of:{r['predicate']}", "metadata": {},
        }
        if DB.insert_edge(conn, edge):
            inserted += 1
    return inserted
