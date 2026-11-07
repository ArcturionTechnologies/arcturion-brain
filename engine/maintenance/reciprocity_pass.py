"""Reciprocity pass — close hub backlinks (Item 2 of 17).

For each canonical hub (AGENT_REGISTRY, AGENT_HOME_MAP, MEMORY_INDEX,
PROJECT_STATUS_REGISTRY, MASTER_CLOCK), parse the entities the hub references
and inject `related_to: - "[[<hub>]]"` into each referenced entity's
frontmatter (idempotent — skip when already present).

Run:
    python3 -m engine.maintenance.reciprocity_pass [--dry-run]

Idempotent. Wired into brain-reindex.sh as Phase 2b.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from .. import config as C
from typing import Iterable

import yaml


class _PreserveStringsLoader(yaml.SafeLoader):
    """SafeLoader that keeps ISO timestamps as strings so round-tripping
    doesn't strip the `T` separator or shift to PyYAML's date format."""


def _construct_timestamp_as_string(loader, node):
    return loader.construct_scalar(node)


_PreserveStringsLoader.add_constructor(
    "tag:yaml.org,2002:timestamp", _construct_timestamp_as_string
)
_PreserveStringsLoader.add_constructor(
    "tag:yaml.org,2002:value", _construct_timestamp_as_string
)

UNIVERSE_ROOT = C.UNIVERSE_ROOT

# Hub → file path under UNIVERSE_ROOT
HUBS: dict[str, Path] = {
    "AGENT_REGISTRY": Path("04_CONSTELLATION_AGENTS/0400_INDEX/AGENT_REGISTRY.md"),
    "AGENT_HOME_MAP": Path("04_CONSTELLATION_AGENTS/0400_INDEX/AGENT_HOME_MAP.md"),
    "MEMORY_INDEX": Path("06_MEMORY_SYSTEM/0600_INDEX/MEMORY_INDEX.md"),
    "PROJECT_STATUS_REGISTRY": Path("07_PROJECTS/0700_INDEX/PROJECT_STATUS_REGISTRY.md"),
    "MASTER_CLOCK": Path("03_EXECUTIVE_CORE/0301_ACTIVE_CONTEXT/MASTER_CLOCK.md"),
}

# Backtick-quoted path → likely entity. Matches both 2-digit (`04_CONSTELLATION/...`)
# and 4-digit (`0401_ACTIVE_AGENTS/...`) shelf prefixes. The hub authors mix both forms.
BACKTICK_PATH_RE = re.compile(r"`([0-9]{2,4}_[A-Z][A-Z0-9_]+(?:/[0-9A-Za-z_\-\.]+)+/?)`")
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)\]\]")

# Representative file names per directory (order = priority)
REPRESENTATIVE_FILES = ["00_Profile.md", "README.md", "index.md", "00_Index.md"]


def _resolve_dir_to_file(dir_path: Path) -> Path | None:
    """Pick the representative .md file for a directory."""
    if not dir_path.is_dir():
        return None
    for name in REPRESENTATIVE_FILES:
        p = dir_path / name
        if p.exists():
            return p
    # Fallback: first .md in dir
    candidates = sorted(dir_path.glob("*.md"))
    return candidates[0] if candidates else None


def _resolve_path_ref(ref: str) -> Path | None:
    """Resolve a backtick-quoted path reference to an actual .md file.

    Tries UNIVERSE_ROOT/ref first; if that doesn't exist, falls back to a glob
    search for the deepest directory name. This handles hubs that reference
    nested shelves using shortened forms like `0401_ACTIVE_AGENTS/ALTAIR_Engineering/`
    which actually live at `04_CONSTELLATION_AGENTS/0401_ACTIVE_AGENTS/...`.
    """
    target = (UNIVERSE_ROOT / ref).resolve()
    try:
        target.relative_to(UNIVERSE_ROOT)
    except ValueError:
        return None
    if target.exists():
        if target.is_dir():
            return _resolve_dir_to_file(target)
        if target.suffix == ".md":
            return target
        return None
    parts = ref.rstrip("/").split("/")
    if not parts:
        return None
    deepest = parts[-1]
    if not deepest or "." in deepest:
        return None
    ref_suffix = "/" + "/".join(parts)

    def _is_archive(p: Path) -> bool:
        s = p.as_posix()
        return (
            "ARCHIVE" in s
            or "/staging_snapshots/" in s
            or "/retired_staging" in s
            or "/.git/" in s
            or "/Historical_Contracts/" in s
            or "/before_desktop/" in s
            or "/before/" in s
            or "/after_active/" in s
        )

    candidates = [p for p in UNIVERSE_ROOT.rglob(deepest) if p.is_dir() and not _is_archive(p)]
    if not candidates:
        return None
    # Prefer candidates whose path ends with the full ref (suffix match).
    suffix_matches = [p for p in candidates if p.as_posix().endswith(ref_suffix)]
    ordered = suffix_matches + [p for p in candidates if p not in suffix_matches]
    # Try each candidate in priority order until one yields a representative file.
    for cand in ordered:
        resolved = _resolve_dir_to_file(cand)
        if resolved is not None:
            return resolved
    return None


def _resolve_wikilink(name: str) -> Path | None:
    """Resolve `[[FOO]]` → first `FOO.md` found anywhere under UNIVERSE_ROOT."""
    candidates = list(UNIVERSE_ROOT.rglob(f"{name}.md"))
    # Prefer ones not in archive
    for c in candidates:
        rel = c.relative_to(UNIVERSE_ROOT).as_posix()
        if "/99_ARCHIVE/" in f"/{rel}" or rel.startswith("99_ARCHIVE/"):
            continue
        return c
    return candidates[0] if candidates else None


def extract_entity_refs(hub_path: Path) -> list[Path]:
    """Return the unique list of representative files referenced by a hub."""
    text = hub_path.read_text(encoding="utf-8")
    seen: set[Path] = set()
    out: list[Path] = []

    for m in BACKTICK_PATH_RE.finditer(text):
        target = _resolve_path_ref(m.group(1))
        if target and target not in seen and target != hub_path:
            seen.add(target)
            out.append(target)

    for m in WIKILINK_RE.finditer(text):
        target = _resolve_wikilink(m.group(1).strip())
        if target and target not in seen and target != hub_path:
            seen.add(target)
            out.append(target)

    return out


def _split_frontmatter(text: str) -> tuple[dict, str, bool]:
    """Return (frontmatter_dict, body_text, had_frontmatter)."""
    if not text.startswith("---\n"):
        return {}, text, False
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text, False
    raw_yaml = text[4:end]
    body = text[end + 5:]
    try:
        data = yaml.load(raw_yaml, Loader=_PreserveStringsLoader) or {}
        if not isinstance(data, dict):
            return {}, text, False
    except Exception:
        return {}, text, False
    return data, body, True


def _serialize_frontmatter(fm: dict, body: str) -> str:
    raw = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=120)
    return f"---\n{raw}---\n{body}"


def _ensure_backlink(fm: dict, hub_name: str) -> bool:
    """Add `[[hub_name]]` to fm['related_to'] if not present. Returns True if changed."""
    token = f"[[{hub_name}]]"
    rel = fm.get("related_to")
    if rel is None:
        fm["related_to"] = [token]
        return True
    if isinstance(rel, str):
        if token in rel:
            return False
        fm["related_to"] = [rel, token]
        return True
    if isinstance(rel, list):
        if token in rel:
            return False
        rel.append(token)
        return True
    # Unknown type — leave alone, signal no change
    return False


def inject_backlink(target: Path, hub_name: str, dry_run: bool = False) -> str:
    """Inject backlink into target's frontmatter. Returns status string."""
    try:
        text = target.read_text(encoding="utf-8")
    except Exception as e:
        return f"read_error: {e}"

    fm, body, had_fm = _split_frontmatter(text)
    if not had_fm:
        # File has no frontmatter — skip rather than fabricate one.
        return "no_frontmatter"

    if not _ensure_backlink(fm, hub_name):
        return "already_present"

    if dry_run:
        return "would_inject"

    new_text = _serialize_frontmatter(fm, body)
    try:
        target.write_text(new_text, encoding="utf-8")
    except Exception as e:
        return f"write_error: {e}"
    return "injected"


def run(dry_run: bool = False) -> dict:
    """Execute the reciprocity pass. Returns per-hub stats."""
    stats: dict[str, dict] = {}
    for hub_name, hub_rel in HUBS.items():
        hub_path = UNIVERSE_ROOT / hub_rel
        if not hub_path.exists():
            stats[hub_name] = {"error": "hub_missing"}
            continue

        targets = extract_entity_refs(hub_path)
        hub_stats = {
            "targets_found": len(targets),
            "injected": 0,
            "already_present": 0,
            "no_frontmatter": 0,
            "errors": [],
            "would_inject": 0,
        }

        for target in targets:
            status = inject_backlink(target, hub_name, dry_run=dry_run)
            if status == "injected":
                hub_stats["injected"] += 1
            elif status == "already_present":
                hub_stats["already_present"] += 1
            elif status == "no_frontmatter":
                hub_stats["no_frontmatter"] += 1
            elif status == "would_inject":
                hub_stats["would_inject"] += 1
            else:
                hub_stats["errors"].append({"path": str(target.relative_to(UNIVERSE_ROOT)), "status": status})

        stats[hub_name] = hub_stats

    return stats


def _print_stats(stats: dict, dry_run: bool) -> None:
    label = "[dry-run]" if dry_run else "[reciprocity]"
    print(f"\n{label} Hub backlink pass — {sum(s.get('injected', 0) + s.get('would_inject', 0) for s in stats.values())} edits across {len(stats)} hubs\n")
    for hub, s in stats.items():
        if "error" in s:
            print(f"  ❌ {hub}: {s['error']}")
            continue
        action = "would inject" if dry_run else "injected"
        print(
            f"  📌 {hub}: {s.get('would_inject' if dry_run else 'injected', 0)} {action}, "
            f"{s.get('already_present', 0)} already, "
            f"{s.get('no_frontmatter', 0)} no-fm, "
            f"{len(s.get('errors', []))} errors "
            f"({s.get('targets_found', 0)} targets found)"
        )
        if s.get("errors"):
            for err in s["errors"][:3]:
                print(f"     • {err['path']}: {err['status']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Reciprocity pass — close hub backlinks.")
    ap.add_argument("--dry-run", action="store_true", help="Report changes without writing.")
    args = ap.parse_args()
    stats = run(dry_run=args.dry_run)
    _print_stats(stats, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
