"""Temporal validity pass (Leg 4, retrieval-honesty programme, 2026-08-06).

"Stale facts lose" — but an additive `-12` score penalty (`brain._authority_score`'s
`superseded_by` check) is just a number competing with other numbers, so there is always a
fusion+recency+similarity combination that beats it. The Zep/Graphiti fix is a validity
WINDOW, not a bigger penalty: `nodes.valid_until` sorts a node into a tier BELOW every live
node (see `brain.rank_key_v2` / `config.TEMPORAL_VALIDITY_TIER`) regardless of its score —
and CONTRADICTION INVALIDATES, it never deletes. An expired node stays in the index, stays
retrievable, and remains the answer when it is the only node that has one.

This module is the ONLY writer of `nodes.valid_until`, and it writes ONLY from an explicit
record naming the node:

  (a) a `status` in {"active", "resolved", "verified"} record in `index/supersessions.jsonl`
      carrying `old_id`/`new_id` fields (a str or list of str) that resolve to real node ids —
      the OLD id(s) are stamped `valid_until` = the record's `activated` (falling back to
      `verified`, then `created`) timestamp.
  (b) a `status: "resolved"` record in `index/contradictions.jsonl` carrying an
      `invalidates_id` field that resolves to a real node id — stamped `valid_until` =
      `resolved_at` (falling back to `created`).

Both jsonl files predate this leg and their existing entries are free-text governance log
(no node ids at all) — `old_id`/`new_id`/`invalidates_id` are new, additive, OPTIONAL keys.
A record without them is simply not actionable by this pass (and is counted as such), never
an error: the governance log keeps recording history in prose without every entry needing to
resolve to a graph node.

A THIRD thing this pass does — and the ONLY place it infers rather than reads an explicit
record — is scan for automatic same-subject candidates: two live (`status="active"`, no
`valid_until` yet), same-family nodes whose titles normalize to the same subject (after
stripping a trailing ISO-date decoration) but whose `last_modified` differs by more than a
day. That is exactly the shape a real supersession takes BEFORE anyone logs it. This pass
NEVER applies that inference — Founder/COVENANT governance owns supersession, not a heuristic
— it only stages a PROPOSAL (`staging/proposals/`, same jsonl-ticket convention
`mcp_server._write_proposal` uses) naming the candidate pair and the evidence, for a human (or
COVENANT) to turn into a real supersessions.jsonl record if it holds up.

Run:
    python3 -m engine.maintenance.validity_pass --dry-run   # preview, writes nothing
    python3 -m engine.maintenance.validity_pass             # stamp valid_until + propose
    python3 -m engine.maintenance.validity_pass --no-propose  # stamp only, skip inference
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .. import config as C
from .. import db as DB
from ..util import now_iso, short_hash

_TRAILING_DATE = re.compile(r"[\s_\-]*20\d{2}[\-_]?\d{2}[\-_]?\d{2}\s*$")


# ── record loading ────────────────────────────────────────────────────────────

def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # malformed line — skip, don't crash a nightly pass over it
    return out


def _as_ids(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [x for x in v if isinstance(x, str)]
    return []


def _existing_ids(conn, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id FROM nodes WHERE id IN ({placeholders})", ids
    ).fetchall()
    return {r["id"] for r in rows}


# ── (a) supersessions.jsonl → valid_until on the OLD id(s) ────────────────────

_SUPERSESSION_ACTIONABLE_STATUS = {"active", "resolved", "verified"}


def stamp_from_supersessions(conn, path: Path, verbose: bool = True,
                             apply: bool = True,
                             records: list[dict] | None = None) -> dict:
    # Existing callers keep the historical whole-file behavior. Bounded callers pass
    # only records created for their verified source batch.
    records = _load_jsonl(path) if records is None else records
    stamped: list[tuple[str, str, str]] = []  # (node_id, valid_until, record_id)
    skipped_no_ids = 0
    skipped_unresolved = 0
    skipped_status = 0
    already_current = 0

    for rec in records:
        old_ids = _as_ids(rec.get("old_id"))
        new_ids = _as_ids(rec.get("new_id"))
        if not old_ids or not new_ids:
            skipped_no_ids += 1
            continue
        status = (rec.get("status") or "").strip().lower()
        if status not in _SUPERSESSION_ACTIONABLE_STATUS:
            skipped_status += 1
            continue
        valid_until = rec.get("activated") or rec.get("verified") or rec.get("created")
        if not valid_until:
            skipped_unresolved += 1
            continue
        found_old = _existing_ids(conn, old_ids)
        found_new = _existing_ids(conn, new_ids)
        # Both sides must resolve — a supersession record naming a node that no longer
        # exists (or was never ingested) is not something this pass can act on safely.
        if not found_old or not found_new:
            skipped_unresolved += 1
            continue
        for nid in found_old:
            current = conn.execute(
                "SELECT valid_until FROM nodes WHERE id=?", (nid,)
            ).fetchone()[0]
            if current == valid_until:
                already_current += 1
                continue
            stamped.append((nid, valid_until, rec.get("id") or "?"))

    if not apply:
        if verbose:
            print(f"[validity_pass:supersessions] DRY RUN — would stamp {len(stamped)} "
                  f"node(s) (skipped: no_ids={skipped_no_ids} status={skipped_status} "
                  f"unresolved={skipped_unresolved})")
        return {"stamped": 0, "candidates": len(stamped), "applied": False,
                "skipped_no_ids": skipped_no_ids, "skipped_status": skipped_status,
                "skipped_unresolved": skipped_unresolved,
                "already_current": already_current, "ids": [s[0] for s in stamped]}

    pending = len(stamped)
    for nid, valid_until, _rec_id in stamped:
        conn.execute("UPDATE nodes SET valid_until=? WHERE id=?", (valid_until, nid))
    conn.commit()
    if verbose:
        print(f"[validity_pass:supersessions] stamped {len(stamped)} node(s) valid_until "
              f"from {path.name}")
    return {"stamped": pending + already_current, "updated": pending, "applied": True,
            "skipped_no_ids": skipped_no_ids, "skipped_status": skipped_status,
            "skipped_unresolved": skipped_unresolved,
            "already_current": already_current, "ids": [s[0] for s in stamped]}


# ── (b) contradictions.jsonl → valid_until on the invalidated id ──────────────

def stamp_from_contradictions(conn, path: Path, verbose: bool = True,
                              apply: bool = True) -> dict:
    records = _load_jsonl(path)
    stamped: list[tuple[str, str]] = []
    skipped_no_id = 0
    skipped_unresolved = 0
    skipped_status = 0
    already_current = 0

    for rec in records:
        status = (rec.get("status") or "").strip().lower()
        if status != "resolved":
            skipped_status += 1
            continue
        inval_ids = _as_ids(rec.get("invalidates_id"))
        if not inval_ids:
            skipped_no_id += 1
            continue
        valid_until = rec.get("resolved_at") or rec.get("created")
        if not valid_until:
            skipped_unresolved += 1
            continue
        found = _existing_ids(conn, inval_ids)
        if not found:
            skipped_unresolved += 1
            continue
        for nid in found:
            current = conn.execute(
                "SELECT valid_until FROM nodes WHERE id=?", (nid,)
            ).fetchone()[0]
            if current == valid_until:
                already_current += 1
                continue
            stamped.append((nid, valid_until))

    if not apply:
        if verbose:
            print(f"[validity_pass:contradictions] DRY RUN — would stamp {len(stamped)} "
                  f"node(s) (skipped: no_id={skipped_no_id} status={skipped_status} "
                  f"unresolved={skipped_unresolved})")
        return {"stamped": 0, "candidates": len(stamped), "applied": False,
                "skipped_no_id": skipped_no_id, "skipped_status": skipped_status,
                "skipped_unresolved": skipped_unresolved,
                "already_current": already_current, "ids": [s[0] for s in stamped]}

    pending = len(stamped)
    for nid, valid_until in stamped:
        conn.execute("UPDATE nodes SET valid_until=? WHERE id=?", (valid_until, nid))
    conn.commit()
    if verbose:
        print(f"[validity_pass:contradictions] stamped {len(stamped)} node(s) valid_until "
              f"from {path.name}")
    return {"stamped": pending + already_current, "updated": pending, "applied": True,
            "skipped_no_id": skipped_no_id, "skipped_status": skipped_status,
            "skipped_unresolved": skipped_unresolved,
            "already_current": already_current, "ids": [s[0] for s in stamped]}


# ── (c) automatic same-subject inference → PROPOSAL only, never applied ───────

def _normalize_subject(title: str) -> str:
    t = (title or "").strip().lower()
    t = _TRAILING_DATE.sub("", t)
    return t.strip()


def _days_apart(a: str | None, b: str | None) -> float | None:
    from ..brain import _parse_ts  # local import — brain imports config/db at module
                                    # scope, importing it eagerly here would be a cycle
                                    # risk if brain ever imports this module.
    da, db_ = _parse_ts(a), _parse_ts(b)
    if da is None or db_ is None:
        return None
    return abs((da - db_).total_seconds()) / 86400.0


def find_same_subject_candidates(conn, min_days_apart: float = 1.0) -> list[dict]:
    """Nodes that SHARE a normalized subject, are both live and un-stamped, and whose
    last_modified differs by more than `min_days_apart` days. This is an INFERENCE, not a
    fact — callers must stage it as a proposal, never write valid_until from it.

    Scoped to `family='document'` (prose knowledge, not source files) with a multi-word
    subject of meaningful length. Without this, a bare filename ("readme", "index",
    "main.js") collides across hundreds of unrelated code/automation nodes that merely
    share a common basename. Filtering reduces unrelated filename matches."""
    rows = conn.execute(
        "SELECT id, title, family, status, last_modified FROM nodes "
        "WHERE status='active' AND valid_until IS NULL AND title IS NOT NULL "
        "  AND family='document'"
    ).fetchall()
    by_subject: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        subj = _normalize_subject(r["title"])
        if not subj or " " not in subj or len(subj) < 12:
            continue
        by_subject.setdefault((r["family"], subj), []).append(dict(r))

    candidates = []
    for (family, subj), group in by_subject.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda n: n.get("last_modified") or "")
        older, newer = group[0], group[-1]
        if older["id"] == newer["id"]:
            continue
        gap = _days_apart(older.get("last_modified"), newer.get("last_modified"))
        if gap is None or gap < min_days_apart:
            continue
        candidates.append({
            "family": family, "subject": subj,
            "older_id": older["id"], "older_last_modified": older.get("last_modified"),
            "newer_id": newer["id"], "newer_last_modified": newer.get("last_modified"),
            "days_apart": round(gap, 2),
        })
    return candidates


def _write_proposals(candidates: list[dict], source: str = "validity_pass",
                     max_proposals: int = 300) -> Path | None:
    """Stage up to `max_proposals` candidates as ONE jsonl ticket, highest days_apart
    first (the widest gap is the strongest signal this is a real evolving fact rather than
    a same-day reorg/rename artifact). A live corpus with a heavy id-churn/reorg history
    (see content_dedup.py's own header — 167,958 duplicate-era nodes) can surface well
    over a thousand same-title matches; dumping all of them into one COVENANT-facing
    ticket is not a proposal queue anyone will read, so this caps what gets WRITTEN while
    `find_same_subject_candidates`'s return value (and therefore `run()`'s reported
    `candidates` count) stays the honest, uncapped total."""
    if not candidates:
        return None
    ranked = sorted(candidates, key=lambda c: -c["days_apart"])[:max_proposals]
    proposals = []
    for c in ranked:
        pid = f"prop_{short_hash(c['older_id'] + c['newer_id'] + 'temporal_validity')[:8]}"
        proposals.append({
            "proposal_id": pid,
            "kind": "temporal_validity_candidate",
            "older_id": c["older_id"],
            "newer_id": c["newer_id"],
            "subject": c["subject"],
            "family": c["family"],
            "evidence": {
                "older_last_modified": c["older_last_modified"],
                "newer_last_modified": c["newer_last_modified"],
                "days_apart": c["days_apart"],
            },
            "recommendation": (
                f"Same normalized subject '{c['subject']}' ({c['family']}), "
                f"{c['days_apart']} days apart. If {c['newer_id']} genuinely supersedes "
                f"{c['older_id']}, add an old_id/new_id supersession record to "
                f"index/supersessions.jsonl — this pass never applies the inference itself."
            ),
            "asserted_by": source,
            "asserted_at": now_iso(),
        })
    C.STAGING_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    identity = "|".join(p["proposal_id"] for p in proposals)
    h = short_hash(f"{source}:{ts}:{len(proposals)}:{identity}")[:8]
    path = C.STAGING_DIR / f"{ts}_{source}_{h}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for p in proposals:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    return path


# ── entry point ─────────────────────────────────────────────────────────────

def run(conn=None, apply: bool = True, propose: bool = True, verbose: bool = True) -> dict:
    owns_conn = conn is None
    conn = conn or DB.connect()
    try:
        DB.init_db(conn)  # idempotent — ensures valid_from/valid_until exist (migration)
        sup = stamp_from_supersessions(conn, C.GRAPH_DIR / "supersessions.jsonl",
                                       verbose=verbose, apply=apply)
        con = stamp_from_contradictions(conn, C.GRAPH_DIR / "contradictions.jsonl",
                                        verbose=verbose, apply=apply)
        result = {"supersessions": sup, "contradictions": con,
                  "total_stamped": sup.get("stamped", 0) + con.get("stamped", 0)}
        if propose:
            candidates = find_same_subject_candidates(conn)
            proposal_path = _write_proposals(candidates) if apply else None
            staged_n = min(len(candidates), 300)
            result["proposals"] = {
                "candidates": len(candidates),
                "staged": staged_n if proposal_path else 0,
                "staged_path": str(proposal_path) if proposal_path else None,
            }
            if verbose:
                if apply:
                    print(f"[validity_pass:proposals] {len(candidates)} same-subject "
                          f"candidate(s), staged {staged_n} → {proposal_path or '(none)'}")
                else:
                    print(f"[validity_pass:proposals] DRY RUN — {len(candidates)} "
                          f"same-subject candidate(s) would be proposed")
        return result
    finally:
        if owns_conn:
            conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="preview only, write nothing")
    ap.add_argument("--no-propose", action="store_true",
                    help="skip the same-subject inference / proposal step")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON only")
    args = ap.parse_args()
    result = run(apply=not args.dry_run, propose=not args.no_propose,
                 verbose=not args.json)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
