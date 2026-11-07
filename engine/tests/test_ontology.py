"""Hermetic tests for the machine-readable ontology (Task 1). No pytest, no production DB.

Run:  python3 -m engine.tests.test_ontology
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

from .. import ontology as ONT
from .. import validator as V


# ── Fixture helpers ────────────────────────────────────────────────────────

_FIXTURE_OK = """
version: 1
families: [alpha, beta]
node_types:
  Widget: {family: alpha}
  Gadget: {family: beta}
  Doohickey: {family: beta}
predicates:
  part_of:
    inverse: contains
    transitive: true
  contains:
    inverse: part_of
    transitive: true
  owned_by:
    functional: true
    domain: [alpha]
    range: [beta]
  related_to:
    inverse: related_to
  old_verb: {}
retired_predicates: [old_verb]
disjoint:
  - [alpha, beta]
  - [Widget, Gadget]
  - [Widget, beta]
"""


def _write(tmp: Path, name: str, text: str) -> Path:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return p


def _using(path: Path):
    """Point the module-level ONTOLOGY_PATH at a fixture (cache is path-keyed)."""
    class _Ctx:
        def __enter__(self):
            self.orig = ONT.ONTOLOGY_PATH
            ONT.ONTOLOGY_PATH = path
            return path

        def __exit__(self, *exc):
            ONT.ONTOLOGY_PATH = self.orig
            return False
    return _Ctx()


# ── ONT_* capability probe (C1, whole-branch review fix wave) ──────────────
# engine/validator.py is a SHARED, uncommitted-in-places file: ALTAIR owns
# unrelated in-flight work in it (DOMAIN_RANGE/SAME_FAMILY_PREDICATES), and per
# a standing Founder-level constraint this suite may never edit that file to
# force a fix. The ONT_UNKNOWN_NODE_TYPE / ONT_DISJOINT_VIOLATION /
# ONT_FUNCTIONAL_VIOLATION checks — and the private `_ontology()` hook they
# depend on — may or may not be committed at any given moment. Detect ACTUAL
# presence (a capability probe), never a version guess or commit-hash check,
# so this suite tells the truth about whatever tree it happens to run
# against. Ledger: .superpowers/sdd/neurosymbolic-ontology/progress.md,
# Card 1(b) — re-tighten (delete this probe + the three guards below) once
# validator.py lands with these checks committed.
_ONT_SKIPPED_CASES = (
    "case_7_validator_unknown_node_type",
    "case_7b_validator_functional_and_disjoint",
    "case_10_validator_loads_without_package_context",
)


def _ont_checks_present() -> bool:
    """True only if _ontology() exists AND all three ONT_* codes are in the
    loaded validator module's own source — i.e. the checks are really there,
    not merely a same-named stub."""
    if not hasattr(V, "_ontology"):
        return False
    try:
        src = Path(V.__file__).read_text(encoding="utf-8")
    except Exception:
        return False
    return all(code in src for code in (
        "ONT_UNKNOWN_NODE_TYPE",
        "ONT_DISJOINT_VIOLATION",
        "ONT_FUNCTIONAL_VIOLATION",
    ))


_ONT_PRESENT = _ont_checks_present()
_ONT_SKIP_REASON = (
    "engine/validator.py does not (yet) contain the ONT_* checks / _ontology() "
    "hook in this tree — see .superpowers/sdd/neurosymbolic-ontology/progress.md, "
    "Card 1(b)"
)


def _print_skip_banner() -> None:
    bar = "!" * 78
    print(
        "\n" + bar +
        "\nSKIPPED (degraded run) — ONT_* validator checks are ABSENT from this tree\n" +
        "Cases NOT run: " + ", ".join(_ONT_SKIPPED_CASES) + "\n" +
        "Reason: " + _ONT_SKIP_REASON + "\n" +
        bar + "\n",
        file=sys.stderr,
    )


# ── Cases ──────────────────────────────────────────────────────────────────

def case_1_loads_and_caches() -> None:
    doc = ONT.load()
    assert isinstance(doc, dict), "load() must return a dict"
    assert doc.get("version") == 1, f"unexpected version: {doc.get('version')!r}"
    assert ONT.load() is doc, "load() must be cached (same object on second call)"
    assert ONT.node_types(), "node_types() must be non-empty"
    assert ONT.families(), "families() must be non-empty"
    assert ONT.predicates(), "predicates() must be non-empty"


def case_2_predicate_parity() -> None:
    """The ontology and the validator literal must never drift apart.

    `PREDICATE_CANON` stays a literal in validator.py — that file must import with
    no package context (see case 10) — so this assertion, not a shared constant, is
    what keeps the two in step. Editing either without the other fails here.
    """
    assert ONT.predicates() == V.PREDICATE_CANON, (
        "ontology predicates diverge from validator.PREDICATE_CANON: "
        f"only-in-yaml={sorted(ONT.predicates() - V.PREDICATE_CANON)} "
        f"only-in-validator={sorted(V.PREDICATE_CANON - ONT.predicates())}"
    )
    assert len(ONT.predicates()) == 82, f"expected 82 predicates, got {len(ONT.predicates())}"
    assert ONT.retired_predicates() == V.RETIRED_PREDICATES, (
        "ontology retired_predicates diverge from validator.RETIRED_PREDICATES: "
        f"{sorted(ONT.retired_predicates() ^ V.RETIRED_PREDICATES)}"
    )
    assert ONT.retired_predicates() <= ONT.predicates(), "retired predicates must be declared"


def case_3_inverse_symmetry() -> None:
    checked = 0
    for p in sorted(ONT.predicates()):
        inv = ONT.inverse_of(p)
        if inv is None:
            continue
        assert inv in ONT.predicates(), f"{p}.inverse={inv!r} is not a declared predicate"
        assert ONT.inverse_of(inv) == p, f"asymmetric inverse: {p} -> {inv} -> {ONT.inverse_of(inv)}"
        checked += 1
    assert checked >= 50, f"expected the bulk of the canon to declare inverses, got {checked}"


def case_4_family_integrity() -> None:
    fams = ONT.families()
    for t in sorted(ONT.node_types()):
        f = ONT.family_of(t)
        assert f is not None, f"node type {t!r} has no family"
        assert f in fams, f"node type {t!r} maps to undeclared family {f!r}"
    assert ONT.family_of("no_such_type_xyz") is None


def case_5_transitivity() -> None:
    assert ONT.is_transitive("part_of") is True
    assert ONT.is_transitive("calls") is False
    assert "part_of" in ONT.transitive_predicates()
    assert "calls" not in ONT.transitive_predicates()
    # Transitivity must be inverse-consistent: if X is transitive so is its inverse.
    for p in ONT.transitive_predicates():
        inv = ONT.inverse_of(p)
        if inv:
            assert ONT.is_transitive(inv), f"{p} is transitive but its inverse {inv} is not"


def case_6_disjointness(tmp: Path) -> None:
    # Real ontology asserts no disjointness (Founder governance decision, out of scope)
    # and no domain/range (that layer is validator.DOMAIN_RANGE, per canon/0006).
    assert ONT.disjoint_sets() == (), "the shipped ontology must assert no disjoint sets"
    assert ONT.are_disjoint("document", "code") is False
    assert ONT.domain_of("owned_by") == frozenset(), "endpoint typing is not this layer"
    assert ONT.range_of("owned_by") == frozenset(), "endpoint typing is not this layer"

    fx = _write(tmp, "fixture_ok.yaml", _FIXTURE_OK)
    with _using(fx):
        assert ONT.disjoint_sets() == (frozenset({"alpha", "beta"}),
                                       frozenset({"Widget", "Gadget"}),
                                       frozenset({"Widget", "beta"}))
        assert ONT.are_disjoint("alpha", "beta") is True
        assert ONT.are_disjoint("beta", "alpha") is True
        assert ONT.are_disjoint("Widget", "Gadget") is True
        assert ONT.are_disjoint("Widget", "Doohickey") is False   # not in a shared set
        assert ONT.are_disjoint("alpha", "Widget") is False       # different sets
        assert ONT.are_disjoint("alpha", "alpha") is False        # a label is not disjoint from itself
        assert ONT.are_disjoint("nope", "alpha") is False
        # the functional + domain/range machinery works even though the real
        # ontology asserts none of it
        assert ONT.is_functional("owned_by") is True
        assert ONT.functional_predicates() == frozenset({"owned_by"})
        assert ONT.domain_of("owned_by") == frozenset({"alpha"})
        assert ONT.range_of("owned_by") == frozenset({"beta"})
        assert ONT.domain_of("part_of") == frozenset()  # unconstrained
    # real ontology asserts no functional predicates
    assert ONT.functional_predicates() == frozenset()


def case_7_validator_unknown_node_type(tmp: Path) -> None:
    if not _ONT_PRESENT:
        print(f"  skip case_7_validator_unknown_node_type — {_ONT_SKIP_REASON}", file=sys.stderr)
        return
    good = _write(tmp, "good.md", "---\nnode_type: Decision\nowner: COVENANT\ncreated: 2026-07-31\n---\n\nbody\n")
    bad = _write(tmp, "bad.md", "---\nnode_type: Doohickey\nowner: COVENANT\ncreated: 2026-07-31\n---\n\nbody\n")

    codes_good = {v.code for v in V.validate_node(good)}
    assert not any(c.startswith("ONT_") for c in codes_good), f"valid node emitted {codes_good}"

    viols = [v for v in V.validate_node(bad) if v.code == "ONT_UNKNOWN_NODE_TYPE"]
    assert len(viols) == 1, f"expected 1 ONT_UNKNOWN_NODE_TYPE, got {len(viols)}"
    assert viols[0].severity == "warn", f"must be warn, not {viols[0].severity!r}"
    assert "Doohickey" in viols[0].message
    # never an error — the PreToolUse hook blocks on errors
    assert not any(v.severity == "error" for v in V.validate_node(bad))


def case_7b_validator_functional_and_disjoint(tmp: Path) -> None:
    """The two axiom-driven checks, exercised against a fixture ontology.

    Neither can fire against the shipped ontology (both axiom sets are empty by
    design), so without a fixture they would ship completely unexercised.
    """
    if not _ONT_PRESENT:
        print(f"  skip case_7b_validator_functional_and_disjoint — {_ONT_SKIP_REASON}", file=sys.stderr)
        return
    fx = _write(tmp, "fixture_ok.yaml", _FIXTURE_OK)
    with _using(fx):
        # functional: a second distinct object for the same subject warns
        jl = _write(tmp, "edges.jsonl", "\n".join([
            '{"from":"a","to":"b","verb":"owned_by"}',
            '{"from":"a","to":"b","verb":"owned_by"}',
            '{"from":"a","to":"c","verb":"owned_by"}',
            '{"from":"z","to":"b","verb":"owned_by"}',
            '{"from":"a","to":"d","verb":"part_of"}',
            '{"from":"a","to":"e","verb":"part_of"}',
        ]) + "\n")
        fv = [v for v in V.validate_jsonl(jl) if v.code == "ONT_FUNCTIONAL_VIOLATION"]
        assert len(fv) == 1, f"expected exactly 1 functional violation, got {[v.message for v in fv]}"
        assert fv[0].severity == "warn"
        assert fv[0].line == 3, f"violation should point at line 3, got {fv[0].line}"

        # disjoint: a node whose declared node_type and family are mutually
        # exclusive warns. Widget's own family is alpha, and [Widget, beta] is a
        # declared disjoint set, so claiming family=beta contradicts the type.
        dn = _write(tmp, "disjoint.md", "---\nnode_type: Widget\nfamily: beta\nowner: X\ncreated: 2026-07-31\n---\n")
        dv = [v for v in V.validate_node(dn) if v.code == "ONT_DISJOINT_VIOLATION"]
        assert len(dv) == 1, f"expected 1 disjoint violation, got {[v.message for v in dv]}"
        assert dv[0].severity == "warn"

        ok = _write(tmp, "nodisjoint.md", "---\nnode_type: Widget\nfamily: alpha\nowner: X\ncreated: 2026-07-31\n---\n")
        assert not [v for v in V.validate_node(ok) if v.code == "ONT_DISJOINT_VIOLATION"]

        # frontmatter `type` is a different namespace and must NOT be checked
        ty = _write(tmp, "typekey.md", "---\ntype: session-log\nowner: X\ncreated: 2026-07-31\n---\n")
        assert not [v for v in V.validate_node(ty) if v.code.startswith("ONT_")], \
            "frontmatter `type` must not be read as an ontology node type"


def case_8_malformed_raises(tmp: Path) -> None:
    cases = {
        "missing.yaml": None,  # file never written
        "notyaml.yaml": "::: not : valid : yaml\n  - [unclosed\n",
        "notadict.yaml": "- just\n- a\n- list\n",
        "nokeys.yaml": "version: 1\n",
        "badfamily.yaml": "version: 1\nfamilies: [alpha]\nnode_types:\n  W: {family: nope}\npredicates: {}\nretired_predicates: []\ndisjoint: []\n",
        "badinverse.yaml": "version: 1\nfamilies: [alpha]\nnode_types: {}\npredicates:\n  a: {inverse: b}\n  b: {inverse: a}\n  c: {inverse: a}\nretired_predicates: []\ndisjoint: []\n",
        "danglinginverse.yaml": "version: 1\nfamilies: [alpha]\nnode_types: {}\npredicates:\n  a: {inverse: zzz}\nretired_predicates: []\ndisjoint: []\n",
        "baddisjoint.yaml": "version: 1\nfamilies: [alpha]\nnode_types: {}\npredicates: {}\nretired_predicates: []\ndisjoint:\n  - [alpha, ghost]\n",
    }
    for name, text in cases.items():
        p = tmp / name
        if text is not None:
            p.write_text(text, encoding="utf-8")
        try:
            ONT.load(p)
        except ONT.OntologyError:
            continue
        raise AssertionError(f"{name}: expected OntologyError, load() succeeded")


def case_9_audit_is_read_only(tmp: Path) -> None:
    """--audit reports conformance against a throwaway DB and never writes to it."""
    import sqlite3
    db = tmp / "audit.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT)")
    conn.executemany("INSERT INTO nodes VALUES (?, ?)", [
        ("n1", "Decision"),       # documented
        ("n2", "Source"),         # documented
        ("n3", "Approval_gate"),  # in the ontology, undocumented in canon/0005
        ("n4", "decision"),       # excluded case-variant -> defect
        ("n5", "<agent|founder>"),  # excluded placeholder -> defect
        # Staged proposals: the prop_* namespace answers to the approval gate, not
        # canon/0005. Its lowercase types are proposal vocabulary, NOT corruption.
        # prop_x shares n4's type and prop_y an otherwise-unseen one, so a regression
        # that stops excluding them shows up as a count AND as a new unknown type.
        ("prop_x", "decision"),
        ("prop_y", "concept"),
        # The literal '_' in the prefix must not act as a LIKE wildcard: 'propX' is
        # an ordinary node and its defect type must still be reported.
        ("propX", "event"),
    ])
    conn.commit()
    conn.close()
    before = db.stat().st_mtime_ns, db.stat().st_size

    a = ONT.audit(db)
    assert a["nodes_total"] == 8, a["nodes_total"]
    assert a["proposal_nodes_excluded"] == 2, a["proposal_nodes_excluded"]
    assert a["unknown_types"] == {"<agent|founder>": 1, "decision": 1, "event": 1}, a["unknown_types"]
    assert a["unknown_nodes"] == 3
    assert "concept" not in a["unknown_types"], "prop_* types must not be audited"
    assert a["undocumented_types"] == {"Approval_gate": 1}, a["undocumented_types"]
    assert (db.stat().st_mtime_ns, db.stat().st_size) == before, "--audit must not modify the DB"


def case_10_validator_loads_without_package_context() -> None:
    """validator.py must import by ABSOLUTE PATH with no package context.

    ALTAIR's infra-regression suite (test_brain_domain_range.py) loads it via
    spec_from_file_location. A module-level `from . import ontology` would raise
    ImportError there, so the ontology import is deferred inside the check and
    fails soft. This pins that contract.
    """
    if not _ONT_PRESENT:
        print(f"  skip case_10_validator_loads_without_package_context — {_ONT_SKIP_REASON}", file=sys.stderr)
        return
    path = Path(V.__file__)
    name = "brain_validator_no_package_ctx"
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # @dataclass resolves __module__ via sys.modules
    try:
        spec.loader.exec_module(mod)  # must not raise
        assert len(mod.PREDICATE_CANON) == 82
        assert mod.__package__ in (None, ""), f"expected no package context, got {mod.__package__!r}"
        # The ontology-backed check degrades to a silent no-op rather than blowing up.
        assert mod._ontology() is None, "deferred import must fail soft with no package context"
        tmpd = Path(tempfile.mkdtemp(prefix="brain_ont_nopkg_"))
        try:
            f = _write(tmpd, "n.md", "---\nnode_type: Doohickey\nowner: X\ncreated: 2026-07-31\n---\n")
            viols = mod.validate_node(f)
            assert not any(v.code.startswith("ONT_") for v in viols), \
                f"ONT_* must not fire without the ontology: {[v.code for v in viols]}"
            assert not any(v.severity == "error" for v in viols)
        finally:
            shutil.rmtree(tmpd, ignore_errors=True)
    finally:
        sys.modules.pop(name, None)


# ── Runner ─────────────────────────────────────────────────────────────────

def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="brain_ontology_test_"))
    try:
        if not _ONT_PRESENT:
            _print_skip_banner()
        case_1_loads_and_caches()
        case_2_predicate_parity()
        case_3_inverse_symmetry()
        case_4_family_integrity()
        case_5_transitivity()
        case_6_disjointness(tmp)
        case_7_validator_unknown_node_type(tmp)
        case_7b_validator_functional_and_disjoint(tmp)
        case_8_malformed_raises(tmp)
        case_9_audit_is_read_only(tmp)
        case_10_validator_loads_without_package_context()
        if _ONT_PRESENT:
            print("PASS — ontology: load+cache, 82/82 predicate + retired parity vs validator, "
                  "inverse symmetry, family integrity, transitivity, disjoint/functional machinery "
                  "(fixture), ONT_UNKNOWN_NODE_TYPE / ONT_FUNCTIONAL_VIOLATION / ONT_DISJOINT_VIOLATION "
                  "at warn, OntologyError on 8 malformed inputs, read-only audit, "
                  "absolute-path import with no package context")
        else:
            print("PASS (DEGRADED — NOT a full run): load+cache, 82/82 predicate + retired parity "
                  "vs validator, inverse symmetry, family integrity, transitivity, disjoint/functional "
                  "machinery (fixture), OntologyError on 8 malformed inputs, read-only audit — but "
                  "case_7/case_7b/case_10 (ONT_UNKNOWN_NODE_TYPE / ONT_FUNCTIONAL_VIOLATION / "
                  "ONT_DISJOINT_VIOLATION / no-package-context _ontology() contract) were SKIPPED: " +
                  _ONT_SKIP_REASON)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run())
