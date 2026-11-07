'Dedup + GC (Brain v2 Phase 7).'
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
import sys
from collections import defaultdict
from pathlib import Path

from .. import config as C
from .. import db as DB
from ..util import now_iso
from ..resolver import is_archive_path
from .entity_dedup import _reparent_edges, _emit_supersession_edge, _mark_superseded

_DATED_ID = re.compile(r"^mem_\d{8}_")


def write_recovery_log(conn, pass_name: str, losers, log_dir=None,
                       db_path=None, extra: dict | None = None):
    """Record exactly what is about to be deleted, flushed to disk BEFORE any DELETE.

    Same contract and format as `engine.maintenance.skiplist_gc.write_recovery_log` —
    deliberately the same pattern rather than a second invention, because there is now
    one recovery procedure to learn instead of two.

    A node row is small; the edges attached to it are not, and these passes remove both.
    A DB backup tells you the state before; only this tells you what THIS run decided to
    remove and why. Reconstructing that from a diff of a 1.2 GB file is not a recovery
    procedure anyone follows at the moment it matters.

    fsync'd before the transaction opens, so it survives a crash mid-DELETE — the case
    where knowing the intended set is worth the most.

    Returns the log path. **RAISES** if the log cannot be written — it does not return
    None, and it does not warn-and-continue. That is deliberate and the callers depend on
    it: an unattended 02:30 hard delete with no record of what it removed is the exact
    hazard this function exists to prevent, so failing to log must STOP the delete rather
    than annotate it. (An earlier version of this docstring promised a None return. The
    docstring was the bug; the fail-safe behaviour was already correct.)
    """
    losers = list(losers)
    base = Path(log_dir) if log_dir else (C.BRAIN_ROOT / "backups")
    path = base / f"{pass_name}_{time.strftime('%Y%m%dT%H%M%S')}.json"
    # Positional indexing, not r["id"]: callers hand us whatever connection they have and
    # sqlite3's default row_factory returns plain tuples.
    rows = {r[0]: r[1] for r in
            conn.execute("SELECT id, path FROM nodes WHERE path IS NOT NULL")}
    payload = {
        "swept": now_iso(),
        "pass": pass_name,
        "db": str(db_path or C.BRAIN_DB),
        "total": len(losers),
        "_note": f"Nodes removed by engine.maintenance.content_dedup:{pass_name}. Edges, "
                 f"FTS rows and embeddings attached to these IDs were removed with them "
                 f"and are NOT listed here — restore from the DB backup taken before the "
                 f"sweep, then use this file to confirm the scope.",
        **(extra or {}),
        "removed": [{"id": i, "path": rows.get(i)} for i in sorted(losers)],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(payload, indent=2, ensure_ascii=False))
        f.flush()
        os.fsync(f.fileno())
    return path


def _drop_derived_rows(conn, temp_table: str, id_col: str) -> None:
    """Delete the FTS rows AND the vectors belonging to a set of removed node ids.

    Every pass here used to drop `nodes` + `edges` + `fts_nodes` and leave `embeddings`
    alone. `vector_seed` reads `SELECT node_id, vector FROM embeddings` with NO join
    back to `nodes`, so an orphan vector still seeds a candidate id that resolves to
    nothing — a phantom occupying a seed slot. These maintenance passes must remove
    related embeddings as well as nodes, edges and full-text entries.

    engine/maintenance/skiplist_gc.py already did this correctly; this is the same
    pattern, hoisted so all three content_dedup passes share it.

    ONLY "no such table" is swallowed. The original bare `except Exception: pass` meant a
    LOCKED or interrupted delete silently recreated the very orphan-vector class this
    function was written to fix — a self-healing bug is not self-healing if its repair can
    fail quietly. A missing table is a fixture graph and genuinely fine; anything else
    (SQLITE_BUSY, disk error, corruption) is raised, which aborts the pass inside its
    transaction so nodes and vectors cannot diverge.

    Cost note (measured 2026-08-05, live 1.19 GB index): `node_id` is UNINDEXED in the
    FTS5 schema, so this is a scan of `fts_nodes_content`. Measured at **0.02 s for 1,600
    ids** — `DB.upsert_node` already issues the identical `DELETE FROM fts_nodes WHERE
    node_id = ?` once per upserted node (hundreds per pass), so this adds nothing new and
    comes nowhere near the watcher's `busy_timeout=30000`. Left as-is deliberately; an
    FTS5 virtual-table column cannot be indexed, and the alternatives (a shadow rowid map,
    or indexing the private `fts_nodes_content` shadow table) buy nothing at 0.02 s.
    """
    for table, col in (("fts_nodes", "node_id"), ("embeddings", "node_id")):
        try:
            conn.execute(f"DELETE FROM {table} WHERE {col} IN "
                         f"(SELECT {id_col} FROM {temp_table})")
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                continue          # fixture graph without FTS/vectors — genuinely fine
            raise


# ── 7a — same-path id-churn collapse (bulk) ──────────────────────────────────

def _canonical_same_path(items: list[tuple[str, str]]) -> str:
    """items = [(id, last_modified)]. Prefer a dateless stable id, then newest."""
    def key(it):
        nid, lm = it
        return (0 if not _DATED_ID.match(nid) else 1, _neg_str(lm), nid)
    return sorted(items, key=key)[0][0]


def _neg_str(s: str):
    # newest last_modified first → invert lexicographic order of ISO timestamps
    return tuple(-ord(c) for c in (s or ""))


def gc_same_path_twins(conn, verbose: bool = True, apply: bool = True,
                       recovery_log_dir=None, db_path=None) -> dict:
    """Collapse the multiple node ids that share one path into the canonical one.

    RUNS ON EVERY REINDEX since 2026-08-05 (Leg 4), unattended, at 02:30. Because of
    that it now behaves like Leg 1's sweep: `apply=False` performs the identical counting
    pass and writes nothing, and every applied run writes a recovery log naming each
    doomed id BEFORE the transaction opens. If the log cannot be written the delete does
    NOT proceed — an unattended hard delete with no record of what it removed is the
    thing being guarded against, so failing to log is a reason to stop, not a detail to
    warn about."""
    rows = conn.execute(
        "SELECT id, path, last_modified FROM nodes WHERE path IS NOT NULL"
    ).fetchall()
    by_path: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for r in rows:
        by_path[r["path"]].append((r["id"], r["last_modified"] or ""))

    mapping: list[tuple[str, str]] = []  # (loser_id, canonical_id)
    for path, items in by_path.items():
        if len(items) < 2:
            continue
        canonical = _canonical_same_path(items)
        for nid, _ in items:
            if nid != canonical:
                mapping.append((nid, canonical))

    if not mapping:
        if verbose:
            print("[content_dedup:7a] no same-path twins")
        return {"paths_collapsed": 0, "losers_removed": 0, "applied": bool(apply),
                "recovery_log": None}

    if not apply:
        n_paths = len({c for _, c in mapping})
        if verbose:
            print(f"[content_dedup:7a] DRY RUN — would collapse {n_paths} paths, "
                  f"removing {len(mapping)} id-churn twins")
        return {"paths_collapsed": n_paths, "losers_removed": 0, "applied": False,
                "candidates": len(mapping), "recovery_log": None}

    log_path = write_recovery_log(
        conn, "gc_same_path_twins", [l for l, _ in mapping],
        log_dir=recovery_log_dir, db_path=db_path,
        extra={"paths_collapsed": len({c for _, c in mapping})})

    # Bulk rewrite via a temp id-map: far faster than per-node UPDATEs for ~98k losers.
    conn.execute("DROP TABLE IF EXISTS _idmap")
    conn.execute("CREATE TEMP TABLE _idmap (loser TEXT PRIMARY KEY, canonical TEXT NOT NULL)")
    conn.executemany("INSERT OR IGNORE INTO _idmap (loser, canonical) VALUES (?,?)", mapping)
    conn.execute("CREATE INDEX IF NOT EXISTS _idmap_canon ON _idmap(canonical)")

    # Reparent edges loser→canonical (UPDATE OR IGNORE skips rows that would collide
    # with an existing identical edge; those leftovers are deleted next).
    conn.execute(
        "UPDATE OR IGNORE edges SET to_id=(SELECT canonical FROM _idmap WHERE loser=edges.to_id) "
        "WHERE to_id IN (SELECT loser FROM _idmap)")
    conn.execute(
        "UPDATE OR IGNORE edges SET from_id=(SELECT canonical FROM _idmap WHERE loser=edges.from_id) "
        "WHERE from_id IN (SELECT loser FROM _idmap)")
    # Remove any edges still touching a loser (UNIQUE-conflict leftovers) + self-loops.
    conn.execute("DELETE FROM edges WHERE from_id IN (SELECT loser FROM _idmap) "
                 "OR to_id IN (SELECT loser FROM _idmap)")
    conn.execute("DELETE FROM edges WHERE from_id = to_id")
    # Drop the loser nodes + their FTS rows + their vectors.
    conn.execute("DELETE FROM nodes WHERE id IN (SELECT loser FROM _idmap)")
    _drop_derived_rows(conn, "_idmap", "loser")
    conn.execute("DROP TABLE IF EXISTS _idmap")
    conn.commit()

    paths_collapsed = len({c for _, c in mapping})
    losers = len(mapping)
    if verbose:
        print(f"[content_dedup:7a] collapsed {paths_collapsed} paths, removed {losers} "
              f"id-churn twins (recovery log → {log_path})")
    return {"paths_collapsed": paths_collapsed, "losers_removed": losers,
            "applied": True, "recovery_log": str(log_path)}


# ── 7c — stale skip-listed sweep (every pass with a COMPLETE walk) ───────────

def gc_stale_skiplisted(conn, seen_paths: set[str], verbose: bool = True,
                        apply: bool = True, recovery_log_dir=None,
                        db_path=None) -> dict:
    """Remove file-backed nodes a full walk no longer yields because their path became
    skip-listed (archive/backup/log dirs) or the file was deleted. reindex upserts
    walked files but never deletes unwalked ones, so backup copies indexed before the
    skip-list tightened linger as status=active and outrank live twins (Phase 8 eval
    regression: a Reorg_Backups copy of voice-mode-architecture beat the canonical note).

    Scope is deliberately tight — a node is swept only when it is BOTH absent from this
    pass's `seen_paths` AND matches util.should_skip_path. The first clause protects every
    file the walk legitimately yields, including Runtime-MAP nodes (walked directly via
    walk_runtime_map despite their ARCTURION_EXTERNAL_RUNTIME path) and harvested
    Session/Edit/Agent/Capability nodes (re-emitted each pass). HARD delete: these are
    non-canonical backup copies whose edges duplicate the live copy's.

    RUNS ON EVERY PASS, full or incremental, since 2026-08-05 (Leg 4) — the header of
    this module explains why the old `--full` gating meant it ran on no schedule at all.
    `seen_paths` is populated from the walk BEFORE the content-hash skip, so it is exactly
    as complete on an incremental pass as on a full one, which is the sole precondition
    this sweep leans on.

    NEVER call with a truncated walk — a partial `seen_paths` would wrongly sweep
    everything the file cap excluded. `graph_builder` enforces that with its `truncated`
    guard, and says so loudly on stderr when the cap is hit, because a sweep that silently
    stops running is how a privacy exclusion silently stops being enforced.

    Unattended and nightly, so like 7a: `apply=False` counts without writing, and every
    applied run writes a recovery log before the transaction opens."""
    from pathlib import Path as _P
    from ..util import should_skip_path
    rows = conn.execute("SELECT id, path FROM nodes WHERE path IS NOT NULL").fetchall()
    losers = [r["id"] for r in rows
              if r["path"] not in seen_paths and should_skip_path(_P(r["path"]))]
    if not losers:
        if verbose:
            print("[content_dedup:7c] no stale skip-listed nodes")
        return {"removed": 0, "applied": bool(apply), "recovery_log": None}

    if not apply:
        if verbose:
            print(f"[content_dedup:7c] DRY RUN — {len(losers)} stale skip-listed nodes "
                  f"would be removed")
        return {"removed": 0, "applied": False, "candidates": len(losers),
                "recovery_log": None}

    log_path = write_recovery_log(conn, "gc_stale_skiplisted", losers,
                                  log_dir=recovery_log_dir, db_path=db_path)

    conn.execute("DROP TABLE IF EXISTS _skipgc")
    conn.execute("CREATE TEMP TABLE _skipgc (id TEXT PRIMARY KEY)")
    conn.executemany("INSERT OR IGNORE INTO _skipgc (id) VALUES (?)", [(x,) for x in losers])
    conn.execute("DELETE FROM edges WHERE from_id IN (SELECT id FROM _skipgc) "
                 "OR to_id IN (SELECT id FROM _skipgc)")
    conn.execute("DELETE FROM nodes WHERE id IN (SELECT id FROM _skipgc)")
    _drop_derived_rows(conn, "_skipgc", "id")
    conn.execute("DROP TABLE IF EXISTS _skipgc")
    conn.commit()
    if verbose:
        print(f"[content_dedup:7c] removed {len(losers)} stale skip-listed nodes "
              f"(recovery log → {log_path})")
    return {"removed": len(losers), "applied": True, "recovery_log": str(log_path)}


# Node types that are HARVESTED (re-emitted each pass from live sources), not walked
# from a file on disk — their `path` is synthetic (a session transcript ref, an agent
# home, a capability class), so on-disk absence does NOT mean stale. 7d must never sweep
# these. Mirrors the seen_paths protection 7c leans on, but for harvested families that
# may not appear in a static-walk seen_paths set.
_HARVESTED_TYPES = {"Session", "Edit", "Handoff", "Agent", "Capability"}


def gc_missing_path(conn, seen_paths: set[str] | None = None,
                    dry_run: bool = True, verbose: bool = True) -> dict:
    """Phase 7d — remove file-backed nodes whose stored path no longer exists on disk.

    The reorg→Brain-v2 relocation moved/renamed thousands of files; reindex upserts the
    NEW path as a fresh node but never deletes the OLD-path node, so pre-reorg copies
    (e.g. `06_MEMORY_SYSTEM/...`, old `10_INFRASTRUCTURE/...`) linger as status=active with
    no content-twin to supersede against (so 7b skips them) and a non-archive path (so 7c
    skips them). This is the path-existence GC the brain-v2 residual flagged.

    Scope is deliberately narrow and triple-guarded:
      1. family ∈ {document, code, automation} — only files-on-disk nodes;
      2. type ∉ _HARVESTED_TYPES — never touch synthetic-path harvested nodes;
      3. the absolute path (DESKTOP_ROOT / stored relpath) does NOT exist;
      4. if `seen_paths` is given (a full, non-truncated walk), the path is also absent
         from it — belt-and-suspenders so a node walked THIS pass is never swept even if a
         race makes os.exists flap.

    PRONG-AWARE PATH RESOLUTION (critical): stored paths are relative to their SOURCE
    root, not a single root — mirroring extractors.markdown._rel_to_universe, which resolves
    against UNIVERSE_ROOT, then DESKTOP_ROOT, then WORK_ROOT. So this GC must check all
    THREE roots and count a node present if its path exists under ANY:
      - HQ-prong nodes store UNIVERSE_ROOT-relative paths (no `ARCTURION_HQ/` prefix, because
        the walker yields them relative to UNIVERSE_ROOT);
      - constellation/atlas/brain/runtime nodes store DESKTOP_ROOT-relative paths (their
        top-level dir IS the prefix);
      - the extra-roots (`~/.claude/plans|hooks`, audit 2026-06-16) store `~`-relative paths
        (`.claude/...`) that exist under NEITHER Desktop nor Universe — only under the configured work root.
    Check each configured root before treating a stored path as missing.

    HARD delete (the file is gone; the live content lives under its new-path node). Edges
    + FTS rows for the losers are removed in one temp-table pass (same mechanism as 7c).

    DEFAULTS TO dry_run=True — returns the would-remove count + a sample WITHOUT mutating.
    Call dry_run=False only on a quiescent DB (no concurrent reindex) AFTER a snapshot.
    Reversible only via the pre-run DB snapshot, exactly like 7a/7c."""
    from pathlib import Path as _P
    from .. import config as _C
    desk = _P(_C.DESKTOP_ROOT)
    uni = _P(_C.UNIVERSE_ROOT)
    home = _C.WORK_ROOT  # ~/.claude extra-roots store home-relative `.claude/...` paths
    seen = seen_paths or set()
    rows = conn.execute(
        "SELECT id, path, type, family FROM nodes WHERE path IS NOT NULL"
    ).fetchall()

    def _on_disk(rel: str) -> bool:
        # Resolve against ALL THREE roots (mirrors _rel_to_universe): UNIVERSE_ROOT (HQ),
        # DESKTOP_ROOT (other prongs), and home (~/.claude extra-roots). Present under any
        # ⇒ not stale. Three roots, not two — a two-root check false-flags live .claude nodes.
        return (desk / rel).exists() or (uni / rel).exists() or (home / rel).exists()

    losers = []
    for r in rows:
        if r["family"] not in ("document", "code", "automation"):
            continue
        if (r["type"] or "") in _HARVESTED_TYPES:
            continue
        p = r["path"]
        if p in seen:
            continue
        if _on_disk(p):
            continue
        losers.append(r["id"])

    sample = [r["path"] for r in rows if r["id"] in set(losers[:5])]
    if not losers:
        if verbose:
            print("[content_dedup:7d] no missing-path nodes")
        return {"removed": 0, "dry_run": dry_run, "sample": []}

    if dry_run:
        if verbose:
            print(f"[content_dedup:7d] DRY-RUN — {len(losers)} missing-path nodes "
                  f"would be removed (sample: {sample})")
        return {"removed": 0, "would_remove": len(losers),
                "dry_run": True, "sample": sample}

    conn.execute("DROP TABLE IF EXISTS _missgc")
    conn.execute("CREATE TEMP TABLE _missgc (id TEXT PRIMARY KEY)")
    conn.executemany("INSERT OR IGNORE INTO _missgc (id) VALUES (?)",
                     [(x,) for x in losers])
    conn.execute("DELETE FROM edges WHERE from_id IN (SELECT id FROM _missgc) "
                 "OR to_id IN (SELECT id FROM _missgc)")
    conn.execute("DELETE FROM nodes WHERE id IN (SELECT id FROM _missgc)")
    _drop_derived_rows(conn, "_missgc", "id")
    conn.execute("DROP TABLE IF EXISTS _missgc")
    conn.commit()
    if verbose:
        print(f"[content_dedup:7d] removed {len(losers)} missing-path nodes")
    return {"removed": len(losers), "dry_run": False, "sample": sample}


# ── 7b — cross-path content collapse (soft, supersede) ───────────────────────

def _canonical_idx_content(group: list[dict], pinned_ids: set[str] | None = None) -> int:
    """Pick canonical among same-content nodes. Priority:
      0. (Leg 1, 7.5) a fixture-pinned id (golden/heldout `expect`/`alternates`/
         `unexpected`) beats everything else — a pinned id must never be demoted out
         from under `retrieval_eval.check_expected_ids` when a winner can be chosen
         instead. Among multiple pinned ids in one group (rare — two alternates in the
         same content_hash bucket), lowest id wins for determinism.
      1. live path beats archive path (is_archive_path),
      2. a stable dateless id beats an old date-stamped id (the dateless one is the
         freshly-reindexed copy at the current path; the dated one is the stale
         pre-reorg/moved-file copy — this is the load-bearing tiebreak),
      3. newest last_modified.
    Without (2), identical file-mtimes tie and row order decides, which can wrongly
    supersede the fresh canonical node and keep the stale twin."""
    if pinned_ids:
        pinned_idxs = [i for i, n in enumerate(group) if n.get("id") in pinned_ids]
        if pinned_idxs:
            return min(pinned_idxs, key=lambda i: group[i]["id"])

    def key(n):
        return (
            1 if is_archive_path(n.get("path")) else 0,
            1 if _DATED_ID.match(n.get("id") or "") else 0,
            _neg_str(n.get("last_modified") or ""),
        )
    return min(range(len(group)), key=lambda i: key(group[i]))


def collapse_content_twins(conn, dry_run: bool = False, verbose: bool = True,
                           fixtures_dir: Path | None = None,
                           include_pairs: bool = False) -> dict:
    """`fixtures_dir` is forwarded to `_load_pinned_ids` (Leg 1, 7.5) so a fixture-pinned
    id is preferred as canonical here too, not only in 7e — 7b runs first in the
    documented Run order and would otherwise silently demote a pinned id (soft-superseded,
    so `check_expected_ids` still resolves it structurally, but without the explicit
    alias adoption 7e performs) before 7e ever sees it."""
    pinned_ids = _load_pinned_ids(fixtures_dir)
    rows = conn.execute(
        "SELECT id, title, path, content_hash, last_modified, status FROM nodes "
        "WHERE content_hash IS NOT NULL AND status != 'superseded'"
    ).fetchall()
    # group by (content_hash, lower(title)) — title match guards same-hash boilerplate.
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        n = dict(r)
        key = (n["content_hash"], (n.get("title") or "").strip().lower())
        groups[key].append(n)

    merges = []  # (canonical_id, loser_id)
    for key, group in groups.items():
        paths = {n.get("path") for n in group}
        if len(group) < 2 or len(paths) < 2:
            continue  # need a true cross-path duplicate
        ci = _canonical_idx_content(group, pinned_ids)
        canonical = group[ci]
        for i, n in enumerate(group):
            if i != ci:
                merges.append((canonical["id"], n["id"]))

    if dry_run:
        sample = [{"canonical": c, "loser": l} for c, l in merges[:20]]
        if verbose:
            print(f"[content_dedup:7b dry-run] groups={len(groups)} merges={len(merges)}")
            for s in sample:
                print(f"  • {s['loser']}  ⟶  {s['canonical']}")
        result = {"groups": len(groups), "merges": len(merges), "sample": sample}
        if include_pairs:
            result["pairs"] = [{"canonical": c, "loser": l} for c, l in merges]
        return result

    ts = now_iso()
    done = 0
    aliases_adopted: list[tuple[str, str]] = []
    for canonical_id, loser_id in merges:
        _reparent_edges(conn, loser_id, canonical_id)
        _emit_supersession_edge(conn, loser_id, canonical_id, ts)
        _mark_superseded(conn, loser_id)
        if loser_id in pinned_ids:
            _adopt_alias(conn, loser_id, canonical_id)
            aliases_adopted.append((loser_id, canonical_id))
        done += 1
        if done % 500 == 0:
            conn.commit()
    conn.commit()
    if verbose:
        print(f"[content_dedup:7b] superseded {done} cross-path content twins; "
              f"aliases adopted: {len(aliases_adopted)}")
    return {"groups": len(groups), "merges": len(merges), "superseded": done,
            "aliases_adopted": aliases_adopted}


# Applies to the configured graph and source inventory.

MIN_TITLE_TWIN_SIGNATURE_LEN = 30
MAX_DISTINCT_HASHES_PER_TITLE = 25

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
_FIXTURE_FILES = ("golden.jsonl", "heldout.jsonl")


def _load_pinned_ids(fixtures_dir: Path | None = None) -> set[str]:
    """Every node id referenced by expect/alternates/unexpected across the golden +
    held-out fixtures. Read-only file access — this does NOT run the eval, it just
    parses the same jsonl files `retrieval_eval.check_expected_ids` resolves against,
    so a fixture-pinned id is never picked as a demotion loser without the winner
    adopting it. Failure-tolerant: a missing/unreadable fixture file yields no pins
    for that file rather than raising, matching check_expected_ids' own fail-open."""
    d = fixtures_dir or _FIXTURES_DIR
    pinned: set[str] = set()
    for fname in _FIXTURE_FILES:
        p = d / fname
        if not p.exists():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                for key in ("expect", "alternates", "unexpected"):
                    pinned.update(entry.get(key) or [])
        except (OSError, json.JSONDecodeError):
            continue
    return pinned


def _title_twin_signature(n: dict) -> str:
    """The exact-match merge key: the stored body_excerpt, falling back to summary.
    Empty/short signatures never gate a merge (MIN_TITLE_TWIN_SIGNATURE_LEN) — an empty
    excerpt is not evidence of identical content, it is evidence of no excerpt."""
    sig = (n.get("body_excerpt") or "").strip()
    if len(sig) < MIN_TITLE_TWIN_SIGNATURE_LEN:
        sig = (n.get("summary") or "").strip()
    return sig


def _canonical_idx_title_group(group: list[dict], pinned_ids: set[str]) -> int:
    """Pick canonical among a same-title, cross-hash twin cluster. Priority:
      1. a fixture-pinned id (golden/heldout `expect`/`alternates`/`unexpected`) — never
         let a pinned id become a loser when a winner can be chosen instead; among
         multiple pinned ids in one cluster (rare), lowest id wins for determinism.
      2. live path over archive path,
      3. a stable dateless id over a date-stamped one,
      4. newest last_modified.
    Shares its tail three tiebreaks with `_canonical_idx_content` by design — same
    question ("which copy is the fresh one"), same answer."""
    pinned_idxs = [i for i, n in enumerate(group) if n.get("id") in pinned_ids]
    if pinned_idxs:
        return min(pinned_idxs, key=lambda i: group[i]["id"])
    return _canonical_idx_content(group)


def _adopt_alias(conn, loser_id: str, canonical_id: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO aliases (alias, canonical_id) VALUES (?, ?)",
        (loser_id, canonical_id),
    )


def collapse_title_twins(conn, dry_run: bool = False, verbose: bool = True,
                         fixtures_dir: Path | None = None,
                         include_pairs: bool = False) -> dict:
    """7e: collapse cross-hash same-title twins (see module-level docstring above for
    the full rationale and the measured false-positive analysis behind the exact-match
    gate). Restricted to family='document', non-harvested types, non-superseded, with a
    content_hash and a title — the same scope 7b/7d already operate in."""
    rows = conn.execute(
        "SELECT id, title, path, content_hash, body_excerpt, summary, last_modified, "
        "created FROM nodes WHERE content_hash IS NOT NULL AND title IS NOT NULL "
        "AND title != '' AND status != 'superseded' AND family = 'document' "
        f"AND type NOT IN ({','.join('?' * len(_HARVESTED_TYPES))})",
        tuple(_HARVESTED_TYPES),
    ).fetchall()

    by_title: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        n = dict(r)
        by_title[n["title"].strip().lower()].append(n)

    pinned_ids = _load_pinned_ids(fixtures_dir)
    merges: list[tuple[str, str]] = []   # (canonical_id, loser_id)
    groups_affected = 0
    skipped_oversized = 0

    for title, members in by_title.items():
        if len(members) < 2:
            continue
        by_hash: dict[str, list[dict]] = defaultdict(list)
        for n in members:
            by_hash[n["content_hash"]].append(n)
        if len(by_hash) < 2:
            continue  # single hash under this title — 7b's job, not ours
        if len(by_hash) > MAX_DISTINCT_HASHES_PER_TITLE:
            skipped_oversized += 1
            continue

        # One representative per hash, exact-signature union-find clustering.
        reps = [sorted(members_h, key=lambda n: n["id"])[0]
                for members_h in by_hash.values()]
        sig_to_hashes: dict[str, list[str]] = defaultdict(list)
        for rep in reps:
            sig = _title_twin_signature(rep)
            if len(sig) < MIN_TITLE_TWIN_SIGNATURE_LEN:
                continue
            sig_to_hashes[sig].append(rep["content_hash"])

        for sig, hashes in sig_to_hashes.items():
            if len(hashes) < 2:
                continue  # exact-match cluster of one hash — nothing to merge
            group = [n for h in hashes for n in by_hash[h]]
            ci = _canonical_idx_title_group(group, pinned_ids)
            canonical = group[ci]
            groups_affected += 1
            for i, n in enumerate(group):
                if i != ci:
                    merges.append((canonical["id"], n["id"]))

    if dry_run:
        sample = [{"canonical": c, "loser": l} for c, l in merges[:20]]
        if verbose:
            print(f"[content_dedup:7e dry-run] title_groups_merged={groups_affected} "
                  f"merges={len(merges)} skipped_oversized_titles={skipped_oversized}")
            for s in sample:
                print(f"  • {s['loser']}  ⟶  {s['canonical']}")
        result = {"groups": groups_affected, "merges": len(merges), "sample": sample,
                  "skipped_oversized_titles": skipped_oversized}
        if include_pairs:
            result["pairs"] = [{"canonical": c, "loser": l} for c, l in merges]
        return result

    ts = now_iso()
    done = 0
    aliases_adopted: list[tuple[str, str]] = []
    for canonical_id, loser_id in merges:
        _reparent_edges(conn, loser_id, canonical_id)
        _emit_supersession_edge(conn, loser_id, canonical_id, ts)
        _mark_superseded(conn, loser_id)
        if loser_id in pinned_ids:
            _adopt_alias(conn, loser_id, canonical_id)
            aliases_adopted.append((loser_id, canonical_id))
        done += 1
        if done % 500 == 0:
            conn.commit()
    conn.commit()
    if verbose:
        print(f"[content_dedup:7e] superseded {done} cross-hash same-title twins "
              f"across {groups_affected} groups; aliases adopted: {len(aliases_adopted)}")
    return {"groups": groups_affected, "merges": len(merges), "superseded": done,
            "skipped_oversized_titles": skipped_oversized,
            "aliases_adopted": aliases_adopted}


def duplicate_rate(conn) -> dict:
    """The dedup-progress metric for this leg: what fraction of in-scope active
    document nodes are demotable duplicates RIGHT NOW, by content_dedup's own detectors
    (7b's content_hash+title groups, plus 7e's cross-hash same-title groups above).
    `rate = merges / (merges + canonical_survivors)` — merges is exactly the count 7b/7e
    would each report as `merges` in a dry run; canonical_survivors is every node NOT
    flagged as a loser by either detector. Both detectors are read-only here (dry_run
    always), so calling this never mutates the index."""
    b = collapse_content_twins(conn, dry_run=True, verbose=False)
    e = collapse_title_twins(conn, dry_run=True, verbose=False)
    total = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE content_hash IS NOT NULL AND status != 'superseded'"
    ).fetchone()[0]
    dup_nodes = b["merges"] + e["merges"]
    rate = (dup_nodes / total) if total else 0.0
    return {"total_active_with_hash": total, "content_dedup_7b_merges": b["merges"],
            "title_twins_7e_merges": e["merges"], "duplicate_nodes": dup_nodes,
            "duplicate_rate": rate}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gc", action="store_true", help="7a: collapse same-path id-churn twins")
    ap.add_argument("--dry-run", action="store_true",
                    help="preview only, write nothing (applies to 7a with --gc, else 7b)")
    ap.add_argument("--missing-path", action="store_true",
                    help="7d: preview missing-on-disk nodes (dry-run)")
    ap.add_argument("--missing-path-apply", action="store_true",
                    help="7d: DELETE missing-on-disk nodes — quiescent DB + snapshot ONLY")
    ap.add_argument("--stale-skiplisted", action="store_true",
                    help="7c: preview the privacy/skip-list sweep (dry-run unless "
                         "--stale-skiplisted-apply). Performs a real walk first so the "
                         "seen-path protection is honest.")
    ap.add_argument("--stale-skiplisted-apply", action="store_true",
                    help="7c: DELETE stale skip-listed nodes — snapshot the DB first")
    ap.add_argument("--title-twins", action="store_true",
                    help="7e: collapse cross-hash same-title twins (soft supersede, "
                         "reversible). Dry-run unless combined with --apply.")
    ap.add_argument("--apply", action="store_true",
                    help="with --title-twins: actually write (default is dry-run)")
    ap.add_argument("--duplicate-rate", action="store_true",
                    help="print the current content_dedup duplicate-rate metric "
                         "(7b + 7e detectors, read-only) and exit")
    args = ap.parse_args()
    conn = DB.connect()
    try:
        if args.stale_skiplisted or args.stale_skiplisted_apply:
            # 7c is the pass that enforces EXCLUDED_AGENTS, and until now it was
            # reachable ONLY from inside a reindex — so its blast radius could not be
            # rehearsed from a shell, which is the same problem B-4 fixed for 7a/7b/7d.
            #
            # It takes a REAL walk rather than an empty seen_paths set. `seen_paths` is
            # the clause that protects every file the walk legitimately yields (the
            # Runtime-MAP nodes above all), so previewing with an empty set would report
            # a blast radius wildly larger than the nightly's and teach the operator to
            # distrust the preview. The walk costs ~1.5 s and needs no extraction: the
            # stored-path spelling comes from `_rel_to_universe`, exactly as
            # graph_builder derives it for a file whose extractor failed.
            from .. import walker
            from ..extractors.markdown import _rel_to_universe
            seen = {_rel_to_universe(p)
                    for p, _f, _t in walker.walk_universe_and_constellation()}
            print(f"[content_dedup:7c] walk yielded {len(seen)} stored paths")
            print(gc_stale_skiplisted(conn, seen,
                                      apply=bool(args.stale_skiplisted_apply)))
        elif args.gc:
            print(gc_same_path_twins(conn, apply=not args.dry_run))
        elif args.missing_path or args.missing_path_apply:
            print(gc_missing_path(conn, dry_run=not args.missing_path_apply))
        elif args.duplicate_rate:
            print(duplicate_rate(conn))
        elif args.title_twins:
            print(collapse_title_twins(conn, dry_run=not args.apply))
        else:
            print(collapse_content_twins(conn, dry_run=args.dry_run))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
