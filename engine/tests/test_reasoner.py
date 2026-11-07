"""Hermetic tests for the attribute inference reasoner (Task 2). No pytest, no production DB.

Run:  python3 -m engine.tests.test_reasoner
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from .. import reasoner as R


# ── Fixture ────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE nodes (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, family TEXT NOT NULL,
  title TEXT, path TEXT, owner TEXT, status TEXT, metadata TEXT
);
CREATE TABLE edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_id TEXT NOT NULL, to_id TEXT NOT NULL, predicate TEXT NOT NULL,
  family TEXT NOT NULL, weight REAL DEFAULT 1.0, confidence REAL DEFAULT 1.0,
  asserted_by TEXT, asserted_at TEXT, evidence_span TEXT, metadata TEXT,
  UNIQUE(from_id, to_id, predicate, asserted_by)
);
"""

_NODES = [
    # (id, type, family, title, owner)
    ("agent_altair", "Agent", "agent", "ALTAIR", None),
    ("agent_nemesis", "Agent", "agent", "NEMESIS", None),
    ("founder_avery_example", "Entity", "concept", "Avery Example", None),
    ("doc_resolved", "Document", "document", "Some Doc", None),
    ("concept_misc", "Entity", "concept", "Not The Founder", None),
    ("n_clean", "Document", "document", "clean evidence", None),
    ("n_dangling", "Document", "document", "dangling literal", None),
    ("n_variants", "Document", "document", "case variants collapse", None),
    ("n_ambig", "Document", "document", "ambiguous", None),
    ("n_agree", "Document", "document", "already consistent", "ALTAIR"),
    ("n_agree_idform", "Document", "document", "node-id owner form", "agent_altair"),
    ("n_conflict", "Document", "document", "contradiction", "UNIVERSE"),
    ("prop_x", "decision", "concept", "staged proposal", None),
    ("propaganda", "Document", "document", "prop-prefix, not proposal ns", None),
    ("n_founder", "Document", "document", "founder-owned", None),
    ("n_concept_target", "Document", "document", "concept target is not owner", None),
    ("n_nonrange", "Document", "document", "document target is not owner", None),
]

_EDGES = [
    # (from_id, to_id)
    ("n_clean", "agent_altair"),
    ("n_dangling", "COVENANT"),
    ("n_variants", "agent_altair"),
    ("n_variants", "ALTAIR"),
    ("n_variants", "altair"),
    ("n_ambig", "agent_altair"),
    ("n_ambig", "NEMESIS"),
    ("n_agree", "agent_altair"),
    ("n_agree_idform", "agent_altair"),
    ("n_conflict", "agent_altair"),
    ("prop_x", "agent_altair"),
    ("propaganda", "agent_nemesis"),
    ("n_founder", "founder_avery_example"),
    ("n_concept_target", "concept_misc"),
    ("n_nonrange", "doc_resolved"),
    ("ghost_subject", "agent_altair"),  # edge whose subject has no node row
]

ROLLBACK_SQL = "DELETE FROM inferred_attrs WHERE rule = 'owner_from_owned_by'"


def _fixture(tmp: Path, name: str = "fixture.sqlite", nodes=_NODES, edges=_EDGES) -> Path:
    db = tmp / name
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes (id, type, family, title, owner) VALUES (?,?,?,?,?)", nodes)
    conn.executemany(
        "INSERT INTO edges (from_id, to_id, predicate, family, asserted_by) "
        "VALUES (?,?,'owned_by','structure','fixture')", edges)
    conn.commit()
    conn.close()
    return db


def _counts(conn) -> tuple[int, int]:
    n = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    e = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    return n, e


def _owners(conn) -> dict:
    return dict(conn.execute("SELECT id, owner FROM nodes").fetchall())


# ── Cases ──────────────────────────────────────────────────────────────────

def case_1_rule_fires_on_clean_evidence(conn) -> dict:
    out = R.infer_attrs(conn)
    by_node = {p["node_id"]: p for p in out["proposed"]}
    assert "n_clean" in by_node, f"clean evidence must yield a proposal: {sorted(by_node)}"
    p = by_node["n_clean"]
    assert p["attr"] == "owner" and p["value"] == "ALTAIR", p
    assert p["rule"] == R.RULE_OWNER_FROM_OWNED_BY and p["confidence"] == 0.85, p
    assert by_node["n_dangling"]["value"] == "COVENANT", "dangling literal is evidence"
    assert by_node["n_founder"]["value"] == "FOUNDER", "founder node maps to FOUNDER"
    assert by_node["propaganda"]["value"] == "NEMESIS", \
        "'prop' prefix without '_' is NOT the proposal namespace (ESCAPE discipline)"
    assert out["by_rule"] == {R.RULE_OWNER_FROM_OWNED_BY: len(out["proposed"])}
    return out


def case_2_ambiguous_yields_nothing(out) -> None:
    ids = {p["node_id"] for p in out["proposed"]}
    assert "n_ambig" not in ids, "two distinct owners is not a conclusion"
    assert out["ambiguous"] == 1, f"expected exactly 1 ambiguous subject, got {out['ambiguous']}"
    # ...but same-owner case variants collapse to ONE token and DO propose
    assert "n_variants" in ids, "agent_altair + 'ALTAIR' + 'altair' is one owner, not three"
    by_node = {p["node_id"]: p for p in out["proposed"]}
    assert by_node["n_variants"]["value"] == "ALTAIR", "resolved agent title is the display form"


def case_3_existing_owner_never_touched(out) -> None:
    ids = {p["node_id"] for p in out["proposed"]}
    assert "n_agree" not in ids and "n_conflict" not in ids, \
        "nodes with a non-null owner must never receive a proposal"
    assert "n_agree_idform" not in ids, "'agent_altair' owner form agrees with ALTAIR"
    conflict_ids = {c["node_id"] for c in out["conflicts"]}
    assert "n_agree" not in conflict_ids and "n_agree_idform" not in conflict_ids, \
        "agreement (incl. normalized forms) is not a conflict"


def case_4_contradiction_lands_in_conflicts(out) -> None:
    cs = [c for c in out["conflicts"] if c["node_id"] == "n_conflict"]
    assert len(cs) == 1, f"expected exactly 1 conflict, got {out['conflicts']}"
    c = cs[0]
    assert c["attr"] == "owner" and c["current"] == "UNIVERSE" and c["inferred"] == "ALTAIR", c
    assert c["rule"] == R.RULE_OWNER_FROM_OWNED_BY, c


def case_5_scope_exclusions(out) -> None:
    ids = {p["node_id"] for p in out["proposed"]}
    assert "prop_x" not in ids, "staged proposals answer to the approval gate, not inference"
    assert "ghost_subject" not in ids, "an edge from a nonexistent node annotates nothing"
    assert "n_concept_target" not in ids, "a non-Founder concept target is not owner evidence"
    assert "n_nonrange" not in ids, "a document-family target is not owner evidence"


def case_6_dry_run_writes_nothing(tmp: Path) -> None:
    db = _fixture(tmp, "dryrun.sqlite")
    before = (db.stat().st_mtime_ns, db.stat().st_size)
    conn = sqlite3.connect(db)
    n0, e0 = _counts(conn)
    owners0 = _owners(conn)
    out = R.infer_attrs(conn)          # apply defaults to False
    assert out["applied"] == 0
    assert _counts(conn) == (n0, e0)
    assert _owners(conn) == owners0
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "inferred_attrs" not in tables, "dry-run must not even create the table"
    conn.close()
    assert (db.stat().st_mtime_ns, db.stat().st_size) == before, \
        "dry-run must leave the DB file byte-identical"


def case_7_apply_idempotent_and_additive(tmp: Path) -> None:
    db = _fixture(tmp, "apply.sqlite")
    conn = sqlite3.connect(db)
    n0, e0 = _counts(conn)
    owners0 = _owners(conn)

    out1 = R.infer_attrs(conn, apply=True)
    assert out1["applied"] == len(out1["proposed"]) > 0, out1
    rows1 = conn.execute(
        "SELECT node_id, attr, value, rule, confidence FROM inferred_attrs "
        "ORDER BY node_id").fetchall()
    assert len(rows1) == out1["applied"]
    assert all(r[3] == R.RULE_OWNER_FROM_OWNED_BY and r[4] == 0.85 for r in rows1)
    assert all(conn.execute(
        "SELECT inferred_at FROM inferred_attrs").fetchall()), "provenance timestamp required"

    out2 = R.infer_attrs(conn, apply=True)  # second run: same proposals, zero new rows
    assert out2["applied"] == 0, f"second apply must insert nothing, got {out2['applied']}"
    assert len(out2["proposed"]) == len(out1["proposed"])
    rows2 = conn.execute(
        "SELECT node_id, attr, value, rule, confidence FROM inferred_attrs "
        "ORDER BY node_id").fetchall()
    assert rows2 == rows1, "apply must be idempotent row-for-row"

    # additive only: nodes/edges counts and every owner value are untouched
    assert _counts(conn) == (n0, e0), "apply must never touch nodes/edges"
    assert _owners(conn) == owners0, "apply must never write nodes.owner"

    # documented rollback removes exactly the inferred rows
    conn.execute(ROLLBACK_SQL)
    conn.commit()
    left = conn.execute("SELECT COUNT(*) FROM inferred_attrs").fetchone()[0]
    assert left == 0, f"rollback must remove exactly the inferred rows, {left} remain"
    assert _counts(conn) == (n0, e0)
    conn.close()


def case_8_limit_honoured(conn) -> None:
    out = R.infer_attrs(conn, limit=2)
    assert len(out["proposed"]) == 2, f"limit=2 must cap proposals, got {len(out['proposed'])}"
    out0 = R.infer_attrs(conn, limit=0)
    assert out0["proposed"] == [], "limit=0 proposes nothing"
    assert out0["conflicts"], "conflict detection still runs under limit"


def case_9_empty_graph_is_noop(tmp: Path) -> None:
    db = _fixture(tmp, "empty.sqlite", nodes=[], edges=[])
    conn = sqlite3.connect(db)
    out = R.infer_attrs(conn, apply=True)   # even with apply, nothing to stage
    assert out == {"proposed": [], "conflicts": [], "applied": 0,
                   "by_rule": {R.RULE_OWNER_FROM_OWNED_BY: 0}, "ambiguous": 0}, out
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "inferred_attrs" not in tables, "no proposals -> no table"
    conn.close()


# ── Runner ─────────────────────────────────────────────────────────────────

def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="brain_reasoner_test_"))
    try:
        db = _fixture(tmp)
        conn = sqlite3.connect(db)
        out = case_1_rule_fires_on_clean_evidence(conn)
        case_2_ambiguous_yields_nothing(out)
        case_3_existing_owner_never_touched(out)
        case_4_contradiction_lands_in_conflicts(out)
        case_5_scope_exclusions(out)
        conn.close()
        case_6_dry_run_writes_nothing(tmp)
        case_7_apply_idempotent_and_additive(tmp)
        conn = sqlite3.connect(db)
        case_8_limit_honoured(conn)
        conn.close()
        case_9_empty_graph_is_noop(tmp)
        print("PASS — reasoner: owner_from_owned_by fires on agent/dangling/Founder evidence, "
              "ambiguity proposes nothing, case-variant owners collapse to one token, non-null "
              "owner never overwritten (normalized agreement is not a conflict), contradiction "
              "-> conflicts, prop_*/ghost/non-range targets excluded, dry-run byte-identical, "
              "apply idempotent + additive-only with working one-query rollback, limit honoured, "
              "empty graph no-op")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run())
