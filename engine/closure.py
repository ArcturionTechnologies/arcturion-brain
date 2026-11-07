"""Transitive closure traversal (Task 3) — READ-ONLY reachability over the Brain graph.

Coyle's OWL move: *"`ancestor` is transitive — Sue→Mary, Mary→Ann ⇒ Sue→Ann."*
Applied here so "everything ALTAIR owns, all the way down" is ONE query instead of
N round trips:

    python3 -m engine.closure agent_altair owned_by --up

THE ONTOLOGY GOVERNS WHAT REASONING IS LEGAL — NOT THE CALLER.
`descendants`/`ancestors`/`closure_size`/`is_reachable` raise `ValueError` when
`not engine.ontology.is_transitive(predicate)`. Refusing to chain a
non-transitive predicate is the feature, not a limitation: chaining `relates_to`
or `references` would manufacture conclusions the canon never asserted. Exactly
four predicates carry `transitive: true` in canon/0007 — `contains`, `part_of`,
`owns`, `owned_by`. Widening that set is a canon edit, not a code edit.

WRITE DISCIPLINE (Standing constraints 2 & 3):
  This module NEVER writes. When it opens its own connection it opens the DB
  `mode=ro` at the URI level, so a stray write raises rather than lands. There is
  deliberately NO materialised closure table: transitive closure can grow
  quadratically. Expansion runs at query time.

CYCLE-SAFE BY CONSTRUCTION:
  Traversal remains finite even when a predicate contains cycles:

    1. the recursive step is capped by `max_depth`, so the recursion cannot deepen
       forever even on a self-edge; and
    2. rows are consumed in SQLite's breadth-first queue order and each node id is
       kept only the FIRST time it is emitted — which is therefore at its minimum
       depth, with a parent already recorded one level shallower. That first-seen
       parent chain is a genuine shortest path.

REFLEXIVE-FREE BY DESIGN:
  The seed is never a member of its own result set — a node is not its own
  descendant. A node lying on a cycle is therefore absent from its own closure.
  `is_reachable(x, x, p)` still answers the underlying question honestly: it is
  True exactly when a path of length >= 1 leads from x back to x, i.e. when x lies
  on a cycle.

TRUNCATION IS ALWAYS MARKED, NEVER SILENT:
  `max_depth` and `limit` are hard stops on a graph this size. The returned
  `ClosureResult` is a plain `list` subclass carrying `.truncated`,
  `.truncation_reason` ("max_depth" | "limit" | "max_rows"), `.max_depth` and
  `.limit`; its `repr()` says so out loud and the CLI prints a banner. A depth cut
  is confirmed against the graph — the boundary is only reported as truncated when
  a frontier node actually has an unexplored neighbour.

STAGED PROPOSALS (`prop_*`) ARE INCLUDED — deliberately.
  `engine.ontology.audit()` and `engine.reasoner` both EXCLUDE the `prop_*`
  namespace, and this module does not. That is not an inconsistency: those two
  JUDGE nodes against a vocabulary (canon/0005) that does not govern staged
  proposals. Traversal judges nothing — it answers "what is reachable from here",
  and a staged proposal holding a real `contains` edge genuinely IS reachable.
  Hiding it would make "everything under X" quietly incomplete, which is the one
  failure mode a closure query must not have. Callers who want them gone have
  `id` and `title` on every row.

Public API:
  descendants(node_id, predicate, *, conn=None, max_depth=12, limit=10000)
  ancestors(node_id, predicate, *, conn=None, max_depth=12, limit=10000)
  closure_size(node_id, predicate, **kw) -> int
  is_reachable(from_id, to_id, predicate, **kw) -> bool

CLI:
  python3 -m engine.closure <node_id> <predicate> [--up] [--depth N]
"""
from __future__ import annotations

import sqlite3
import sys

from . import config as C
from . import ontology as O

DEFAULT_MAX_DEPTH = 12
DEFAULT_LIMIT = 10_000

# Third backstop, below `max_depth` and `limit`. A dense neighbourhood can emit far
# more (id, depth, parent) rows than it contains distinct nodes, so a walk can be
# expensive long before it is large. Hitting this marks the result truncated like
# any other hard stop.
MAX_SCAN_ROWS = 1_000_000

_ID_CHUNK = 500  # sqlite's default SQLITE_MAX_VARIABLE_NUMBER is 999


class ClosureResult(list):
    """A closure, plus whether it is the WHOLE closure.

    A plain `list` of result dicts (so `== [...]`, slicing and iteration all behave),
    with the truncation verdict attached. Read `.truncated` before treating the
    contents as exhaustive.
    """

    truncated: bool = False
    truncation_reason: str | None = None
    max_depth: int = DEFAULT_MAX_DEPTH
    limit: int | None = DEFAULT_LIMIT

    def __repr__(self) -> str:  # loud on purpose — a silent short answer is the bug
        mark = f" TRUNCATED({self.truncation_reason})" if self.truncated else ""
        return f"<ClosureResult n={len(self)}{mark} max_depth={self.max_depth} limit={self.limit}>"


# ── Legality gate ──────────────────────────────────────────────────────────

def _require_transitive(predicate: str) -> None:
    """The ontology decides what may be chained. The caller does not."""
    if O.is_transitive(predicate):
        return
    known = predicate in O.predicates()
    legal = ", ".join(sorted(O.transitive_predicates()))
    if known:
        raise ValueError(
            f"{predicate!r} is not declared transitive in the ontology — refusing to "
            f"compute its closure. Chaining a non-transitive predicate invents "
            f"conclusions the canon does not assert. Transitive predicates: {legal}. "
            f"To change this, assert `transitive: true` in {O.ONTOLOGY_PATH.name}."
        )
    raise ValueError(
        f"{predicate!r} is not a declared predicate in {O.ONTOLOGY_PATH.name} — "
        f"refusing to compute its closure. Transitive predicates: {legal}."
    )


def _require_bounds(max_depth: int, limit: int | None) -> None:
    if max_depth < 0:
        raise ValueError(f"max_depth must be >= 0, got {max_depth}")
    if limit is not None and limit < 0:
        raise ValueError(f"limit must be >= 0 or None (unbounded), got {limit}")


# ── Traversal ──────────────────────────────────────────────────────────────

# Query-time expansion only. Termination is `w.depth < ?` — the depth cap bounds
# the recursion independently of the graph's shape, cyclic or not — with
# MAX_SCAN_ROWS as the consumption backstop for dense neighbourhoods.
# `UNION` (not UNION ALL) does NOT provide cycle safety: in a genuine cycle each
# lap produces a (id, depth, parent) triple at a *different* depth, so there is
# nothing for UNION to dedup there. Its real job is collapsing EXACT-duplicate
# triples — e.g. the same edge asserted twice by two agents.
#
# Planner hints preserve efficient traversal:
# `CROSS JOIN` keeps the walk as the outer loop; `+e.predicate` prevents an
# unhelpful predicate-only index choice. Unlike INDEXED BY, these hints do not
# require a particular named index to exist.
_SQL_DOWN = """
WITH RECURSIVE walk(id, depth, parent) AS (
    SELECT ?, 0, NULL
  UNION
    SELECT e.to_id, w.depth + 1, w.id
      FROM walk w CROSS JOIN edges e
     WHERE e.from_id = w.id AND +e.predicate = ? AND w.depth < ?
)
SELECT id, depth, parent FROM walk WHERE depth > 0
"""

_SQL_UP = """
WITH RECURSIVE walk(id, depth, parent) AS (
    SELECT ?, 0, NULL
  UNION
    SELECT e.from_id, w.depth + 1, w.id
      FROM walk w CROSS JOIN edges e
     WHERE e.to_id = w.id AND +e.predicate = ? AND w.depth < ?
)
SELECT id, depth, parent FROM walk WHERE depth > 0
"""


def _connect() -> sqlite3.Connection:
    """Open the live brain READ-ONLY. `mode=ro` is enforced by SQLite, not by us."""
    return sqlite3.connect(f"file:{C.BRAIN_DB}?mode=ro", uri=True)


def _walk(conn, node_id, predicate, *, up, max_depth, limit, stop_at=None):
    """One bounded BFS. Returns (seen, truncated, reason, stop_hit).

    `seen` maps id -> (min_depth, parent_at_min_depth-1), insertion-ordered by depth.
    `stop_at` short-circuits the moment that id is emitted (used by is_reachable);
    unlike the normal path it does NOT skip the seed, so a cycle back to the seed
    is observable.
    """
    sql = _SQL_UP if up else _SQL_DOWN
    seen: dict[str, tuple[int, str | None]] = {}
    truncated = False
    reason: str | None = None
    stop_hit = False

    scanned = 0
    cur = conn.execute(sql, (node_id, predicate, max_depth))
    try:
        for rid, depth, parent in cur:
            scanned += 1
            if scanned > MAX_SCAN_ROWS:
                truncated, reason = True, "max_rows"
                break
            if stop_at is not None and rid == stop_at:
                stop_hit = True
                break
            if rid == node_id:
                continue  # reflexive-free: the seed is not its own descendant
            if rid in seen:
                continue  # already recorded at its minimum depth (BFS order)
            if limit is not None and len(seen) >= limit:
                # Checked BEFORE inserting, so `limit` is only reported as a cut when
                # an actual (limit+1)-th distinct node exists. A closure that happens
                # to be exactly `limit` long is complete, and saying otherwise would
                # be as misleading as a silent short answer.
                truncated, reason = True, "limit"
                break
            seen[rid] = (depth, parent)
    finally:
        cur.close()

    return seen, truncated, reason, stop_hit


def _frontier_is_open(conn, node_id, predicate, seen, *, up, max_depth) -> bool:
    """Did the depth cap actually cut anything off?

    Only report a `max_depth` truncation when a node sitting exactly on the boundary
    has a neighbour we never recorded. A closure that simply ends at the boundary is
    complete, and calling it truncated would be as misleading as the reverse.
    """
    if max_depth == 0:
        frontier = [node_id]
    else:
        frontier = [i for i, (d, _) in seen.items() if d == max_depth]
    if not frontier:
        return False
    col_out, col_in = ("from_id", "to_id") if up else ("to_id", "from_id")
    for i in range(0, len(frontier), _ID_CHUNK):
        chunk = frontier[i:i + _ID_CHUNK]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT DISTINCT {col_out} FROM edges "
            f"WHERE predicate = ? AND {col_in} IN ({marks})",
            (predicate, *chunk),
        ).fetchall()
        for (nxt,) in rows:
            if nxt != node_id and nxt not in seen:
                return True
    return False


def _paths(node_id, seen) -> dict[str, list[str]]:
    """Rebuild one shortest path per node from the first-seen parent chain.

    Every recorded parent sits exactly one level shallower than its child, so
    following parents strictly decreases depth and terminates at the seed. The
    guard is belt-and-braces against a malformed chain, never the primary defence.
    """
    out: dict[str, list[str]] = {}
    for rid in seen:
        chain = [rid]
        cur = seen[rid][1]
        guard = 0
        while cur is not None and cur != node_id and guard <= len(seen) + 1:
            chain.append(cur)
            entry = seen.get(cur)
            if entry is None:
                break
            cur = entry[1]
            guard += 1
        chain.append(node_id)
        chain.reverse()
        out[rid] = chain
    return out


def _titles(conn, ids) -> dict[str, str | None]:
    """Human labels for the result rows. ~80% of edge endpoints dangle, so a missing
    node row is ordinary and yields None rather than dropping the result."""
    found: dict[str, str | None] = {}
    ids = list(ids)
    for i in range(0, len(ids), _ID_CHUNK):
        chunk = ids[i:i + _ID_CHUNK]
        marks = ",".join("?" * len(chunk))
        for nid, title in conn.execute(
            f"SELECT id, title FROM nodes WHERE id IN ({marks})", chunk
        ):
            found[nid] = title
    return {i: found.get(i) for i in ids}


def _closure(node_id, predicate, *, up, conn, max_depth, limit) -> ClosureResult:
    _require_transitive(predicate)
    _require_bounds(max_depth, limit)

    own = conn is None
    if own:
        conn = _connect()
    try:
        seen, truncated, reason, _ = _walk(
            conn, node_id, predicate, up=up, max_depth=max_depth, limit=limit)
        if not truncated and _frontier_is_open(
                conn, node_id, predicate, seen, up=up, max_depth=max_depth):
            truncated, reason = True, "max_depth"
        paths = _paths(node_id, seen)
        titles = _titles(conn, seen)
    finally:
        if own:
            conn.close()

    rows = [
        {"id": rid, "depth": depth, "path": paths[rid], "title": titles.get(rid)}
        for rid, (depth, _) in seen.items()
    ]
    rows.sort(key=lambda r: (r["depth"], r["id"]))
    result = ClosureResult(rows)
    result.truncated = truncated
    result.truncation_reason = reason
    result.max_depth = max_depth
    result.limit = limit
    return result


# ── Public API ─────────────────────────────────────────────────────────────

def descendants(node_id: str, predicate: str, *, conn=None,
                max_depth: int = DEFAULT_MAX_DEPTH,
                limit: int | None = DEFAULT_LIMIT) -> ClosureResult:
    """Everything reachable FROM `node_id` by chaining `predicate` forward.

    `X contains Y, Y contains Z` => descendants(X, "contains") == [Y, Z].

    Each row is {"id", "depth", "path", "title"}; `path` runs seed -> ... -> id and
    is A shortest path (ties broken by BFS order). Rows are ordered by depth then id.
    Raises ValueError unless the ontology declares `predicate` transitive.
    Check `.truncated` on the result before treating it as the complete closure.
    """
    return _closure(node_id, predicate, up=False, conn=conn,
                    max_depth=max_depth, limit=limit)


def ancestors(node_id: str, predicate: str, *, conn=None,
              max_depth: int = DEFAULT_MAX_DEPTH,
              limit: int | None = DEFAULT_LIMIT) -> ClosureResult:
    """Everything that reaches `node_id` by chaining `predicate` forward.

    The exact inverse of `descendants` over the same predicate: with
    `X owned_by AGENT`, `ancestors("agent_altair", "owned_by")` is everything
    ALTAIR owns, all the way down. Same row shape, same truncation contract;
    `path` runs seed -> ... -> id in traversal (upward) order.
    """
    return _closure(node_id, predicate, up=True, conn=conn,
                    max_depth=max_depth, limit=limit)


def closure_size(node_id: str, predicate: str, *, up: bool = False, **kw) -> int:
    """How many distinct nodes the closure holds.

    Counts what was actually traversed: if the walk truncated, this is the size of
    the truncated set, not an estimate of the whole. Call `descendants`/`ancestors`
    directly when you need to know which.
    """
    conn = kw.pop("conn", None)
    max_depth = kw.pop("max_depth", DEFAULT_MAX_DEPTH)
    limit = kw.pop("limit", DEFAULT_LIMIT)
    if kw:  # a typo'd bound must not silently become an unbounded walk
        raise TypeError(f"closure_size() got unexpected keyword(s): {sorted(kw)}")
    return len(_closure(node_id, predicate, up=up, conn=conn,
                        max_depth=max_depth, limit=limit))


def is_reachable(from_id: str, to_id: str, predicate: str, *, conn=None,
                 max_depth: int = DEFAULT_MAX_DEPTH,
                 limit: int | None = DEFAULT_LIMIT, up: bool = False) -> bool:
    """Is there a `predicate` chain of length >= 1 from `from_id` to `to_id`?

    Short-circuits the moment the target is emitted rather than materialising the
    whole closure. `is_reachable(x, x, p)` is True exactly when x lies on a cycle.
    A False can mean "no path" OR "no path within max_depth/limit" — tighten those
    bounds only when you can accept that ambiguity.
    """
    _require_transitive(predicate)
    _require_bounds(max_depth, limit)
    own = conn is None
    if own:
        conn = _connect()
    try:
        _, _, _, hit = _walk(conn, from_id, predicate, up=up,
                             max_depth=max_depth, limit=limit, stop_at=to_id)
    finally:
        if own:
            conn.close()
    return hit


# ── CLI ────────────────────────────────────────────────────────────────────

def _main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Transitive closure over the Brain graph (READ-ONLY, query-time). "
                    "Only ontology-declared transitive predicates may be chained: "
                    + ", ".join(sorted(O.transitive_predicates())))
    ap.add_argument("node_id")
    ap.add_argument("predicate")
    ap.add_argument("--up", action="store_true",
                    help="traverse incoming edges (ancestors) instead of outgoing")
    ap.add_argument("--depth", type=int, default=DEFAULT_MAX_DEPTH,
                    help=f"max traversal depth (default {DEFAULT_MAX_DEPTH})")
    args = ap.parse_args(argv)

    fn = ancestors if args.up else descendants
    try:
        out = fn(args.node_id, args.predicate, max_depth=args.depth)
    except ValueError as e:
        print(f"[closure] REFUSED: {e}", file=sys.stderr)
        return 2

    direction = "ancestors (incoming)" if args.up else "descendants (outgoing)"
    print(f"[closure] {direction} of {args.node_id!r} over {args.predicate!r}: "
          f"{len(out):,} node(s), max_depth={args.depth}")
    if out.truncated:
        print(f"  *** TRUNCATED ({out.truncation_reason}) — this is NOT the complete "
              f"closure. Raise --depth or the limit to see more.")
    by_depth: dict[int, int] = {}
    for r in out:
        by_depth[r["depth"]] = by_depth.get(r["depth"], 0) + 1
    if by_depth:
        print("  by depth: " + "  ".join(f"{d}:{n:,}" for d, n in sorted(by_depth.items())))
    for r in out[:25]:
        print(f"    d={r['depth']:<2} {r['id']}  {r['title'] or ''}")
    if len(out) > 25:
        print(f"    ... {len(out) - 25:,} more")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
