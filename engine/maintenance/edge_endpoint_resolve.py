"""Dangling edge-endpoint resolution (Leg 5, retrieval-precision programme).

WHY THIS EXISTS
----------------
`engine.db.edge_health()` names a class of predicates "fully dangling": every
edge asserted under that predicate has an endpoint id that never resolves to
a row in `nodes`. The predicate is vocabulary the walker can never traverse —
not a link, dead weight (see `index/coverage_assertion.md`, `edge-walkability`).

Some of that dangling is a genuine identity mismatch, not a missing fact: a
harvester minted a bare-slug id (`tool_chrome`) for something that a DIFFERENT
producer already catalogued under a real node id
(`app_google_chrome_bfa04b`, title "Google Chrome"). The fact was true and
already in the graph — it just could not be walked because two producers
never agreed on a name for the same entity.

This module resolves exactly that class of gap, and no other:
  1. `aliases` table (alias -> canonical_id), case-insensitive — the
     general, already-curated identity mapping (Leg 3/ENTITY_ALIASES.md +
     Leg 1 dedup superseded-twin reparenting both land here).
  2. `nodes.path` suffix match — a dangling id that is itself a relative
     file path (`tested_by`/`tests` asserted straight from frontmatter)
     resolves to the one node whose indexed `path` ends with it.
  3. Curated name-identity match — a dangling id built from a known slug
     convention (`tool_<name>`, an absolute interpreter path) resolves to
     the one Application/CLI_Tool/Service node whose normalized title is
     the same word. Requires an EXACT, UNIQUE match; ambiguous or absent
     matches are left alone.

What this module explicitly refuses to do: invent a node. `hw_macbook_m5`
(hosts/runs_on) and `tool_homebrew` (installs/installed_by) and the
`scan_router`/`docintegrity` service ids (configures/configured_by) have no
corresponding node anywhere in the graph under any name — the harvester that
asserted the edge never materialized the node on the other end. That is a
producer gap, not an identity mismatch, and stays dangling on purpose.
`depends_on`/`depended_on_by` point at literal markdown table-cell prose
(a frontmatter-parsing bug upstream), not an id in any namespace — also left
alone. `synonymous_with` (`engine.aliases.synonymous_edges`) asserts
canonical -> raw-alias-phrase edges, so its `to_id` is almost never a node id
by design; most of it resolves anyway because the SAME alias table this
module reads back in as mechanism 1 already maps a large share of those
phrases to a canonical id that IS a materialized node (agents, concepts,
tools) — the remainder (alias phrases with no node-backed canonical, e.g.
`:8765`, `Tier 0a`) stays honestly dangling.

Writes are additive and reversible: a resolved fact is inserted as a NEW
edge row with the endpoint replaced by its resolved id, `asserted_by`
suffixed with `+ENDPOINT_RESOLVE_L5`, and `metadata.resolved_from` recording
the original dangling id + mechanism. The original dangling edge is never
touched. Undo only the exact inserted edge IDs returned for a batch; the producer
suffix also marks older repairs and is not a safe whole-history undo target.

Usage:
    cd ~/Desktop/ARCTURION_Brain
    python3 -m engine.maintenance.edge_endpoint_resolve            # apply
    python3 -m engine.maintenance.edge_endpoint_resolve --dry-run  # report only
    python3 -m engine.maintenance.edge_endpoint_resolve --json
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .. import config as C
from .. import db as DB
from ..util import now_iso

PRODUCER_SUFFIX = "+ENDPOINT_RESOLVE_L5"

# Node types eligible for the curated name-identity match (mechanism 3). Kept
# narrow on purpose — these are the catalogues a slug id could plausibly be
# a bare-name alias of; expanding this list is a deliberate, reviewed choice,
# not a default.
_NAME_MATCH_TYPES = ("Application", "CLI_Tool", "Service")

_NORM_RE = re.compile(r"[^a-z0-9]+")


def _norm(s: str) -> str:
    return _NORM_RE.sub("", s.lower())


def _slug_candidates(dangling_id: str) -> list[str]:
    """Candidate bare-name tokens a dangling id might be a slug/path form of."""
    out: list[str] = []
    s = dangling_id
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    for prefix in ("tool_", "cli_", "app_", "entity_tool_"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    # strip a file extension (python3.11 -> python3, script.py -> script)
    if "." in s and not s.rsplit(".", 1)[-1].isdigit():
        s = s.rsplit(".", 1)[0]
    out.append(s)
    # strip a trailing version digit run (python3 -> python)
    stripped = re.sub(r"\d+$", "", s)
    if stripped and stripped != s:
        out.append(stripped)
    return [c for c in {c.strip() for c in out} if c]


def _connect_rw(db_path: Path | None = None) -> sqlite3.Connection:
    return DB.connect(db_path or C.BRAIN_DB)


def _load_alias_map(conn: sqlite3.Connection) -> dict[str, str | None]:
    """Case-folded aliases; unavailable/ambiguous aliases explicitly refuse fallback."""
    grouped: dict[str, dict[str, bool]] = {}
    for row in conn.execute(
        "SELECT a.alias, a.canonical_id, n.id AS node_id FROM aliases a "
        "LEFT JOIN nodes n ON n.id = a.canonical_id"
    ):
        key = row["alias"].strip().casefold()
        if key:
            grouped.setdefault(key, {})[row["canonical_id"]] = row["node_id"] is not None
    return {
        key: next(iter(targets)) if len(targets) == 1 and all(targets.values()) else None
        for key, targets in grouped.items()
    }

def _load_path_index(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """[(path, node_id), ...] for nodes with a non-empty path."""
    return [
        (r["path"], r["id"])
        for r in conn.execute("SELECT id, path FROM nodes WHERE path IS NOT NULL AND path != ''")
    ]


def _load_name_index(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """normalized(title) -> [node_id, ...] for the curated type allowlist."""
    out: dict[str, list[str]] = {}
    placeholders = ",".join("?" for _ in _NAME_MATCH_TYPES)
    rows = conn.execute(
        f"SELECT id, title FROM nodes WHERE type IN ({placeholders}) AND title IS NOT NULL",
        _NAME_MATCH_TYPES,
    )
    for r in rows:
        out.setdefault(_norm(r["title"]), []).append(r["id"])
    return out


def resolve_one(
    dangling_id: str,
    conn: sqlite3.Connection,
    alias_map: dict[str, str | None],
    path_index: list[tuple[str, str]],
    name_index: dict[str, list[str]],
    mechanisms: tuple[str, ...] = ("alias_table", "path_suffix", "name_identity"),
) -> tuple[str, str] | None:
    """Return a node-backed exact resolution; conflicting aliases cannot fall back."""
    key = dangling_id.strip().casefold()
    if key in alias_map and alias_map[key] is None:
        return None
    if "alias_table" in mechanisms:
        hit = alias_map.get(key)
        if hit:
            return hit, "alias_table"

    if "path_suffix" in mechanisms and "/" in dangling_id:
        matches = sorted({nid for p, nid in path_index if p.endswith(dangling_id)})
        if len(matches) == 1:
            return matches[0], "path_suffix"

    if "name_identity" in mechanisms:
        for cand in _slug_candidates(dangling_id):
            key = _norm(cand)
            if not key:
                continue
            matches = sorted(set(name_index.get(key, [])))
            if len(matches) == 1:
                return matches[0], "name_identity"
    return None


_MECHANISMS = ("alias_table", "path_suffix", "name_identity")


def _exact_filter(values: list[str] | None, label: str) -> tuple[str, ...] | None:
    if values is None:
        return None
    if not values or any(not isinstance(v, str) or not v.strip() for v in values):
        raise ValueError(f"{label} must be a non-empty exact allowlist")
    return tuple(dict.fromkeys(values))

def run(
    conn: sqlite3.Connection,
    predicates: list[str],
    dry_run: bool = False,
    *,
    asserted_by: list[str] | None = None,
    evidence_spans: list[str] | None = None,
    mechanisms: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Add resolved twins within optional exact producer/evidence/mechanism bounds.

    commit=False leaves transaction ownership with the caller. Returned inserted IDs
    identify this batch only; do not undo a batch by deleting all historical twins.
    """
    producers = _exact_filter(asserted_by, "asserted_by")
    spans = _exact_filter(evidence_spans, "evidence_spans")
    allowed = _exact_filter(mechanisms, "mechanisms") or _MECHANISMS
    if any(m not in _MECHANISMS for m in allowed):
        raise ValueError("unknown endpoint-resolution mechanism")
    alias_map = _load_alias_map(conn)
    path_index = _load_path_index(conn) if "path_suffix" in allowed else []
    name_index = _load_name_index(conn) if "name_identity" in allowed else {}

    resolved_edges = 0
    inserted_edges = 0
    inserted_edge_ids: list[int] = []
    by_predicate: dict[str, dict[str, Any]] = {}
    refused: dict[str, list[str]] = {}
    asserted_at = now_iso()

    for pred in predicates:
        clauses, params = ["predicate = ?"], [pred]
        for column, values in (("asserted_by", producers), ("evidence_span", spans)):
            if values is not None:
                clauses.append(f"{column} IN ({','.join('?' for _ in values)})")
                params.extend(values)
        rows = conn.execute(
            "SELECT id, from_id, to_id, family, weight, confidence, asserted_by, "
            "evidence_span, metadata FROM edges WHERE " + " AND ".join(clauses)
            + " ORDER BY id",
            params,
        ).fetchall()
        pred_stats = {"edges_seen": len(rows), "resolved": 0, "inserted": 0, "mechanisms": {}}
        seen_dangling: set[str] = set()

        for r in rows:
            from_ok = conn.execute("SELECT 1 FROM nodes WHERE id=?", (r["from_id"],)).fetchone()
            to_ok = conn.execute("SELECT 1 FROM nodes WHERE id=?", (r["to_id"],)).fetchone()
            if from_ok and to_ok:
                continue

            new_from, new_to = r["from_id"], r["to_id"]
            mech = None
            for side, is_backed in (("from_id", from_ok), ("to_id", to_ok)):
                if is_backed:
                    continue
                got = resolve_one(r[side], conn, alias_map, path_index, name_index, allowed)
                if got:
                    endpoint, mechanism = got
                    if side == "from_id":
                        new_from = endpoint
                    else:
                        new_to = endpoint
                    mech = mech or mechanism
                else:
                    seen_dangling.add(r[side])

            if new_from == r["from_id"] and new_to == r["to_id"]:
                continue
            if not (
                conn.execute("SELECT 1 FROM nodes WHERE id=?", (new_from,)).fetchone()
                and conn.execute("SELECT 1 FROM nodes WHERE id=?", (new_to,)).fetchone()
            ):
                continue

            resolved_edges += 1
            pred_stats["resolved"] += 1
            pred_stats["mechanisms"][mech] = pred_stats["mechanisms"].get(mech, 0) + 1
            if dry_run:
                continue

            try:
                meta = json.loads(r["metadata"]) if r["metadata"] else {}
            except (TypeError, ValueError):
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            meta["resolved_from"] = {
                "source_edge_id": r["id"],
                "original_from_id": r["from_id"],
                "original_to_id": r["to_id"],
                "mechanism": mech,
                "resolved_by": "engine.maintenance.edge_endpoint_resolve",
            }
            new_asserted_by = f"{r['asserted_by'] or 'UNKNOWN'}{PRODUCER_SUFFIX}"
            inserted = DB.insert_edge(conn, {
                "from": new_from, "to": new_to, "predicate": pred,
                "family": r["family"], "weight": r["weight"],
                "confidence": max(0.0, (r["confidence"] if r["confidence"] is not None else 1.0) * 0.9),
                "asserted_by": new_asserted_by, "asserted_at": asserted_at,
                "evidence_span": f"endpoint-resolved:{mech}:{r['evidence_span'] or ''}",
                "metadata": meta,
            })
            # Several original aliases can collapse onto one existing unique fact.
            # Count candidates and physical insertions separately.
            if inserted:
                inserted_edges += 1
                pred_stats["inserted"] += 1
                landed = conn.execute(
                    "SELECT id FROM edges WHERE from_id=? AND to_id=? "
                    "AND predicate=? AND asserted_by=?",
                    (new_from, new_to, pred, new_asserted_by),
                ).fetchone()
                if landed is None:
                    raise RuntimeError("inserted endpoint twin has no attributable row")
                inserted_edge_ids.append(landed["id"])

        pred_stats["still_dangling_distinct_ids"] = sorted(seen_dangling)
        by_predicate[pred] = pred_stats
        if seen_dangling:
            refused[pred] = sorted(seen_dangling)

    if not dry_run and commit:
        conn.commit()

    return {
        "predicates_processed": predicates,
        "scope": {
            "asserted_by": producers,
            "evidence_spans": spans,
            "mechanisms": allowed,
        },
        "resolved_edges": resolved_edges,
        "inserted_edges": inserted_edges if not dry_run else None,
        "inserted_edge_ids": inserted_edge_ids,
        "by_predicate": by_predicate,
        "refused_dangling_ids": refused,
        "dry_run": dry_run,
    }

def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve honestly-resolvable dangling edge endpoints")
    ap.add_argument("--dry-run", action="store_true", help="report only, no writes")
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--predicates", nargs="+", default=None,
        help="restrict to these predicates (default: current fully-dangling predicates)",
    )
    ap.add_argument("--asserted-by", nargs="+", default=None, help="exact producer allowlist")
    ap.add_argument("--evidence-spans", nargs="+", default=None, help="exact evidence-span allowlist")
    ap.add_argument("--mechanisms", nargs="+", choices=_MECHANISMS, default=None)
    args = ap.parse_args()

    conn = _connect_rw()
    try:
        if args.predicates:
            predicates = args.predicates
        else:
            health = DB.edge_health(conn)
            predicates = health["fully_dangling_predicates"]
        result = run(
            conn, predicates, dry_run=args.dry_run,
            asserted_by=args.asserted_by, evidence_spans=args.evidence_spans,
            mechanisms=args.mechanisms,
        )
    finally:
        conn.close()

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"predicates processed: {result['predicates_processed']}")
        print(f"resolved edges: {result['resolved_edges']} "
              f"(inserted: {result['inserted_edges']}, dry_run={result['dry_run']})")
        for pred, stats in result["by_predicate"].items():
            print(f"  {pred}: resolved={stats['resolved']}/{stats['edges_seen']} "
                  f"inserted={stats['inserted']} mechanisms={stats['mechanisms']}")
        if result["refused_dangling_ids"]:
            print("refused (no honest resolution, still dangling):")
            for pred, ids in result["refused_dangling_ids"].items():
                print(f"  {pred}: {ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
