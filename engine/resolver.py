"""Node-id resolver (Brain v2 Phase 3a) — the load-bearing fix.

Turns the bare strings that extractors/frontmatter produce ("ALTAIR", "COVENANT",
a title, a relative path) into a real `nodes.id`. Unresolved string endpoints
cannot be traversed by node id; inference can use this resolver to link them.

Build once per pass, reuse for every lookup:
    R = build_resolution_index(conn)
    nid = R.resolve("ALTAIR")        # -> "agent_altair"
"""
from __future__ import annotations

from . import aliases as A
from . import config as C

# Path fragments that mark a non-canonical (archive/backup/staging) copy. When two
# nodes share a title, the live one wins as the resolution target. Sourced from
# config.ARCHIVE_MARKERS — the single list the walker (util.should_skip_path) also
# uses — so skip rules and resolution tie-breaks can never drift apart again.
ARCHIVE_MARKERS = C.ARCHIVE_MARKERS


def is_archive_path(path: str | None) -> bool:
    p = path or ""
    parts = set(p.replace("\\", "/").lower().split("/"))
    return any(m.lower() in p.lower() for m in ARCHIVE_MARKERS) or bool(parts & set(C.HISTORICAL_SOURCE_MARKERS))


import re as _re

# Split on the first separator so "ALTAIR_Engineering", "LYRA + COVENANT",
# "ORION (discovery) | ..." all surface a leading agent token.
_FIRST_TOKEN = _re.compile(r"[ _(|+·,/]")


def _looks_junk(token: str) -> bool:
    """Template placeholders that can never resolve to a node."""
    t = (token or "").strip()
    if not t:
        return True
    if "<" in t or ">" in t:          # <OWNER>, <AGENT>, <AGENT NAME>
        return True
    return False


class Resolver:
    def __init__(self, ids: set[str], by_agent: dict, by_title: dict, alias_to_canonical: dict):
        self._ids = ids
        self._by_agent = by_agent            # UPPER agent name -> agent_<lower>
        self._by_title = by_title            # title.lower() -> node id (live preferred)
        self._alias = alias_to_canonical     # alias string -> canonical id/name

    def resolve(self, token: str, hint: str | None = None) -> str | None:
        if token is None:
            return None
        t = str(token).strip()
        if not t or _looks_junk(t):
            return None
        # 1. already a real node id
        if t in self._ids:
            return t
        # 2. agent name (ALTAIR -> agent_altair)
        a = self._by_agent.get(t.upper())
        if a:
            return a
        # 3. alias table (may yield a canonical id or another name → re-resolve once)
        c = self._alias.get(t) or self._alias.get(t.lower())
        if c:
            if c in self._ids:
                return c
            a2 = self._by_agent.get(str(c).upper())
            if a2:
                return a2
            t2 = self._by_title.get(str(c).lower())
            if t2:
                return t2
        # 4. exact title match (live copy preferred at build time)
        title_hit = self._by_title.get(t.lower())
        if title_hit:
            return title_hit
        # 5. leading agent token: "ALTAIR_Engineering", "LYRA + COVENANT",
        #    "ORION (discovery) | ..." → the primary agent owner.
        first = _FIRST_TOKEN.split(t, 1)[0].strip()
        if first and first.upper() in self._by_agent:
            return self._by_agent[first.upper()]
        return None


def build_resolution_index(conn) -> Resolver:
    ids: set[str] = set()
    by_agent: dict[str, str] = {}
    by_title: dict[str, str] = {}
    by_title_path: dict[str, str] = {}  # remember the chosen target's path for live-preference

    for r in conn.execute("SELECT id, title, path FROM nodes").fetchall():
        nid = r["id"]
        ids.add(nid)
        if nid.startswith("agent_"):
            by_agent[nid[len("agent_"):].upper()] = nid
        title = (r["title"] or "").strip()
        if title:
            k = title.lower()
            path = r["path"] or ""
            cur_path = by_title_path.get(k)
            # first-seen wins, but a live path always displaces an archive path
            if k not in by_title or (is_archive_path(cur_path) and not is_archive_path(path)):
                by_title[k] = nid
                by_title_path[k] = path

    try:
        _, alias_to_canonical = A.load_aliases()
    except Exception:
        alias_to_canonical = {}

    return Resolver(ids, by_agent, by_title, alias_to_canonical)
