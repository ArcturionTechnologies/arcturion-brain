"""Utility helpers — hashing, slug, id, frontmatter parsing, safe yaml load."""

from __future__ import annotations
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from . import config as C


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    s = _NON_ALNUM.sub("_", text.lower()).strip("_")
    return s[:80] or "node"


def short_hash(s: str, n: int = 6) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()[:n]


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_node_id(path: Path, title: str | None = None, day: str | None = None) -> str:
    # Brain v2 (2026-06-16): a node's id is a pure function of (title, path) — NO date
    # stamp. The old `mem_<YYYYMMDD>_...` form minted a NEW id for the same file on every
    # calendar day a reindex ran, and stale rows were never GC'd → 42k paths carried 2-11
    # ids each. `day` is still accepted but IGNORED, for caller/test back-compat.
    base = title or path.stem
    return f"{C.ID_PREFIX}_{slugify(base)}_{short_hash(str(path))}"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def valid_date(s) -> str | None:
    """Return s if it looks like an ISO date/datetime (YYYY-MM-DD…), else None.

    Guards timestamp fields against template placeholders leaking in from frontmatter
    (e.g. a file whose `last_modified:` is the literal '<YYYY-MM-DDTHH:MM:SS-04:00>').
    Such values are non-sortable and corrupt temporal range queries."""
    if not s:
        return None
    s = str(s).strip()
    return s if _ISO_DATE_RE.match(s) else None


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# ATLAS rev3 (2026-05-26): YAML lives inside an Obsidian callout drawer at the
# bottom of the file, not as top-of-file frontmatter. Pattern matches the
# `🔧 Raw YAML` section inside `> [!info]- 📋 Note Properties`.
_ATLAS_DRAWER_YAML_RE = re.compile(
    r">\s*\[!info\][-+]?\s*📋?\s*Note Properties.*?>\s*```yaml\s*\n(.*?)\n>\s*```",
    re.DOTALL,
)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body_without_frontmatter). Empty dict if none.

    Looks first for top-of-file YAML (`---\\n...\\n---`). If absent, falls back
    to ATLAS drawer-nested YAML (rev3 pattern: callout `> [!info]- 📋 Note Properties`
    containing a `> ```yaml ...> ``` ` code block). This keeps Brain indexing
    working for ATLAS notes where YAML lives in the drawer instead of at the top.
    """
    m = _FRONTMATTER_RE.match(text)
    if m:
        raw = m.group(1)
        body = text[m.end():]
        try:
            import yaml
            fm = yaml.safe_load(raw) or {}
            if not isinstance(fm, dict):
                fm = {}
        except Exception:
            fm = {}
        return fm, body

    # Fallback: ATLAS drawer-nested YAML
    dm = _ATLAS_DRAWER_YAML_RE.search(text)
    if dm:
        # Strip leading `> ` from each line of the captured YAML
        raw_lines = dm.group(1).splitlines()
        unprefixed = "\n".join(
            line[2:] if line.startswith("> ") else line[1:] if line.startswith(">") else line
            for line in raw_lines
        )
        try:
            import yaml
            fm = yaml.safe_load(unprefixed) or {}
            if not isinstance(fm, dict):
                fm = {}
        except Exception:
            fm = {}
        return fm, text  # body stays intact (drawer is part of the body)

    return {}, text


_WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:[#\|][^\]]*)?\]\]")


def extract_wikilinks(text: str) -> list[str]:
    return [m.group(1).strip() for m in _WIKILINK_RE.finditer(text)]


# A path component whose name contains "agent" is an agent-home SHELF: the component
# directly beneath it is one agent's own tree. Checked against every real excluded-agent
# path in the live index — `0104_AGENT_IDENTITIES`, `04_CONSTELLATION_AGENTS`,
# `03 🛰️ AGENTS`, `agent_specialized`, and ATLAS's `15. 🧬 Universe & Agentic HQ`.
_AGENT_HOME_SHELF = re.compile(r"agent", re.IGNORECASE)


def excluded_agent_for_path(path) -> str | None:
    """Handle source records using the configured exclusion policy."""
    excluded = getattr(C, "EXCLUDED_AGENTS", {})
    if not excluded:
        return None
    p = Path(path)
    if p.is_absolute():
        for root in (getattr(C, "DESKTOP_ROOT", None), C.WORK_ROOT):
            if root is None:
                continue
            try:
                p = p.relative_to(root)
                break
            except ValueError:
                continue
    parts = p.parts
    for i, part in enumerate(parts):
        if part not in excluded:
            continue
        if i == 0 or _AGENT_HOME_SHELF.search(parts[i - 1]):
            return part
    return None


# Apply the configured exclusion policy.
_PERSONA_FRAME = (r"(?i:you\s+are(?:\s+now)?|you\s*['’]\s*re|you\s+will\s+be|"
                  r"act\s+as|acting\s+as|roleplay\s+as|assume\s+the\s+role\s+of|"
                  r"persona\s*[:=])")


def persona_re():
    """Built at call time, not import time, so a change to EXCLUDED_AGENTS (or a test
    that patches it) takes effect without reloading the module. Returns None when the
    exclusion list is empty."""
    names = sorted(getattr(C, "EXCLUDED_AGENTS", {}) or {})
    if not names:
        return None
    alt = "|".join(re.escape(n) for n in names)
    return re.compile(
        _PERSONA_FRAME + r"\s+(?i:the\s+|agent\s+)*[\"'`*]*(" + alt + r")\b")


def persona_agent_for_text(text: str) -> str | None:
    """The `config.EXCLUDED_AGENTS` key this text ASSIGNS as a persona, or None.

    Returns None for text that merely mentions an excluded agent. Callers outside this
    repository must import THIS function rather than re-declaring the pattern — the
    comment above records what re-declaring it cost."""
    if not text:
        return None
    rx = persona_re()
    if rx is None:
        return None
    hit = rx.search(text)
    return hit.group(1) if hit else None


def should_skip_path(rel_path: Path) -> bool:
    parts = set(rel_path.parts)
    if parts & C.SKIP_DIR_NAMES:
        return True

    # Apply the configured exclusion policy.
    if excluded_agent_for_path(rel_path):
        return True

    s = str(rel_path)
    if s.startswith("."):
        return True
    for frag in C.SKIP_PATH_FRAGMENTS:
        if frag in s:
            return True
    # Archive/backup copies (incl. mixed-case reorg folders) — matched case-
    # insensitively so "99 🗄️ Archive" is skipped exactly like "99 🗄️ ARCHIVE".
    # Shares config.ARCHIVE_MARKERS with resolver so backups never index as
    # status=active and the walker/resolver views of "archive" stay identical.
    sl = s.lower()
    for marker in C.ARCHIVE_MARKERS:
        if marker.lower() in sl:
            return True
    return False


# Applies to the configured graph and source inventory.
_LEGACY_SESSION_LOG_MARKERS = ("logs/sessions", "Memory/Sessions/raw")
# Node types minted only from a raw transcript. See harvesters/sessions.py.
_TRANSCRIPT_NODE_TYPES = frozenset({"Session", "Edit"})


def is_raw_transcript_path(path) -> bool:
    """True when `path` names a raw session TRANSCRIPT (a verbatim prompt-echo), as
    opposed to an authored distillation written about one. See the note above."""
    p = str(path or "")
    if not p:
        return False
    # An Edit node addresses one file INSIDE a transcript: "<transcript>.jsonl#<basename>".
    if p.split("#", 1)[0].endswith(".jsonl"):
        return True
    return any(m in p for m in _LEGACY_SESSION_LOG_MARKERS)


def is_raw_transcript_node(node) -> bool:
    """True when `node` is a raw session transcript row. Prefers the node's TYPE, which no
    restructure can move, and falls back to the path for rows (backups, legacy shelves)
    that predate the harvester's typing."""
    if not isinstance(node, dict):
        return is_raw_transcript_path(node)
    if str(node.get("type") or "") in _TRANSCRIPT_NODE_TYPES:
        return True
    return is_raw_transcript_path(node.get("path"))


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n…[truncated]"


def first_paragraph(text: str, max_chars: int = 400) -> str:
    body = text.strip().split("\n\n", 1)[0]
    return truncate(body, max_chars)
