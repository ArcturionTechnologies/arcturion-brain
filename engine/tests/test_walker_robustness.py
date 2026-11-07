"""Walker robustness — the failure modes that silently shrink the graph (no pytest).

Every case here ends the same way if unguarded: the nightly reindex produces a SMALLER
graph and reports success. That is the shape of failure this repo keeps rediscovering —
a check that asserts less than it appears to, a job whose green exit means nothing. A
walker that aborts halfway is indistinguishable from a universe with fewer files in it.

Run:  python3 -m engine.tests.test_walker_robustness
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from .. import config as C
from .. import walker as W


def _mk(root: Path, rel: str, body: str = "x") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _walked(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p, _f, _t in W.walk_root(root)}


def test_symlink_cycle_terminates() -> None:
    """`Path.rglob` follows directory symlinks with no cycle detection, so one self-
    referential link is unbounded recursion inside the nightly walk."""
    tmp = Path(tempfile.mkdtemp(prefix="walk_cycle_"))
    try:
        _mk(tmp, "real/a.md")
        _mk(tmp, "real/deep/b.md")
        # The classic shape: a directory containing a link back to its own ancestor.
        (tmp / "real" / "loop").symlink_to(tmp, target_is_directory=True)
        (tmp / "real" / "self").symlink_to(tmp / "real", target_is_directory=True)

        got = _walked(tmp)   # must terminate at all
        assert "real/a.md" in got and "real/deep/b.md" in got, got
        # Each real file appears exactly once — not once per path through the link.
        assert len(got) == 2, f"symlink traversal duplicated files: {sorted(got)}"
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_unreadable_directory_does_not_abort_the_walk() -> None:
    """One PermissionError mid-iteration used to abort the whole generator — and with it
    every root queued behind this one, in a run that still exits 0."""
    tmp = Path(tempfile.mkdtemp(prefix="walk_perm_"))
    try:
        _mk(tmp, "aaa/before.md")
        locked = tmp / "bbb_locked"
        locked.mkdir()
        _mk(tmp, "bbb_locked/hidden.md")
        _mk(tmp, "zzz/after.md")
        os.chmod(locked, 0o000)
        try:
            got = _walked(tmp)
            # The readable siblings on BOTH sides of the failure survive. "after" is the
            # one that matters: it is what an abort silently drops.
            assert "aaa/before.md" in got, got
            assert "zzz/after.md" in got, f"walk aborted at the unreadable dir: {sorted(got)}"
        finally:
            os.chmod(locked, 0o755)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_vanishing_file_does_not_abort_the_walk() -> None:
    """A full walk takes minutes; files legitimately disappear during it (temp files,
    an agent rewriting a note). A dangling symlink is the same condition, deterministically."""
    tmp = Path(tempfile.mkdtemp(prefix="walk_gone_"))
    try:
        _mk(tmp, "a.md")
        (tmp / "gone.md").symlink_to(tmp / "does_not_exist.md")
        _mk(tmp, "z.md")
        got = _walked(tmp)
        assert got == {"a.md", "z.md"}, got
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_classify_survives_a_missing_extension_map_entry() -> None:
    """`C.EXTENSION_MAP[".env.tpl"]` was a direct index into hand-edited config: removing
    that one key would have raised KeyError out of the walk instead of just not indexing
    .env.tpl files."""
    orig = dict(C.EXTENSION_MAP)
    try:
        C.EXTENSION_MAP.pop(".env.tpl", None)
        assert W._classify(Path("x/sample.env.tpl")) is None
        assert W._classify(Path("x/a.md")) is not None
    finally:
        C.EXTENSION_MAP.clear()
        C.EXTENSION_MAP.update(orig)


def test_brain_prune_covers_nested_scaffolding_without_eating_content() -> None:
    """`_BRAIN_SKIP_TOP` was checked only at the top level, so a nested `canon/templates/`
    reopened the placeholder-node failure class. The naive fix — any component named
    `templates` — de-indexed 4 real files under the memory/ content shelf, which is exactly
    the collateral the original top-level scoping existed to avoid."""
    for pruned in ["index/graph.json", "templates/decision.md", "embeddings.qdrant/x.json",
                   "canon/templates/blank.md", "docs/index/notes.md",
                   "engine/templates/scaffold.md"]:
        assert W._brain_prune(Path(pruned)), f"nested scaffolding not pruned: {pruned}"

    for kept in ["memory/0609_PARA/Areas/Content/pipeline/templates/weekly-shipped-thread.md",
                 "memory/0609_PARA/Areas/Content/pipeline/digital-products/drafts/"
                 "T1.4-faceless-reels-pack/templates/INDEX.md",
                 "canon/0007_ONTOLOGY.md", "docs/plans/x.md", "engine/brain.py"]:
        assert not W._brain_prune(Path(kept)), f"real content over-pruned: {kept}"

    # Fixtures stay pruned through the same predicate.
    assert W._brain_prune(Path("engine/tests/fixtures/golden.jsonl"))


def test_walk_brain_matches_the_live_index_expectations() -> None:
    """Guard against the rewrite quietly changing coverage: the Brain's own tree must still
    yield its canon/, docs/, registry/ and memory/ content."""
    if not C.BRAIN_ROOT.exists():
        print("     (skipped: BRAIN_ROOT not present)")
        return
    tops = {str(p.relative_to(C.BRAIN_ROOT)).split("/")[0] for p, _f, _t in W.walk_brain()}
    for expected in ("canon", "engine", "memory"):
        assert expected in tops, f"walk_brain stopped yielding {expected}/: {sorted(tops)}"
    for forbidden in ("index", "templates", "embeddings.qdrant"):
        assert forbidden not in tops, f"walk_brain yielded {forbidden}/"


def test_therapist_file_never_produces_a_node_through_any_producer_path() -> None:
    """Leg 7 / 7.1 — THERAPIST must be structurally unindexable, not policy-excluded.

    Three independent producer paths existed historically (or exist today) that could
    seat an excluded agent's file: (1) a full tree walk via `walk_root`, (2) the
    walker's single-FILE branch (`walk_paths`, used by `python3 -m engine.ingest
    <file>` and the live watcher) — the exact hole `_excluded()`'s docstring records as
    "missing entirely" before 2026-08-05, and (3) `ingest.admit_node`, the admission
    gate every producer funnels a parsed node through before it earns a row. This test
    asserts all three refuse a THERAPIST-rooted file, using a throwaway tree so it
    never touches the real vault."""
    from .. import ingest as I

    tmp = Path(tempfile.mkdtemp(prefix="walk_therapist_"))
    orig_desktop_root = C.DESKTOP_ROOT
    try:
        # `excluded_agent_for_path` reduces absolutes to Desktop- or home-relative
        # before matching, so the test tree must BE a Desktop root for the anchor rule
        # ("first path component") to fire the way it does on the real vault.
        C.DESKTOP_ROOT = tmp

        # Desktop-relative shape: <agent>/<content>.md
        f = _mk(tmp, "THERAPIST/02 Mental Health/session-notes.md",
                "confidential therapy content " * 50)

        # (1) full walk_root over the parent — THERAPIST subtree must yield nothing.
        got = _walked(tmp)
        assert not any("THERAPIST" in rel for rel in got), (
            f"walk_root produced THERAPIST-owned paths: {sorted(got)}")

        # (2) single-file branch — the historical hole.
        single = {str(p) for p, _f, _t in W.walk_paths([f])}
        assert not single, f"walk_paths(single file) admitted a THERAPIST file: {single}"

        # (3) admission-gate defense-in-depth — even a node handed to admit_node
        # directly (bypassing the walker entirely) must be refused.
        node = {
            "path": "THERAPIST/02 Mental Health/session-notes.md",
            "title": "session-notes",
            "body_excerpt": "confidential therapy content " * 50,
        }
        admit, reason, _facts = I.admit_node(None, node, path=f)
        assert not admit, f"admit_node ADMITTED a THERAPIST-owned node: {reason}"
        assert "excluded-agent" in reason, f"unexpected refusal reason: {reason}"
    finally:
        C.DESKTOP_ROOT = orig_desktop_root
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  ❌ {t.__name__}: {e}")
    print(f"\n{'PASS' if not failures else 'FAIL'} — walker robustness: "
          f"{len(tests) - failures}/{len(tests)} tests")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
