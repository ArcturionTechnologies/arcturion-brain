"""Domain/range (RDFS-style) endpoint-typing audit — READ ONLY.

Reports edges whose endpoint *families* contradict the domain/range table in
`engine.validator` (DOMAIN_RANGE / SAME_FAMILY_PREDICATES). Mirrors the
read-only/proposal-only posture of `engine.maintenance.gravity`: this module
never writes to `edges` or `nodes`, never repairs, never merges.

Why an audit rather than a write-time gate: `validate_triple` sits only on the
MCP-propose and .jsonl-hook paths, so it sees almost no real traffic, while the
~20 direct edge producers bypass validation entirely. One SQL sweep inspects
everything they have already written.

Coverage caveat, reported in every run: only ~19.4% of edges have BOTH
endpoints resolving to a real `nodes.id`. The rest dangle by design (bare
strings for calls/imports/owned_by) or are orphans of deleted nodes. The
`JOIN nodes` below IS the tri-state gate — unresolvable endpoints simply do
not join, so they are never reported as violations.

Usage:
    cd ~/Desktop/ARCTURION_Brain
    python3 -m engine.maintenance.domain_range_audit          # markdown
    python3 -m engine.maintenance.domain_range_audit --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from .. import config as C
from .. import validator as V

REPORT_FILENAME = "domain_range_audit.md"

# Both endpoints must join against nodes.id — that join is the tri-state gate.
_PAIR_SQL = """
SELECT e.predicate                        AS predicate,
       nf.family                          AS from_family,
       nt.family                          AS to_family,
       COALESCE(e.asserted_by, '(null)')  AS asserted_by,
       COUNT(*)                           AS n,
       MIN(e.from_id)                     AS example_from,
       MIN(e.to_id)                       AS example_to
FROM edges e
JOIN nodes nf ON nf.id = e.from_id
JOIN nodes nt ON nt.id = e.to_id
GROUP BY 1, 2, 3, 4
"""


def _connect_ro(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the index strictly read-only. Not DB.connect() — that opens RW."""
    p = db_path or C.BRAIN_DB
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute(f"PRAGMA mmap_size={C.SQLITE_MMAP_BYTES}")
    except (sqlite3.OperationalError, AttributeError):
        pass
    return conn


def classify_pair(predicate: str, from_family: str, to_family: str) -> list[str]:
    """Return the violation codes this family-pair triggers ([] when clean).

    Delegates the actual rule set to engine.validator so the table has exactly
    one home. Kept as a thin wrapper so the audit and the write-time check can
    never drift apart.
    """
    codes: list[str] = []
    rule = V.DOMAIN_RANGE.get(predicate)
    if rule:
        domain = rule.get("domain")
        rng = rule.get("range")
        if domain is not None and from_family not in domain:
            codes.append("DOMAIN_MISMATCH")
        if rng is not None and to_family not in rng:
            codes.append("RANGE_MISMATCH")
    if predicate in V.SAME_FAMILY_PREDICATES and from_family != to_family:
        codes.append("SAME_FAMILY_MISMATCH")
    return codes


def audit(db_path: Path | None = None) -> dict[str, Any]:
    conn = _connect_ro(db_path)
    try:
        total_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        total_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        rows = conn.execute(_PAIR_SQL).fetchall()
    finally:
        conn.close()

    resolvable = 0
    conforming = 0
    violations: list[dict[str, Any]] = []
    unmodeled: dict[str, dict[str, Any]] = {}
    modeled = set(V.DOMAIN_RANGE) | set(V.SAME_FAMILY_PREDICATES)

    for r in rows:
        n = r["n"]
        pred = r["predicate"]
        resolvable += n
        if pred not in modeled:
            slot = unmodeled.setdefault(
                pred, {"predicate": pred, "edges": 0, "_pairs": {}, "producers": set()}
            )
            slot["edges"] += n
            # Aggregate across asserted_by — the SQL groups by producer too, so
            # the same family-pair arrives once per producer and would otherwise
            # be listed twice in the report.
            key = f"{r['from_family']} -> {r['to_family']}"
            slot["_pairs"][key] = slot["_pairs"].get(key, 0) + n
            slot["producers"].add(r["asserted_by"])
            continue
        codes = classify_pair(pred, r["from_family"], r["to_family"])
        if not codes:
            conforming += n
            continue
        violations.append({
            "predicate": pred,
            "pair": f"{r['from_family']} -> {r['to_family']}",
            "from_family": r["from_family"],
            "to_family": r["to_family"],
            "asserted_by": r["asserted_by"],
            "codes": codes,
            "edges": n,
            "example_from": r["example_from"],
            "example_to": r["example_to"],
        })

    violations.sort(key=lambda v: (-v["edges"], v["predicate"], v["pair"]))
    for slot in unmodeled.values():
        slot["pairs"] = [
            {"pair": k, "n": v}
            for k, v in sorted(slot.pop("_pairs").items(), key=lambda kv: -kv[1])
        ]
        slot["producers"] = sorted(slot["producers"])
    unmodeled_list = sorted(unmodeled.values(), key=lambda u: -u["edges"])

    violating_edges = sum(v["edges"] for v in violations)
    unmodeled_edges = sum(u["edges"] for u in unmodeled_list)

    return {
        "total_edges": total_edges,
        "total_nodes": total_nodes,
        "resolvable_edges": resolvable,
        "coverage_pct": round(100.0 * resolvable / total_edges, 2) if total_edges else 0.0,
        "modeled_predicates": len(modeled),
        "conforming_edges": conforming,
        "violating_edges": violating_edges,
        "unmodeled_edges": unmodeled_edges,
        "violations": violations,
        "unmodeled": unmodeled_list,
    }


def to_markdown(report: dict[str, Any]) -> str:
    L: list[str] = []
    L.append("# Brain domain/range audit (read-only)\n")
    L.append(
        f"- Edges in graph: **{report['total_edges']:,}** · nodes: {report['total_nodes']:,}\n"
        f"- Inspectable (both endpoints resolve to a node): "
        f"**{report['resolvable_edges']:,} ({report['coverage_pct']}%)**\n"
        f"- Modeled predicates: {report['modeled_predicates']}\n"
        f"- Conforming: **{report['conforming_edges']:,}** · "
        f"violating: **{report['violating_edges']:,}** · "
        f"unmodeled: {report['unmodeled_edges']:,}\n"
    )
    L.append(
        "\n> ⚠️ Coverage is partial by design. The ~80% of edges not counted here have an\n"
        "> intentionally dangling endpoint (bare strings for `calls`/`imports`/`owned_by`)\n"
        "> or are orphans of deleted nodes. A low violation count does **not** mean the\n"
        "> whole graph is clean — only the inspectable slice was checked.\n"
    )

    L.append("\n## Violations\n")
    if not report["violations"]:
        L.append("\n_None in the inspectable slice._\n")
    else:
        L.append("\n| Edges | Predicate | Family pair | Codes | Asserted by (producer to fix) |\n")
        L.append("|---:|---|---|---|---|\n")
        for v in report["violations"]:
            L.append(
                f"| {v['edges']:,} | `{v['predicate']}` | {v['pair']} | "
                f"{', '.join(v['codes'])} | `{v['asserted_by']}` |\n"
            )
        L.append("\n### Examples\n")
        for v in report["violations"][:15]:
            L.append(
                f"- `{v['predicate']}` ({v['pair']}, {v['edges']:,} edges, "
                f"`{v['asserted_by']}`)\n"
                f"  - from: `{v['example_from']}`\n"
                f"  - to:   `{v['example_to']}`\n"
            )

    L.append("\n## Unmodeled predicates (evidence for growing the table)\n")
    if not report["unmodeled"]:
        L.append("\n_None._\n")
    else:
        L.append("\n| Predicate | Edges | Distinct family pairs | Top pairs |\n")
        L.append("|---|---:|---:|---|\n")
        for u in report["unmodeled"]:
            top = " · ".join(f"{p['pair']} ({p['n']:,})" for p in u["pairs"][:3])
            L.append(f"| `{u['predicate']}` | {u['edges']:,} | {len(u['pairs'])} | {top} |\n")
        L.append(
            "\n_A predicate with ONE dominant family pair is a candidate for the table;_\n"
            "_one spanning many pairs (`contains`, `part_of`, `semantically_similar_to`)_\n"
            "_is a structural catch-all and should stay unmodeled._\n"
        )
    return "".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Read-only domain/range endpoint-typing audit for the Brain graph"
    )
    ap.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    ap.add_argument("--no-write", action="store_true",
                    help="skip writing the markdown report to index/")
    args = ap.parse_args()

    report = audit()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    md = to_markdown(report)
    print(md, end="")
    if not args.no_write:
        out = C.GRAPH_DIR / REPORT_FILENAME
        out.write_text(md, encoding="utf-8")
        print(f"\n[wrote] {out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
