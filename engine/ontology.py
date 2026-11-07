"""Machine-readable ontology loader — the Brain's vocabulary and its axioms.

`canon/0007_ONTOLOGY.yaml` is the single source of truth for:
  - the closed family vocabulary
  - the closed node-type vocabulary (each type mapped to one family)
  - every predicate in the canon, with its inverse / domain / range /
    transitivity / functionality
  - the retired-predicate set
  - disjointness axioms (currently empty by design — see the yaml header)

`engine/validator.py`'s `PREDICATE_CANON` must stay exactly equal to here; parity
is test-enforced (`engine/tests/test_ontology.py`), not derived; the type reasoner
and the transitive-closure traversal engine read the axioms through this API.

Public API:
  ONTOLOGY_PATH             — Path to canon/0007_ONTOLOGY.yaml
  OntologyError             — raised on any malformed / unloadable ontology
  load(path=None)           — parsed + structurally validated + cached
  node_types() / families() / predicates() / retired_predicates()
  family_of(node_type)
  domain_of(predicate) / range_of(predicate)   — empty frozenset = unconstrained
  inverse_of(predicate)
  is_transitive(p) / is_functional(p)
  transitive_predicates() / functional_predicates()
  disjoint_sets() / are_disjoint(a, b)

CLI:
  python3 -m engine.ontology --audit    # read-only live-graph conformance report
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from . import config as C

ONTOLOGY_PATH: Path = C.CANON_DIR / "0007_ONTOLOGY.yaml"

_REQUIRED_KEYS = ("version", "families", "node_types", "predicates",
                  "retired_predicates", "disjoint")

# Path-keyed so switching ONTOLOGY_PATH (tests, alternate canon roots) reloads
# cleanly while `load() is load()` still holds for any given path.
_CACHE: dict[Path, dict] = {}


class OntologyError(RuntimeError):
    """The ontology file is missing, unparseable, or structurally invalid."""


# ── Loading + structural validation ────────────────────────────────────────

def _fail(path: Path, msg: str) -> None:
    raise OntologyError(f"{path}: {msg}")


def _validate(doc: dict, path: Path) -> None:
    for k in _REQUIRED_KEYS:
        if k not in doc:
            _fail(path, f"missing required top-level key {k!r}")

    families = doc["families"]
    if not isinstance(families, list) or not all(isinstance(f, str) for f in families):
        _fail(path, "`families` must be a list of strings")
    fam_set = set(families)

    node_types = doc["node_types"]
    if not isinstance(node_types, dict):
        _fail(path, "`node_types` must be a mapping")
    for t, spec in node_types.items():
        if not isinstance(spec, dict):
            _fail(path, f"node_types[{t!r}] must be a mapping")
        fam = spec.get("family")
        if not isinstance(fam, str):
            _fail(path, f"node_types[{t!r}] has no string `family`")
        if fam not in fam_set:
            _fail(path, f"node_types[{t!r}].family={fam!r} is not in `families`")

    preds = doc["predicates"]
    if not isinstance(preds, dict):
        _fail(path, "`predicates` must be a mapping")
    for p, spec in preds.items():
        if spec is None:
            spec = {}
        if not isinstance(spec, dict):
            _fail(path, f"predicates[{p!r}] must be a mapping or null")
        for key in ("domain", "range"):
            val = spec.get(key)
            if val is None:
                continue
            if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                _fail(path, f"predicates[{p!r}].{key} must be a list of strings")
            for x in val:
                if x not in fam_set and x not in node_types:
                    _fail(path, f"predicates[{p!r}].{key} references unknown label {x!r}")
        for key in ("transitive", "functional", "undocumented"):
            if key in spec and not isinstance(spec[key], bool):
                _fail(path, f"predicates[{p!r}].{key} must be a boolean")

    # Inverse: must resolve to a declared predicate, and must be symmetric.
    for p, spec in preds.items():
        inv = (spec or {}).get("inverse")
        if inv is None:
            continue
        if not isinstance(inv, str):
            _fail(path, f"predicates[{p!r}].inverse must be a string")
        if inv not in preds:
            _fail(path, f"predicates[{p!r}].inverse={inv!r} is not a declared predicate")
        back = (preds[inv] or {}).get("inverse")
        if back != p:
            _fail(path, f"asymmetric inverse: {p!r} -> {inv!r} but {inv!r} -> {back!r}")

    retired = doc["retired_predicates"]
    if not isinstance(retired, list) or not all(isinstance(r, str) for r in retired):
        _fail(path, "`retired_predicates` must be a list of strings")
    for r in retired:
        if r not in preds:
            _fail(path, f"retired predicate {r!r} is not declared in `predicates`")

    disjoint = doc["disjoint"]
    if disjoint is None:
        disjoint = []
    if not isinstance(disjoint, list):
        _fail(path, "`disjoint` must be a list of lists")
    for i, grp in enumerate(disjoint):
        if not isinstance(grp, list) or len(grp) < 2 or not all(isinstance(x, str) for x in grp):
            _fail(path, f"disjoint[{i}] must be a list of 2+ strings")
        for x in grp:
            if x not in fam_set and x not in node_types:
                _fail(path, f"disjoint[{i}] references unknown label {x!r}")


def load(path: Path | None = None) -> dict:
    """Parse, structurally validate, and cache the ontology.

    Raises OntologyError on anything malformed. Never returns a partial ontology.
    """
    p = Path(path) if path is not None else ONTOLOGY_PATH
    cached = _CACHE.get(p)
    if cached is not None:
        return cached
    try:
        import yaml
    except ImportError as e:  # pragma: no cover - yaml ships with the brain
        raise OntologyError(f"PyYAML unavailable, cannot load ontology: {e}") from e
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as e:
        raise OntologyError(f"{p}: cannot read ontology: {e}") from e
    try:
        doc = yaml.safe_load(raw)
    except Exception as e:
        raise OntologyError(f"{p}: YAML parse error: {e}") from e
    if not isinstance(doc, dict):
        raise OntologyError(f"{p}: top-level document must be a mapping, got {type(doc).__name__}")
    _validate(doc, p)
    _CACHE[p] = doc
    return doc


# ── Accessors ──────────────────────────────────────────────────────────────

def _pred(predicate: str) -> dict:
    spec = load()["predicates"].get(predicate)
    return spec or {}


def node_types() -> frozenset[str]:
    return frozenset(load()["node_types"])


def families() -> frozenset[str]:
    return frozenset(load()["families"])


def predicates() -> frozenset[str]:
    return frozenset(load()["predicates"])


def desk_vocabulary(scope: str, pack: dict | None = None) -> dict:
    """Return a validated, versioned desk vocabulary without global admission."""
    if scope != "VEGA":
        raise ValueError("unknown desk scope")
    if pack is None:
        import json
        from .vega_index import PACK_PATH
        pack = json.loads(PACK_PATH.read_text(encoding="utf-8"))
    from .validator import validate_desk_pack
    concepts, predicates = validate_desk_pack(scope, pack)
    return {"scope": scope, "version": pack["version"], "concepts": concepts, "predicates": predicates}


def retired_predicates() -> frozenset[str]:
    return frozenset(load()["retired_predicates"])


def family_of(node_type: str) -> str | None:
    spec = load()["node_types"].get(node_type)
    return spec.get("family") if spec else None


def domain_of(predicate: str) -> frozenset[str]:
    return frozenset(_pred(predicate).get("domain") or ())


def range_of(predicate: str) -> frozenset[str]:
    return frozenset(_pred(predicate).get("range") or ())


def inverse_of(predicate: str) -> str | None:
    return _pred(predicate).get("inverse")


def is_transitive(predicate: str) -> bool:
    return bool(_pred(predicate).get("transitive"))


def is_functional(predicate: str) -> bool:
    return bool(_pred(predicate).get("functional"))


def transitive_predicates() -> frozenset[str]:
    return frozenset(p for p in load()["predicates"] if is_transitive(p))


def functional_predicates() -> frozenset[str]:
    return frozenset(p for p in load()["predicates"] if is_functional(p))


def disjoint_sets() -> tuple[frozenset[str], ...]:
    return tuple(frozenset(g) for g in (load()["disjoint"] or ()))


def are_disjoint(a: str, b: str) -> bool:
    """True when a and b are distinct labels sharing a declared disjoint set."""
    if a == b:
        return False
    return any(a in grp and b in grp for grp in disjoint_sets())


# ── CLI: live-graph conformance audit (READ-ONLY) ──────────────────────────

# `prop_*` ids are the propose→approve STAGING namespace (engine/mcp_server.py's
# brain_propose_node / brain_propose_edge), not canon/0005 node types. A proposal's
# `type` is free-form and lowercase by convention ("decision", "event", "concept",
# "skill", "capability"), and it is governed by the approval gate, not by the node
# taxonomy. Auditing it against the ontology reported 7 live, status=active staged
# proposals — one of them a standing Founder directive — as graph corruption, which
# is how a cleanup nearly deleted them (2026-08-01). Same narrowing in spirit as
# restricting ONT_UNKNOWN_NODE_TYPE from `type` to `node_type`: check the field the
# ontology actually governs, over the population it actually governs.
_PROPOSAL_ID_LIKE = r"prop\_%"


def audit(db_path: Path | None = None) -> dict:
    """Report live-graph node-type conformance. Opens the DB read-only; never writes.

    Staged proposals (`prop_*` ids) are counted in `nodes_total` but excluded from
    conformance — see `_PROPOSAL_ID_LIKE`.
    """
    db = Path(db_path) if db_path is not None else C.BRAIN_DB
    known = node_types()
    documented = {t for t, s in load()["node_types"].items() if not s.get("undocumented")}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT type,"
            "       SUM(CASE WHEN id LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END),"
            "       COUNT(*)"
            "  FROM nodes GROUP BY type",
            (_PROPOSAL_ID_LIKE,),
        ).fetchall()
    finally:
        conn.close()
    unknown: dict[str, int] = {}
    undocumented: dict[str, int] = {}
    total = 0
    proposals = 0
    for t, n_prop, n in rows:
        total += n
        proposals += n_prop
        governed = n - n_prop          # proposals answer to the approval gate, not canon/0005
        if not governed:
            continue
        label = t if t is not None else "<null>"
        if label not in known:
            unknown[label] = unknown.get(label, 0) + governed
        elif label not in documented:
            undocumented[label] = undocumented.get(label, 0) + governed
    return {
        "db": str(db),
        "nodes_total": total,
        "distinct_types": len(rows),
        "proposal_nodes_excluded": proposals,
        "unknown_types": dict(sorted(unknown.items(), key=lambda kv: -kv[1])),
        "unknown_nodes": sum(unknown.values()),
        "undocumented_types": dict(sorted(undocumented.items(), key=lambda kv: -kv[1])),
        "undocumented_nodes": sum(undocumented.values()),
    }


def _print_audit(a: dict) -> None:
    print(f"[ontology] live-graph conformance — {a['db']} (read-only)")
    print(f"  nodes: {a['nodes_total']:,}   distinct types: {a['distinct_types']}   "
          f"ontology types: {len(node_types())}   families: {len(families())}   "
          f"predicates: {len(predicates())}")
    print(f"  excluded from conformance: {a['proposal_nodes_excluded']:,} staged "
          f"proposal(s) (prop_* — governed by the approval gate, not canon/0005)")
    print(f"\n  UNKNOWN types (not in the ontology — defects): "
          f"{a['unknown_nodes']} node(s) across {len(a['unknown_types'])} type(s)")
    for t, n in a["unknown_types"].items():
        print(f"    {n:>6}  {t}")
    if not a["unknown_types"]:
        print("    (none)")
    print(f"\n  UNDOCUMENTED types (in the ontology, absent from canon/0005 — drift): "
          f"{a['undocumented_nodes']} node(s) across {len(a['undocumented_types'])} type(s)")
    for t, n in a["undocumented_types"].items():
        print(f"    {n:>6}  {t}")
    if not a["undocumented_types"]:
        print("    (none)")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Brain ontology loader + conformance audit")
    ap.add_argument("--audit", action="store_true", help="live-graph node-type conformance (read-only)")
    ap.add_argument("--json", action="store_true", help="emit the audit as JSON")
    args = ap.parse_args()
    if args.audit:
        summary = audit()
        if args.json:
            import json
            print(json.dumps(summary, indent=2))
        else:
            _print_audit(summary)
        sys.exit(0)
    ap.print_help()
