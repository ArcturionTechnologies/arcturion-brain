"""Predicate vocab audit (Item 16).

Reports zero-instance predicates from the canon. Operators decide which to
retire (formally add to RETIRED_PREDICATES in `brain.validator`) and which
to keep ("needs use case — kept for downstream").

Run:
    python3 -m engine.maintenance.predicate_audit
"""
from __future__ import annotations

import argparse
import json
import sys

from .. import db as DB
from .. import validator as V
from . import gravity


def audit() -> dict:
    conn = DB.connect()
    in_use = {
        r["predicate"]: r["c"]
        for r in conn.execute(
            "SELECT predicate, COUNT(*) c FROM edges GROUP BY predicate"
        ).fetchall()
    }
    conn.close()

    canon = set(V.PREDICATE_CANON)
    retired = set(getattr(V, "RETIRED_PREDICATES", set()))
    canon_active = canon - retired

    used = {p: c for p, c in in_use.items() if p in canon_active}
    unused_in_canon = sorted(p for p in canon_active if in_use.get(p, 0) == 0)
    used_outside_canon = sorted(p for p in in_use if p not in canon)
    used_retired = sorted(p for p in in_use if p in retired)
    gravity_counts = {p: in_use.get(p, 0) for p in gravity.GRAVITY_PREDICATES}

    return {
        "canon_size": len(canon),
        "retired_count": len(retired),
        "active_canon_size": len(canon_active),
        "predicates_in_use": len(used),
        "unused_in_active_canon": unused_in_canon,
        "used_outside_canon": used_outside_canon,
        "used_retired_predicates": used_retired,
        "top_predicates": sorted(used.items(), key=lambda x: -x[1])[:15],
        "gravity_predicates": {
            "counts": gravity_counts,
            "high_value_unused": sorted(p for p, c in gravity_counts.items() if c == 0),
            "candidate_for_retirement_review": sorted(
                p for p in unused_in_canon if p not in gravity.GRAVITY_PREDICATES
            ),
            "candidate_only": gravity.candidate_predicates(),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markdown", action="store_true",
                    help="emit markdown report instead of JSON")
    args = ap.parse_args()
    result = audit()
    if not args.markdown:
        print(json.dumps(result, indent=2))
        return 0
    print(f"# Predicate Vocab Audit")
    print()
    print(f"- Canon size: **{result['canon_size']}**")
    print(f"- Retired: **{result['retired_count']}**")
    print(f"- Active canon size: **{result['active_canon_size']}**")
    print(f"- Predicates currently in use (active canon): **{result['predicates_in_use']}**")
    print(f"- Unused in active canon: **{len(result['unused_in_active_canon'])}**")
    print()
    print("## Unused predicates (active canon)")
    for p in result["unused_in_active_canon"]:
        print(f"- `{p}`")
    print()
    print("## Brain Gravity predicates")
    gp = result["gravity_predicates"]
    for p, c in gp["counts"].items():
        marker = "high-value unused" if p in gp["high_value_unused"] else "in use"
        print(f"- `{p}`: **{c}** ({marker})")
    print()
    print("## Candidate-only predicates (not emitted)")
    for item in gp["candidate_only"]:
        inverse = f" / `{item['inverse']}`" if item.get("inverse") else ""
        print(f"- `{item['predicate']}`{inverse}: {item['rationale']}")
    print()
    print("## Unused non-gravity predicates (retirement review candidates)")
    for p in gp["candidate_for_retirement_review"]:
        print(f"- `{p}`")
    print()
    if result["used_outside_canon"]:
        print("## ⚠️ Predicates outside canon (must be removed or retired)")
        for p in result["used_outside_canon"]:
            print(f"- `{p}`")
        print()
    if result["used_retired_predicates"]:
        print("## ⚠️ Retired predicates still in use (drift)")
        for p in result["used_retired_predicates"]:
            print(f"- `{p}`")
    return 0


if __name__ == "__main__":
    sys.exit(main())
