'Attribute inference reasoner (Task 2) — propose missing node attributes from typed edges.'
from __future__ import annotations

import sqlite3
import sys

from . import config as C
from . import db as DB
from . import util as U

try:  # ALTAIR's endpoint-typing table — authoritative, but possibly uncommitted.
    from . import validator as V
    _DOMAIN_RANGE = getattr(V, "DOMAIN_RANGE", {}) or {}
except Exception:  # pragma: no cover - validator import must never sink the reasoner
    _DOMAIN_RANGE = {}

RULE_OWNER_FROM_OWNED_BY = "owner_from_owned_by"
EVIDENCE_PREDICATE = "owned_by"
CONFIDENCE = 0.85  # house cap for inferred assertions (see engine.edge_inference)

# The Founder's person-node is the one concept-family target that legitimately
# denotes an owner; `nodes.owner` spells that owner "FOUNDER".
# M4: this id is a hardcoded literal, not looked up. If the Founder node's id
# ever changes (re-index, re-key, node merge), the FOUNDER branch in
# _owner_tokens() below silently stops matching — no error, just fewer FOUNDER
# proposals. Check `nodes.id` for the Founder's person-node (family=concept,
# type=Entity) if owner_from_owned_by coverage of Founder-owned nodes drops.
_FOUNDER_NODE_ID = C.OWNER_NODE_ID
_FOUNDER_TOKEN = "FOUNDER"

_PROPOSAL_ID_LIKE = r"prop\_%"  # same exclusion + ESCAPE discipline as ontology.audit

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS inferred_attrs (
  node_id TEXT NOT NULL,
  attr TEXT NOT NULL,
  value TEXT NOT NULL,
  rule TEXT NOT NULL,
  confidence REAL DEFAULT 0.85,
  inferred_at TEXT NOT NULL,
  UNIQUE(node_id, attr, rule)
);
"""


def _norm(value: str) -> str:
    """Canonical comparison form of an owner token.

    `nodes.owner` spells the same owner three ways in the wild: 'ALTAIR',
    'altair', and the node-id form 'agent_altair'. Strip the node-id prefix
    (lowercase form only — the literal placeholder 'AGENT_NAME' must NOT
    collapse to 'NAME') and compare case-insensitively.
    """
    value = value.strip()
    if value.startswith("agent_"):
        value = value[len("agent_"):]
    return value.upper()


def _owner_tokens(conn: sqlite3.Connection):
    if not _FOUNDER_NODE_ID:
        raise ValueError("explicit owner node identity required for owner inference")
    """Map subject id -> {normalized token -> display value} from owned_by edges.

    Token sources, in display-preference order:
      1. a resolved agent-family target's title  (agent_altair -> 'ALTAIR')
      2. the Founder person-node                  -> 'FOUNDER'
      3. a dangling literal target                ('ALTAIR' with no node row)
    Resolved targets of any other family are not owner evidence; when ALTAIR's
    DOMAIN_RANGE declares a range for owned_by, families outside it are skipped
    even before that rule.
    """
    # M2: the import above is fail-soft against DOMAIN_RANGE being absent, but a
    # shape change to an individual entry (ALTAIR's table, not ours) shouldn't
    # crash the reasoner either — a non-mapping value degrades to "no declared
    # range", same as when the whole table is missing.
    _rule = _DOMAIN_RANGE.get(EVIDENCE_PREDICATE)
    rng = _rule.get("range") if isinstance(_rule, dict) else None
    rows = conn.execute(
        "SELECT e.from_id, e.to_id, tn.family, tn.title"
        "  FROM edges e"
        "  LEFT JOIN nodes tn ON tn.id = e.to_id"
        " WHERE e.predicate = ?"
        "   AND e.from_id NOT LIKE ? ESCAPE '\\'",
        (EVIDENCE_PREDICATE, _PROPOSAL_ID_LIKE),
    ).fetchall()
    tokens: dict[str, dict[str, str]] = {}
    for from_id, to_id, tfam, ttitle in rows:
        preference = None
        if to_id == _FOUNDER_NODE_ID:
            display, preference = _FOUNDER_TOKEN, 1
        elif tfam is None:
            display, preference = to_id, 2  # dangling literal, e.g. 'ALTAIR'
        elif rng is not None and tfam not in rng:
            continue  # outside ALTAIR's declared range — not owner evidence
        elif tfam == "agent":
            display, preference = (ttitle or to_id), 0
        else:
            continue  # non-agent, non-Founder resolved target — not owner evidence
        display = display.strip()
        if not display:
            continue
        norm = _norm(display)
        bucket = tokens.setdefault(from_id, {})
        prev = bucket.get(norm)
        if prev is None or preference < prev[0] or (preference == prev[0] and display < prev[1]):
            bucket[norm] = (preference, display)
    return {s: {n: d for n, (_, d) in b.items()} for s, b in tokens.items()}


def infer_attrs(conn=None, *, apply: bool = False, limit: int | None = None) -> dict:
    """Propose `nodes.owner` values from unambiguous owned_by evidence.

    Dry-run by default: with apply=False this function performs zero writes of
    any kind. With apply=True, proposals are staged into `inferred_attrs`
    (INSERT OR IGNORE — idempotent); `nodes` and `edges` are never touched.

    `limit` caps the number of PROPOSALS emitted (conflict detection still runs
    over the full evidence set).

    Returns {"proposed": [...], "conflicts": [...], "applied": int,
             "by_rule": {...}, "ambiguous": int}.
    """
    own = conn is None
    if own:
        # M1: when we own the connection AND apply=False, make the no-write
        # guarantee STRUCTURAL, not just "the code doesn't happen to issue
        # writes" — matches engine.closure's mode=ro precedent (the same
        # shape engine.ontology.audit() already uses) so a stray write raises
        # at the SQLite level instead of silently landing. apply=True keeps
        # DB.connect() exactly as before — it must still write inferred_attrs.
        if apply:
            conn = DB.connect()
        else:
            conn = sqlite3.connect(f"file:{C.BRAIN_DB}?mode=ro", uri=True)
    try:
        evidence = _owner_tokens(conn)
        owners = dict(conn.execute(
            "SELECT id, owner FROM nodes WHERE id IN (SELECT DISTINCT from_id "
            "FROM edges WHERE predicate = ?)",
            (EVIDENCE_PREDICATE,),
        ).fetchall())

        proposed: list[dict] = []
        conflicts: list[dict] = []
        ambiguous = 0
        for subject in sorted(evidence):
            if subject not in owners:
                continue  # edge from a subject with no node row — nothing to annotate
            toks = evidence[subject]
            if len(toks) > 1:
                ambiguous += 1
                continue  # ambiguity is not a conclusion
            value = next(iter(toks.values()))
            current = owners.get(subject)
            if current is not None and current.strip():
                if _norm(current) != _norm(value):
                    conflicts.append({
                        "node_id": subject, "attr": "owner",
                        "current": current, "inferred": value,
                        "rule": RULE_OWNER_FROM_OWNED_BY,
                    })
                continue  # never overwrite a non-null value
            if limit is not None and len(proposed) >= limit:
                continue
            proposed.append({
                "node_id": subject, "attr": "owner", "value": value,
                "rule": RULE_OWNER_FROM_OWNED_BY, "confidence": CONFIDENCE,
            })

        applied = 0
        if apply and proposed:
            conn.execute(_TABLE_SQL)
            now = U.now_iso()
            for p in proposed:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO inferred_attrs "
                    "(node_id, attr, value, rule, confidence, inferred_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (p["node_id"], p["attr"], p["value"], p["rule"],
                     p["confidence"], now),
                )
                applied += cur.rowcount
            conn.commit()

        return {
            "proposed": proposed,
            "conflicts": conflicts,
            "applied": applied,
            "by_rule": {RULE_OWNER_FROM_OWNED_BY: len(proposed)},
            "ambiguous": ambiguous,
        }
    finally:
        if own:
            conn.close()


def _main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Propose missing node attributes from typed edges (detect-and-propose; "
                    "writes only to inferred_attrs, and only with --apply)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="report without writing (the default)")
    mode.add_argument("--apply", action="store_true",
                      help="stage proposals into the inferred_attrs table")
    ap.add_argument("--limit", type=int, default=None, help="cap proposals emitted")
    args = ap.parse_args(argv)

    out = infer_attrs(apply=args.apply, limit=args.limit)
    mode_s = "APPLY" if args.apply else "dry-run"
    print(f"[reasoner] {mode_s}: proposed={len(out['proposed'])} "
          f"conflicts={len(out['conflicts'])} ambiguous={out['ambiguous']} "
          f"applied={out['applied']} by_rule={out['by_rule']}")
    for p in out["proposed"][:10]:
        print(f"  propose {p['node_id']}.{p['attr']} = {p['value']!r}  ({p['rule']}, {p['confidence']})")
    if len(out["proposed"]) > 10:
        print(f"  ... {len(out['proposed']) - 10} more")
    for c in out["conflicts"][:10]:
        print(f"  CONFLICT {c['node_id']}.{c['attr']}: current={c['current']!r} vs inferred={c['inferred']!r}")
    if len(out["conflicts"]) > 10:
        print(f"  ... {len(out['conflicts']) - 10} more")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
