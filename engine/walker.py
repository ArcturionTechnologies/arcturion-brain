"""Universal file walker. Yields (Path, family, default_node_type) tuples."""

from __future__ import annotations
import os
import sys
from pathlib import Path
from typing import Callable, Iterator
from . import config as C
from .util import should_skip_path, excluded_agent_for_path


def _warn(msg: str) -> None:
    """Walk problems go to stderr, never silence. The nightly walker's launchd job
    captures stderr to ~/Library/Logs/arcturion/brain-walker.err, so a root that
    partially failed leaves evidence instead of just producing a smaller graph."""
    print(f"[walker] {msg}", file=sys.stderr)


def _excluded(p: Path, how: str) -> bool:
    """Handle source records using the configured exclusion policy."""
    from .source_integrity import restricted_source_path
    if restricted_source_path(p):
        _warn('refusing restricted rollback artifact source')
        return True
    agent = excluded_agent_for_path(p)
    if agent:
        # Named, not silent — but the message names the AGENT and the reason, never the
        # file, because the whole point of the exclusion is that these paths are private.
        _warn(f"refusing {how}: belongs to excluded agent {agent!r} "
              f"(config.EXCLUDED_AGENTS)")
        return True
    return False


def _classify(path: Path) -> tuple[str, str] | None:
    name = path.name.lower()
    if name.endswith(".env.tpl"):
        # .get, not [] — EXTENSION_MAP is hand-edited config, and a KeyError here would
        # abort an entire nightly walk over one missing dict entry. Missing means
        # "don't index this extension", which is the same answer the general path gives.
        return C.EXTENSION_MAP.get(".env.tpl")
    ext = path.suffix.lower()
    return C.EXTENSION_MAP.get(ext)


def _iter_tree(root: Path, extra_prune: Callable[[Path], bool] | None = None
               ) -> Iterator[tuple[Path, Path]]:
    """Yield (absolute path, path relative to root) for every file under `root`.

    Replaces `Path.rglob("*")`, which had two failure modes that could take down a
    whole nightly reindex:

    1. **rglob follows directory symlinks with no cycle detection.** One cyclic symlink
       anywhere under a walked root (a `node_modules` self-link, a worktree pointing at
       an ancestor) is unbounded recursion. `os.walk(followlinks=False)` does not descend
       symlinked directories at all, and a visited-realpath set catches the cases a
       symlink check cannot — firmlinks, bind mounts, and hardlinked directory trees.
       Not following symlinks loses no coverage here: the extractors store paths by
       RESOLVED location, so a file reached through `memory/0601_AGENT_MEMORY/ALTAIR ->
       ~/Desktop/ALTAIR/…/Memory` produced the same node as the direct walk of that agent
       root. The symlink traversal was duplicated work, not extra content.
    2. **No exception handling.** rglob raises out of the generator, so a single
       PermissionError or ENOENT (a file deleted mid-walk — routine, the walk takes
       minutes) aborted the iteration and every root queued behind it. The reindex then
       upserts a partial graph and reports success. Errors are now contained to the
       subtree that raised, reported to stderr, and the walk continues.

    Directories are pruned as they are discovered rather than filtered file-by-file:
    `should_skip_path` is monotone over path prefixes (its component, fragment, leading-dot
    and archive-marker rules all still match once a parent matched), so pruning cannot skip
    a file the per-file check would have kept. The per-file check below remains the
    authority; pruning only avoids descending into `node_modules` to reject it 40,000 times.
    """
    try:
        if not root.is_dir():
            return
    except OSError as exc:
        _warn(f"cannot stat root {root}: {exc}")
        return

    visited: set[tuple[int, int]] = set()

    def _on_error(exc: OSError) -> None:
        _warn(f"skipping unreadable directory {getattr(exc, 'filename', '?')}: {exc}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=_on_error, followlinks=False):
        here = Path(dirpath)
        # Cycle guard. os.walk(followlinks=False) already refuses symlinked dirs; the
        # (st_dev, st_ino) identity check additionally stops a loop formed by any other
        # means, and is cheap — one stat per directory.
        try:
            st = os.stat(dirpath)
            key = (st.st_dev, st.st_ino)
            if key in visited:
                _warn(f"cycle detected, not descending again into {dirpath}")
                dirnames[:] = []
                continue
            visited.add(key)
        except OSError as exc:
            _warn(f"cannot stat {dirpath}: {exc}")
            dirnames[:] = []
            continue

        kept = []
        for d in dirnames:
            rel_d = (here / d).relative_to(root)
            if should_skip_path(rel_d) or (extra_prune and extra_prune(rel_d)):
                continue
            kept.append(d)
        dirnames[:] = kept

        for name in filenames:
            p = here / name
            rel = p.relative_to(root)
            if should_skip_path(rel) or (extra_prune and extra_prune(rel)):
                continue
            try:
                if not p.is_file():   # dangling symlink, socket, fifo
                    continue
            except OSError as exc:
                _warn(f"cannot stat {p}: {exc}")
                continue
            if _excluded(p, 'file source'):
                continue
            yield p, rel


def walk_root(root: Path) -> Iterator[tuple[Path, str, str]]:
    if not root.exists():
        return
    # The ROOT itself, before descending. `_iter_tree` filters paths RELATIVE to this
    # root, so if the root is an excluded agent's home the exclusion is invisible to
    # every per-file check underneath it.
    if _excluded(root, f"walk of root {root.name!r}"):
        return
    for p, _rel in _iter_tree(root):
        classification = _classify(p)
        if classification is None:
            continue
        family, node_type = classification
        yield p, family, node_type


# Applies to the configured graph and source inventory.
_BRAIN_SCAFFOLD_DIRS = {"index", "embeddings.qdrant", "templates"}

# ...but the depth-wide check is NOT applied under these top-level shelves, which mirror
# AUTHORED CONTENT rather than holding Brain scaffolding. Measured before shipping: a
# naive "any component named templates" rule de-indexed 4 real files —
# `memory/0609_PARA/Areas/Content/pipeline/templates/weekly-shipped-thread.md` and a
# faceless-reels product's CapCut/Canva spec pack. Those are LYRA content-pipeline
# templates, not blank Brain forms, and they are precisely the collateral the original
# top-level scoping existed to avoid. Under these shelves the top-level rule alone applies.
_BRAIN_CONTENT_SHELVES = {"memory", "sessions", "context_packs", "nodes", "entities"}


def _brain_prune(rel: Path) -> bool:
    "Extra prune rules for the Brain's own tree."
    parts = rel.parts
    if not parts:
        return False
    if parts[0] in _BRAIN_SCAFFOLD_DIRS:
        return True                       # top-level artifact/scaffolding dir
    if parts[0] not in _BRAIN_CONTENT_SHELVES and _BRAIN_SCAFFOLD_DIRS & set(parts):
        return True                       # nested scaffolding outside the content shelves
    return C.is_brain_fixture_path(rel)


def walk_brain() -> Iterator[tuple[Path, str, str]]:
    """Walk the brain's OWN content under BRAIN_ROOT (Founder directive 2026-06-16:
    "index everything in ARCTURION_Brain"). The walker historically covered HQ +
    agents + ATLAS + runtime-map but never the brain itself, so its memory/ shelves
    (incl. the 0608 .md distillations), canon/, registry/, entities/, nodes/, and
    engine/ code were invisible. Prunes index artifacts, blank templates and eval
    fixtures at any depth (see _brain_prune) and applies the same SKIP rules as every
    other root. Stored paths resolve 'ARCTURION_Brain/...' (Desktop-relative, via the
    extractors' _rel_to_universe fallback) → prong 'hq'."""
    brain_root = getattr(C, "BRAIN_ROOT", None)
    if not brain_root or not brain_root.exists():
        return
    for p, _rel in _iter_tree(brain_root, extra_prune=_brain_prune):
        cls = _classify(p)
        if cls is None:
            continue
        yield p, cls[0], cls[1]


def walk_universe_and_constellation() -> Iterator[tuple[Path, str, str]]:
    """Walk the Universe root + each constellation agent workspace + ATLAS + the Brain.

    ATLAS is the third prong of Commandment 019 (Founder's canonical second brain).
    Added to the scan path 2026-05-26 after the rev3 properties backfill. Drawer-
    nested YAML is parsed via util.parse_frontmatter's callout fallback.
    """
    yield from walk_root(C.UNIVERSE_ROOT)
    for agent in C.CONSTELLATION_AGENTS:
        agent_root = C.DESKTOP_ROOT / agent
        if not agent_root.exists():
            continue
        for tup in walk_root(agent_root):
            yield tup
    # Third prong — Founder's second brain
    atlas_root = getattr(C, "ATLAS_ROOT", None)
    if atlas_root and atlas_root.exists():
        yield from walk_root(atlas_root)
    # Fourth prong — External Runtime MAP only (Commandment 019, 2026-06-09).
    # We index the manifest/service-catalog, NOT the runtime contents.
    yield from walk_runtime_map()
    # The brain's own content (Founder directive 2026-06-16: index everything in
    # ARCTURION_Brain). Index artifacts pruned inside walk_brain.
    yield from walk_brain()
    # Audit follow-up (Founder 2026-06-16): two knowledge surfaces outside the prong
    # roots — ~/.claude engineering plans + hooks, and ORION's DeepSearch notes.
    yield from walk_extra_roots()


# Knowledge surfaces that live outside the standard prong roots but the Founder wants
# indexed (audit 2026-06-16). Everything else under ~/.claude or Desktop is untouched.
_EXTRA_ROOTS = [
    C.WORK_ROOT / ".claude" / "plans",
    C.WORK_ROOT / ".claude" / "hooks",
    C.DESKTOP_ROOT / "Orion DeepSearch",
]


def walk_extra_roots() -> Iterator[tuple[Path, str, str]]:
    """Walk the explicit extra knowledge roots (Founder audit follow-up 2026-06-16):
    ~/.claude/plans + ~/.claude/hooks (engineering plans + hook scripts) and the
    Desktop 'Orion DeepSearch' research-notes folder. Stored paths resolve to
    '.claude/...' / 'Orion DeepSearch/...' via the extractors' _rel_to_universe."""
    for root in _EXTRA_ROOTS:
        if root.exists():
            yield from walk_root(root)


def walk_runtime_map() -> Iterator[tuple[Path, str, str]]:
    """Yield ONLY the External Runtime map files (manifest + service catalog).

    The runtime's contents stay excluded (SKIP_DIR_NAMES has ARCTURION_EXTERNAL_RUNTIME);
    these specific files are walked directly so the brain knows what runs where without
    ingesting venvs/weights/caches. Fourth prong of the Four-Prong Canon."""
    runtime_root = getattr(C, "RUNTIME_ROOT", None)
    if not runtime_root or not runtime_root.exists():
        return
    map_files = [runtime_root / name for name in getattr(C, "RUNTIME_MAP_FILES", [])]
    yield from walk_paths([p for p in map_files if p.is_file()])


def walk_paths(paths: list[Path]) -> Iterator[tuple[Path, str, str]]:
    """Walk an explicit list of files/dirs — the entry point `engine.ingest` uses, and
    therefore the one the live watcher and every session-end hook reach.

    The single-FILE branch below is where the EXCLUDED_AGENTS rule was missing entirely
    until 2026-08-05: it never called any skip check, so `python3 -m engine.ingest
    <excluded file>` indexed it. The directory branch delegates to `walk_root`, which
    now also checks the root it was handed."""
    for p in paths:
        if p.is_file():
            # B-1: the single-file path. This is the chokepoint the leg was supposed to
            # close and did not.
            if _excluded(p, f"ingest of file {p.name!r}"):
                continue
            cls = _classify(p)
            if cls:
                yield p, cls[0], cls[1]
        elif p.is_dir():
            yield from walk_root(p)
