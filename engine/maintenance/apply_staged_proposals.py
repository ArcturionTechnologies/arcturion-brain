"""One-time applier for two Founder-gated staged proposal files (2026-08-08).

`staging/proposals/` accumulates proposals from maintenance passes that never apply
themselves (see `validity_pass.py`'s own docstring: "this pass NEVER applies the
inference itself"). Two files sat staged-not-applied per the retrieval-precision
continuation card:

  - `20260806T125204Z_validity_pass_a8eb51.jsonl` (300 `temporal_validity_candidate`
    records) — applied by writing each candidate as an `old_id`/`new_id` record into
    `index/supersessions.jsonl` (status "active", `activated` = the newer node's
    last_modified so the old fact stops being current exactly when the new one appeared)
    and then running `validity_pass.run()` — the EXISTING, ONLY writer of
    `nodes.valid_until`. Soft and reversible: a superseded node is tiered below live
    nodes (`config.TEMPORAL_VALIDITY_TIER`), never deleted, and un-stamping is a single
    `UPDATE nodes SET valid_until=NULL WHERE id IN (...)` (see `revert_validity_stamps`
    below) plus trimming the appended supersessions.jsonl lines.

  - `20260806T143346Z_taxonomy_conformance_b52d00.jsonl` (25 `taxonomy_conformance_divergence`
    records) — NOT a graph-node concern; these describe agent Memory/Knowledge shelf
    conformance against the taxonomy canon. Applying them is writing an
    acknowledgment/adoption ledger (`index/taxonomy_conformance.jsonl`, same jsonl-ticket
    convention as `supersessions.jsonl`/`contradictions.jsonl`) — NEVER a vault file
    move/rename/delete. Any record whose own recommendation is a migrate/rename/file-move,
    or that explicitly asks for a Founder/COVENANT call before acting, is SKIPPED and
    reported rather than applied — this module classifies those by proposal_id, not by
    fuzzy text parsing, so the classification is auditable.

Both appliers are idempotent: re-running skips proposal_ids already present in the
target ledger (matched by the `source_proposal_id` field this module stamps on every
record it writes).

Run:
    python3 -m engine.maintenance.apply_staged_proposals --validity <path> --taxonomy <path>
    python3 -m engine.maintenance.apply_staged_proposals --validity <path> --taxonomy <path> --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import config as C
from .. import db as DB
from ..util import now_iso
from . import validity_pass


# ── shared jsonl helpers ───────────────────────────────────────────────────────

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
            continue
    return out


def _append_jsonl(path: Path, records: list[dict]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _already_applied_proposal_ids(ledger_path: Path) -> set[str]:
    return {
        r.get("source_proposal_id")
        for r in _load_jsonl(ledger_path)
        if r.get("source_proposal_id")
    }


# ── (1) temporal_validity_candidate → index/supersessions.jsonl + valid_until ──

def apply_validity_pass_proposals(proposals_path: Path, apply: bool = True,
                                   verbose: bool = True) -> dict:
    records = _load_jsonl(proposals_path)
    supersessions_path = C.GRAPH_DIR / "supersessions.jsonl"
    already = _already_applied_proposal_ids(supersessions_path)

    new_entries = []
    skipped_not_candidate = 0
    skipped_missing_ids = 0
    skipped_already_applied = 0

    for rec in records:
        if rec.get("kind") != "temporal_validity_candidate":
            skipped_not_candidate += 1
            continue
        pid = rec.get("proposal_id")
        if pid in already:
            skipped_already_applied += 1
            continue
        older_id = rec.get("older_id")
        newer_id = rec.get("newer_id")
        if not older_id or not newer_id:
            skipped_missing_ids += 1
            continue
        evidence = rec.get("evidence") or {}
        activated = (evidence.get("newer_last_modified") or rec.get("asserted_at")
                     or now_iso())
        new_entries.append({
            "id": f"supersession_from_{pid}",
            "status": "active",
            "old_id": older_id,
            "new_id": newer_id,
            "old": f"{rec.get('subject', '')} (older: {older_id})",
            "new": f"{rec.get('subject', '')} (newer: {newer_id})",
            "summary": rec.get("recommendation", ""),
            "source": f"staging/proposals/{proposals_path.name}",
            "source_proposal_id": pid,
            "owner": "validity_pass_proposal_apply",
            "created": now_iso(),
            "activated": activated,
        })

    result = {
        "candidates": len(records),
        "written_to_supersessions": len(new_entries),
        "skipped_not_candidate": skipped_not_candidate,
        "skipped_missing_ids": skipped_missing_ids,
        "skipped_already_applied": skipped_already_applied,
    }

    if not apply:
        result["applied"] = False
        result["validity_pass"] = None
        if verbose:
            print(f"[apply_staged_proposals:validity] DRY RUN — would append "
                  f"{len(new_entries)} supersession record(s)")
        return result

    _append_jsonl(supersessions_path, new_entries)
    if verbose:
        print(f"[apply_staged_proposals:validity] appended {len(new_entries)} "
              f"supersession record(s) to {supersessions_path}")

    vp_result = validity_pass.run(apply=True, propose=False, verbose=verbose)
    result["applied"] = True
    result["validity_pass"] = vp_result
    return result


def revert_validity_stamps(node_ids: list[str], apply: bool = True) -> dict:
    """Reversal helper: clear valid_until on the given node ids (does NOT remove the
    appended supersessions.jsonl lines — trim those by source_proposal_id if a full
    revert is needed)."""
    if not node_ids:
        return {"cleared": 0, "applied": False}
    conn = DB.connect()
    try:
        if not apply:
            return {"cleared": len(node_ids), "applied": False}
        placeholders = ",".join("?" * len(node_ids))
        conn.execute(f"UPDATE nodes SET valid_until=NULL WHERE id IN ({placeholders})",
                     node_ids)
        conn.commit()
        return {"cleared": len(node_ids), "applied": True}
    finally:
        conn.close()


# ── (2) taxonomy_conformance_divergence → index/taxonomy_conformance.jsonl ─────

# Explicit, auditable classification by proposal_id — NOT fuzzy text parsing. A record
# is APPLY_IDS if its own recommendation implies zero vault action (adopt-as-canonical
# documentation, or "no action / conformant"). Everything else — migrate, rename,
# file-refiling, or "needs a Founder/COVENANT call" — is SKIP_IDS with a reason, because
# this pass performs index/metadata-side conformance only and never moves, renames, or
# deletes a vault file.
_TAXONOMY_APPLY_IDS = {
    "prop_9c6c68": "adopt-as-canonical (ALTAIR Projects/Feedback/Tools shelves, documented not migrated)",
    "prop_592ca6": "conformant — no action",
    "prop_9b2ce3": "minor_extra, immaterial — no action",
    "prop_d485e5": "minor_extra, no taxonomy action (janitor-sweep flag is informational only)",
    "prop_52dfed": "minor_extra, no taxonomy action (janitor-sweep flag is informational only)",
    "prop_966870": "conformant — no action",
    "prop_bbee76": "adopt-as-canonical (CASSIOPEIA brand-domain shelves)",
    "prop_297bea": "adopt-as-canonical (VEGA +10 trading-domain shelves)",
    "prop_2e32d4": "adopt-as-canonical (SIRIUS Platforms shelf)",
    "prop_f2e865": "adopt-as-canonical (IO navigation shelves)",
    "prop_94ff3a": "conformant — no action",
    "prop_76adbe": "conformant — no action",
    "prop_20d0df": "conformant — no action",
    "prop_e7d7f2": "conformant — no action",
    "prop_0e2f03": "conformant — no action",
    "prop_f37b6b": "conformant — no action",
    "prop_c63b43": "conformant — no action",
    "prop_e3c696": "not_agent_shaped — out of scope, no action",
}
_TAXONOMY_SKIP_IDS = {
    "prop_0d11f0": "recommendation is migrate (rename 'memory'->'Memory', create shelves) — vault file/folder operation, out of scope",
    "prop_56514c": "recommendation includes rename 'sessions'->'Sessions' — vault rename, out of scope",
    "prop_a4c994": "recommendation is migrate — vault file/folder operation, out of scope",
    "prop_798cc2": "recommendation explicitly needs a Founder/COVENANT call before any mechanical action",
    "prop_2c1527": "recommendation is to file loose files under Projects/Decisions — vault move, out of scope",
    "prop_4f1c13": "recommendation is to clarify intent with COVENANT before acting (adopt vs migrate/merge undecided)",
    "prop_08a436": "recommendation is to adopt map files as a sub-shelf, implying moving loose-at-root files — vault move, out of scope",
}


def apply_taxonomy_conformance_proposals(proposals_path: Path, apply: bool = True,
                                          verbose: bool = True) -> dict:
    records = _load_jsonl(proposals_path)
    ledger_path = C.GRAPH_DIR / "taxonomy_conformance.jsonl"
    already = _already_applied_proposal_ids(ledger_path)

    new_entries = []
    skipped = []
    unclassified = []

    for rec in records:
        if rec.get("kind") != "taxonomy_conformance_divergence":
            continue
        pid = rec.get("proposal_id")
        if pid in already:
            continue
        if pid in _TAXONOMY_APPLY_IDS:
            new_entries.append({
                "id": f"taxonomy_conformance_from_{pid}",
                "status": "applied",
                "decision": "adopted-or-acknowledged",
                "agent": rec.get("agent"),
                "area": rec.get("area"),
                "divergence_type": rec.get("divergence_type"),
                "reason": _TAXONOMY_APPLY_IDS[pid],
                "original_recommendation": rec.get("recommendation", ""),
                "source": f"staging/proposals/{proposals_path.name}",
                "source_proposal_id": pid,
                "owner": "taxonomy_conformance_proposal_apply",
                "created": now_iso(),
            })
        elif pid in _TAXONOMY_SKIP_IDS:
            skipped.append({"proposal_id": pid, "agent": rec.get("agent"),
                            "reason": _TAXONOMY_SKIP_IDS[pid]})
        else:
            unclassified.append(pid)

    result = {
        "candidates": len(records),
        "to_apply": len(new_entries),
        "to_skip": len(skipped),
        "skipped": skipped,
        "unclassified": unclassified,
    }

    if not apply:
        result["applied"] = False
        if verbose:
            print(f"[apply_staged_proposals:taxonomy] DRY RUN — would append "
                  f"{len(new_entries)} ledger record(s), skip {len(skipped)}")
        return result

    _append_jsonl(ledger_path, new_entries)
    result["applied"] = True
    if verbose:
        print(f"[apply_staged_proposals:taxonomy] appended {len(new_entries)} "
              f"ledger record(s) to {ledger_path}, skipped {len(skipped)}")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--validity", type=Path, default=None)
    ap.add_argument("--taxonomy", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    result = {}
    if args.validity:
        result["validity"] = apply_validity_pass_proposals(args.validity,
                                                            apply=not args.dry_run)
    if args.taxonomy:
        result["taxonomy"] = apply_taxonomy_conformance_proposals(args.taxonomy,
                                                                   apply=not args.dry_run)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
