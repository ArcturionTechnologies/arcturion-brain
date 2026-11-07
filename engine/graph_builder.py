"""Orchestrator — walker → extractors → SQLite → inverses → JSONL export.

Item 11: extractor pass runs in a multiprocessing.Pool by default. The walker
emits (path, family, node_type) tuples; workers call `route()` and return
(path, node, edges, error). Main process serializes all DB writes inside a
single transaction to avoid SQLite WAL contention.
"""

from __future__ import annotations
import os
import sys
import time
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path
from . import config as C
from . import db as DB
from . import walker
from .extractors import route
from .aliases import load_aliases, synonymous_edges
from .path_edges import derive_path_edges
from .inverse import derive_inverses
from .jsonl_writer import export_triples_jsonl, export_node_index_jsonl, export_graph_json
from .util import now_iso
from .telemetry import extraction_current

PARALLEL_FLOOR = 200      # below this, run serially (overhead > savings)
DEFAULT_CHUNKSIZE = 50
DEFAULT_WORKERS = min(8, os.cpu_count() or 4)


def _extract_one(arg: tuple):
    """Worker entrypoint. Returns (path_str, node | None, edges | [], err | None).

    Lives at module top so workers spawned under macOS 'spawn' start method
    can resolve it. Workers exchange only simple dicts/strings/None with the
    parent — no SQLite handles or other unsafe objects cross the boundary.
    """
    path, family, node_type = arg
    try:
        node, edges = route(path, family, node_type)
        if node is not None:
            # Four-Prong Canon (2026-06-09): tag every node with its prong.
            node["prong"] = C.prong_for_path(path)
        return (str(path), node, edges, None)
    except Exception as e:
        return (str(path), None, [], f"{type(e).__name__}: {e}")


def _inbox(heading: str, lines: list[str]) -> None:
    """Deliver one governance block to the COVENANT inbox, best-effort.

    Lazy import: `retrieval_eval` imports `brain`, and `brain` must not be dragged into
    every reindex to make an alert that fires approximately never. Never fatal — a
    completed reindex must not become a crash because the inbox is unwritable — but never
    silent either; `_append_inbox` echoes an undeliverable block to stderr itself."""
    try:
        from .retrieval_eval import _append_inbox
        _append_inbox(heading, lines)
    except Exception as exc:  # noqa: BLE001 — alerting must not fail the reindex
        print(f"[brain] could not deliver a governance alert to the COVENANT inbox "
              f"({type(exc).__name__}: {exc}); the condition is reported above.",
              file=sys.stderr)


def _alert_privacy_sweep_skipped(what: str, why: str) -> None:
    """Alert when a sweep that enforces EXCLUDED_AGENTS did not run.

    A distinct heading from the cap alert on purpose. "The walk was truncated" and "the
    purge crashed" have the same consequence but different fixes, and conflating them
    sends the reader to the wrong lever."""
    _inbox(
        f"### 🚨 Brain privacy sweep DID NOT RUN — {now_iso()}",
        [f"- `{what}` failed: `{why}`",
         "- That pass is what retires rows an earlier harvest indexed from sessions "
         "belonging to `config.EXCLUDED_AGENTS` (working-directory route, in-band "
         "persona route, and their derived distillations).",
         "- Until it runs cleanly, such rows can persist in the shared index and be "
         "returned by `brain_query`. The reindex itself completed; only the sweep is "
         "missing.",
         "- Fix: `python3 -m engine.harvesters.sessions` (it is idempotent), then "
         "confirm with the ids in the last recovery log.",
         "- log: `~/Library/Logs/arcturion/brain-walker.err`"],
    )


def _alert_cap_reached(limit: int, walked: int) -> None:
    """Raise the file-cap crossing to the COVENANT inbox — a channel with readers.

    Deliberately reuses `retrieval_eval._append_inbox`, the primitive this repo already
    uses for governance alerts, instead of minting a second notification path. Imported
    lazily: `retrieval_eval` imports `brain`, and `brain` must not be pulled into every
    reindex just to make an alert that fires approximately never.

    Best-effort and never fatal — a reindex that completed must not be turned into a
    crash by an unwritable inbox — but never silent either: `_append_inbox` already
    echoes an undeliverable block to stderr, which is the same discipline
    `retrieval_eval` applies to its own alerts."""
    _inbox(
        f"### 🚨 Brain reindex CAP REACHED — {now_iso()}",
        [f"- The walk hit `config.MAX_FILES_PER_PASS` ({limit:,}) after {walked:,} "
         f"files and was TRUNCATED.",
         "- Because the walk is incomplete, the stale skip-listed sweep "
         "(`content_dedup.gc_stale_skiplisted`) was **skipped**.",
         "- **That sweep is what enforces `config.EXCLUDED_AGENTS` against rows an "
         "earlier pass indexed.** Until it runs again, an excluded agent's rows "
         "(e.g. PRIVATE_AGENT) can persist in the shared index and be returned by "
         "`brain_query`. This is a privacy guarantee, not a hygiene nicety.",
         "- Fix: raise `MAX_FILES_PER_PASS` in `engine/config.py` above the live walk "
         "size, then run `python3 -m engine.graph_builder` to let the sweep catch up.",
         "- run: `python3 -m engine.reindex` · log: "
         "`~/Library/Logs/arcturion/brain-walker.err`"],
    )


# Applies to the configured graph and source inventory.
CAP_WARN_FRACTION = 0.80


def _warn_cap_approaching(limit: int, walked: int) -> str | None:
    """Report (and alert) when the walk is nearing the file cap. Returns the message."""
    if not limit or walked < limit * CAP_WARN_FRACTION:
        return None
    pct = 100.0 * walked / limit
    msg = (f"walk is at {walked:,} of the {limit:,} file cap ({pct:.0f}%). Crossing it "
           f"TRUNCATES the walk, which SKIPS the stale skip-listed sweep and stops "
           f"config.EXCLUDED_AGENTS being enforced against already-indexed rows. Raise "
           f"config.MAX_FILES_PER_PASS now, while it is still a warning.")
    print(f"[brain] *** {msg}", file=sys.stderr)
    _inbox(
        f"### ⚠️ Brain reindex approaching the file cap — {now_iso()}",
        [f"- The walk yielded {walked:,} files against a cap of {limit:,} ({pct:.0f}%).",
         "- Nothing is broken yet. This fires BEFORE the crossing precisely because the "
         "crossing is silent in its consequences: a truncated walk skips the sweep that "
         "enforces `config.EXCLUDED_AGENTS` against rows an earlier pass indexed.",
         "- Fix: raise `MAX_FILES_PER_PASS` in `engine/config.py`.",
         "- log: `~/Library/Logs/arcturion/brain-walker.err`"],
    )
    return msg


def _resolve_frontmatter_owners(conn) -> dict:
    """Resolve only explicit owner aliases; preserve originals and caller transaction."""
    from .maintenance.edge_endpoint_resolve import run
    return run(
        conn, ["owned_by"], asserted_by=["STATIC_FRONTMATTER"],
        evidence_spans=["frontmatter:owner"], mechanisms=["alias_table"], commit=False,
    )


# Write-lock sharing during the static pass: hold at most this long, then yield.
WRITE_SLICE_S = 2.0
WRITE_YIELD_S = 0.25


def reindex(full: bool = False, max_files: int | None = None, verbose: bool = True,
            parallel: bool = True, workers: int | None = None) -> dict:
    t0 = time.time()
    conn = DB.connect()
    DB.init_db(conn)

    if verbose:
        print(f"[brain] reindex start (full={full}, parallel={parallel}) at {now_iso()}")

    seen = 0
    upserted = 0
    skipped_hash = 0
    edges_inserted = 0
    failures = 0
    refused = 0                    # records the Leg-4 admission gate declined to index
    stale_sweep_skipped = None     # non-None ⇒ exclusions are NOT being enforced tonight
    session_harvest_failed = None  # non-None ⇒ the excluded-agent purge did not run
    cap_warning = None             # non-None ⇒ the file cap is close enough to act on
    seen_paths: set[str] = set()   # stored paths walked this pass — drives the 7c stale sweep

    # Where the hoisted GC passes write their recovery logs — same shelf Leg 1's
    # skiplist_gc uses, so there is one place to look after an unattended 02:30 delete.
    _GC_RECOVERY_DIR = C.BRAIN_ROOT / "backups"

    # Admission gate + skip ledger (Leg 4). Imported from engine.ingest deliberately:
    # that module is the declared chokepoint, and importing it here is what makes the
    # walker and the watcher share ONE gate instead of two that drift.
    from .ingest import (admit_node as _admit_node, record_skip as _record_skip,
                         ensure_skip_ledger as _ensure_skip_ledger)
    have_ledger = _ensure_skip_ledger(conn)

    cur_metadata = {r["path"]: r["metadata"] for r in conn.execute("SELECT path,metadata FROM nodes")}
    if not full:
        cur_hashes = {
            r["path"]: r["content_hash"]
            for r in conn.execute("SELECT path, content_hash FROM nodes").fetchall()
            if r["path"]
        }
    else:
        cur_hashes = {}

    limit = max_files or C.MAX_FILES_PER_PASS

    # Materialize the walk into a list so we can fan out to workers and
    # respect the file cap deterministically.
    walk_items: list = []
    truncated = False
    for triple in walker.walk_universe_and_constellation():
        walk_items.append(triple)
        if len(walk_items) >= limit:
            if verbose:
                print(f"[brain] file cap {limit} reached; stopping walk")
            truncated = True
            break

    seen = len(walk_items)
    if verbose:
        print(f"[brain] walk yielded {seen} files; extracting…")

    # Warn while there is still time to act, not only once the cap has already disabled
    # the exclusion sweep. Skipped when the caller passed an explicit `max_files` — a
    # deliberately small run (a smoke test) is not a capacity signal about the real cap.
    if max_files is None and not truncated:
        cap_warning = _warn_cap_approaching(limit, seen)

    use_pool = parallel and seen > PARALLEL_FLOOR
    n_workers = workers or DEFAULT_WORKERS

    def _seen_rel_for(path_str):
        """The stored (universe-relative) path for a file whose extractor FAILED.

        The successful path reads `node["path"]`, which the extractors compute with
        `_rel_to_universe`. A failed extraction has no node, so the same helper is called
        directly — using anything else here (the absolute path, say) would put a string in
        `seen_paths` that never matches `nodes.path`, i.e. protection that silently does
        nothing. Best-effort: if even this raises, we protect nothing, which is the old
        behaviour, not a new hazard."""
        try:
            from .extractors.markdown import _rel_to_universe
            return _rel_to_universe(Path(path_str))
        except Exception:  # noqa: BLE001 — protection is best-effort, never fatal
            return None

    def _watermark(path_str: str, rel: str | None, node: dict | None,
                   *, success: bool, error: str | None = None) -> None:
        """Persist file-source proof; failures never advance the last success."""
        try:
            mtime_ns = Path(path_str).stat().st_mtime_ns
        except OSError:
            mtime_ns = None
        source_path = rel or _seen_rel_for(path_str) or path_str
        DB.record_source_watermark(
            conn,
            source_key=f"file:{source_path}",
            source_kind="file",
            source_path=source_path,
            observed_at=now_iso(),
            success=success,
            content_hash=(node or {}).get("content_hash"),
            source_mtime_ns=mtime_ns,
            node_id=(node or {}).get("id"),
            error=error,
        )

    def _consume(results_iter):
        nonlocal upserted, edges_inserted, failures, skipped_hash, refused
        last_commit = time.time()
        for i, (path_str, node, edges, err) in enumerate(results_iter, 1):
            # Share the write lock (2026-11-07). The commit below only fires on the upsert
            # path, so a long run of hash-skipped or refused files kept one write txn
            # (their watermarks) open for minutes, and a commit followed at once by the next
            # write never let another writer in: the memory watcher, MCP propose/capture and
            # CLI ingest all failed "database is locked" for the whole walk. Commit on time
            # on EVERY path, then pause briefly so a waiting writer's busy handler wins.
            if (time.time() - last_commit) > WRITE_SLICE_S:
                conn.commit()
                time.sleep(WRITE_YIELD_S)
                last_commit = time.time()
            if err is not None or node is None:
                failures += 1
                # SEEN BEFORE THE CONTINUE. `seen_paths` is the ONLY thing protecting a
                # walked-but-skiplisted file from the 7c stale sweep's hard delete (the
                # Runtime-MAP nodes are the standing example), and the sweep now runs
                # every night rather than on a `--full` nobody ran. With the record
                # happening after this `continue`, one transient extractor error — a
                # locked file, a read racing an editor, the .numbers plist that fails on
                # every pass — deleted that live node the same night. The walk DID yield
                # the path; whether its extractor succeeded is a separate question.
                failed_rel = _seen_rel_for(path_str)
                if failed_rel:
                    seen_paths.add(failed_rel)
                _watermark(path_str, failed_rel, node, success=False,
                           error=err or "extractor-returned-no-node")
                if verbose and failures < 10:
                    print(f"[brain] extract failed for {path_str}: {err}", file=sys.stderr)
                continue
            rel = node.get("path")
            if rel:
                # Recorded even for records the admission gate then refuses: the walk
                # DID yield this path, and `seen_paths` is what protects a path from
                # the 7c stale sweep. A refused path must not become a deletion
                # candidate — refusing to ADD a row is not the same as ordering an
                # existing one destroyed.
                seen_paths.add(rel)

            extract_error = (node.get("metadata") or {}).get("extract_error")
            if extract_error:
                failures += 1
                DB.upsert_node(conn, node)
                _watermark(path_str, rel, node, success=False, error=str(extract_error))
                if verbose and failures < 10:
                    print(f"[brain] invalid extracted source {path_str}: {extract_error}",
                          file=sys.stderr)
                continue

            # Admission gate (Leg 4) — the same function engine.ingest applies, so the
            # nightly walker and the live watcher cannot disagree about what earns a
            # row. Evaluated BEFORE the content-hash skip on purpose: the verdict then
            # depends only on the record, never on whether this particular pass happened
            # to see it change.
            ok, reason, facts = _admit_node(conn, node, path_str)
            if not ok:
                refused += 1
                if have_ledger:
                    _record_skip(conn, node, reason, facts)
                if verbose and refused < 10:
                    print(f"[brain] refused {rel}: {reason}", file=sys.stderr)
                _watermark(path_str, rel, node, success=False,
                           error=f"admission-refused:{reason}")
                continue


            # Equality with the indexed hash is positive evidence that this source was
            # observed and the index already carries its current bytes.
            if not full and rel in cur_hashes and cur_hashes[rel] == node.get("content_hash") and extraction_current(node, cur_metadata.get(rel)):
                _watermark(path_str, rel, node, success=True)
                skipped_hash += 1
                continue
            DB.upsert_node(conn, node)
            upserted += 1
            for e in edges:
                if DB.insert_edge(conn, e):
                    edges_inserted += 1
            # Mark success only after node/edge admission in the same transaction.
            _watermark(path_str, rel, node, success=True)
            if i % 500 == 0 or (time.time() - last_commit) > 5:
                conn.commit()
                last_commit = time.time()
                if verbose:
                    elapsed = time.time() - t0
                    print(f"[brain] {i}/{seen} processed | {upserted} upserted | "
                          f"{edges_inserted} edges | {elapsed:.1f}s")

    if use_pool:
        with Pool(n_workers) as pool:
            _consume(pool.imap_unordered(_extract_one, walk_items, chunksize=DEFAULT_CHUNKSIZE))
    else:
        _consume(map(_extract_one, walk_items))

    conn.commit()

    if verbose:
        print(f"[brain] static pass complete — {seen} seen, {upserted} upserted")

    # Apply the configured exclusion policy.
    try:
        from .maintenance.content_dedup import gc_same_path_twins
        _gc = gc_same_path_twins(conn, verbose=verbose,
                                 recovery_log_dir=_GC_RECOVERY_DIR)
        conn.commit()
        if verbose:
            print(f"[brain] same-path GC → {_gc['losers_removed']} twins removed "
                  f"across {_gc['paths_collapsed']} paths"
                  + (f" (recovery log → {_gc['recovery_log']})"
                     if _gc.get("recovery_log") else ""))
    except Exception as _e:
        print(f"[brain] same-path GC FAILED: {_e}", file=sys.stderr)

    # Stale skip-listed sweep (Phase 7c) — any pass with a COMPLETE walk. The static pass
    # upserts what it walked but never deletes nodes whose path dropped out of the walk set
    # (archive/backup dirs the skip-list now rejects, EXCLUDED_AGENTS paths, or deleted
    # files), so those linger as status=active and can outrank live twins. Tightly scoped
    # to nodes absent from seen_paths AND matching should_skip_path.
    #
    # The `full` half of the old guard was dropped 2026-08-05 (Leg 4) — see the note on 7a
    # above; `not truncated` is the guard that actually matters and it is kept. This sweep
    # is what gives the EXCLUDED_AGENTS rule its teeth: the rule stops the walk from
    # yielding those paths, and this pass removes the rows a previous walk left behind. On
    # a `--full`-only schedule that never fires, "excluded" meant "excluded from tonight's
    # walk and still fully queryable" — which is not an exclusion.
    #
    # Both clauses stay correct on an incremental pass: `seen_paths` is populated from the
    # walk BEFORE the content-hash skip, so it is exactly as complete as on a full pass
    # (verified — an unchanged file is hash-skipped for UPSERT, never for seen_paths).
    if truncated:
        # `not truncated` is this sweep's only guard. The night the walk crosses the cap,
        # the sweep stops running, and the FIRST thing that stops being true is the
        # EXCLUDED_AGENTS exclusion: rows a previous pass indexed are never removed again.
        # That is this leg's landmine re-arming itself, at scale.
        #
        # It goes to the COVENANT INBOX, not merely to stderr. Round 1 printed it to
        # `brain-walker.err` and put it in the returned stats — and the re-review checked:
        # that .err file has writers and no readers (it currently holds 272 lines of
        # `<frozen>` import noise, so a real signal would be buried in it), and the stats
        # dict's only consumer is `main()`'s print. "Louder than silence" is not the bar;
        # reaching a human or a monitor is. `retrieval_eval._append_inbox` is the channel
        # this repo already uses for exactly this failure class, so the signal rides it
        # rather than inventing a second one. stderr is kept as the belt-and-braces copy.
        msg = (f"CAP REACHED ({limit} files) — walk truncated, so the stale skip-listed "
               f"sweep is SKIPPED. Privacy/skip-list exclusions (config.EXCLUDED_AGENTS) "
               f"are NOT being enforced against rows a previous pass indexed. Raise "
               f"config.MAX_FILES_PER_PASS above the live walk size and re-run.")
        print(f"[brain] *** {msg}", file=sys.stderr)
        stale_sweep_skipped = msg
        _alert_cap_reached(limit, seen)
    else:
        try:
            from .maintenance.content_dedup import gc_stale_skiplisted
            _sg = gc_stale_skiplisted(conn, seen_paths, verbose=verbose,
                                      recovery_log_dir=_GC_RECOVERY_DIR)
            conn.commit()
            if verbose:
                print(f"[brain] stale skip-listed sweep → {_sg['removed']} removed"
                      + (f" (recovery log → {_sg['recovery_log']})"
                         if _sg.get("recovery_log") else ""))
        except Exception as _e:
            # stderr, not stdout: a sweep that silently stopped running is the same
            # failure class as one that was never scheduled.
            print(f"[brain] stale skip-listed sweep FAILED: {_e}", file=sys.stderr)
            stale_sweep_skipped = f"failed: {_e}"

    # Agent anchors must exist BEFORE aliases, inference, and session harvest.  Those
    # producers resolve labels such as "ALTAIR"; if anchors are harvested later, an
    # exact-title document can win and ownership/authorship edges get a document endpoint.
    try:
        from .harvesters import agents as _agents
        _ag = _agents.harvest()
        conn.commit()
        if verbose:
            print(f"[brain] agent anchors → {_ag['nodes']} agent nodes")
    except Exception as _e:
        if verbose:
            print(f"[brain] agent anchor harvest skipped: {_e}")

    # Aliases → synonymous_with edges
    canonical_to_aliases, alias_to_canonical = load_aliases()
    DB.replace_aliases(conn, alias_to_canonical)
    syn = synonymous_edges(canonical_to_aliases)
    syn_count = 0
    for e in syn:
        if DB.insert_edge(conn, e):
            syn_count += 1
    from .code_references import refresh as refresh_code_references
    code_references = refresh_code_references(conn)
    owners = _resolve_frontmatter_owners(conn)
    conn.commit()
    if verbose:
        print(f"[brain] aliases → {syn_count} synonymous_with edges; "
              f"frontmatter owners → {owners['inserted_edges']} canonical twins")

    # Path-hierarchy edges
    path_count = derive_path_edges(conn)
    conn.commit()
    if verbose:
        print(f"[brain] path hierarchy → {path_count} part_of edges")

    # Typed-edge inference + resolution (Brain v2 Phase 3): re-point dangling typed
    # edges (owned_by "ALTAIR" → agent_altair) at real nodes so they're traversable,
    # and infer validated_by from test files. Idempotent; INFERRED_* asserted_by.
    try:
        from .edge_inference import run as _infer_run
        _inf = _infer_run(conn, verbose=verbose)
        conn.commit()
        if verbose:
            print(f"[brain] edge inference → {_inf['resolved_total']} resolved, "
                  f"{_inf['validated_by']} validated_by")
    except Exception as _e:
        if verbose:
            print(f"[brain] edge inference skipped: {_e}")

    # Cross-agent session + temporal layer (Brain v2 Phase 4): harvest each agent's
    # Memory/Sessions/raw/*.jsonl into Session/Edit nodes + touched_by/modified_by/
    # changed_in_session/discussed_in edges, so a concept query surfaces who touched it
    # and when. Idempotent; runs after the resolver index exists. Guarded — a parse
    # error never breaks a reindex.
    #
    # The `except` below is no longer a swallow. This harvest also carries
    # `purge_excluded_sessions`, the sweep that retires excluded-agent rows an earlier
    # pass indexed — a PRIVACY control, not hygiene. Its failure notice sat inside
    # `if verbose: print(...)`, so a failed harvest was a night those rows stayed
    # queryable with nothing saying so. Same reasoning already accepted for the recovery
    # log: failing to protect must be loud. The reindex still continues (one broken
    # transcript must not abort the pass), but the failure now goes to stderr AND to the
    # COVENANT inbox, and is carried in the returned stats.
    try:
        from .harvesters import sessions as _sessions
        _sh = _sessions.harvest(conn, verbose=verbose)
        conn.commit()
        if verbose:
            print(f"[brain] session harvest → {_sh['sessions']} sessions, "
                  f"{_sh['edits']} edits, {_sh['edges']} edges"
                  + (f", {_sh['excluded_rows_purged']} excluded rows purged"
                     if _sh.get("excluded_rows_purged") else ""))
    except Exception as _e:
        session_harvest_failed = f"{type(_e).__name__}: {_e}"
        print(f"[brain] *** session harvest FAILED ({session_harvest_failed}) — the "
              f"excluded-agent purge did NOT run this pass, so rows an earlier pass "
              f"indexed may still be queryable.", file=sys.stderr)
        _alert_privacy_sweep_skipped(
            "session harvest / excluded-agent purge", session_harvest_failed)

    # Capability registry (Brain v2 Phase 5): index skills + MCP connectors as queryable
    # Capability nodes so agents lazy-load only what a subject needs. Reads live sources
    # (SKILL.md files + ~/.claude.json), never copies them. Idempotent; guarded.
    try:
        from . import registry_harvester as _registry
        _rh = _registry.harvest(conn, verbose=verbose)
        conn.commit()
        if verbose:
            print(f"[brain] capability registry → {_rh['nodes']} capabilities, "
                  f"{_rh['edges']} edges")
    except Exception as _e:
        if verbose:
            print(f"[brain] capability registry skipped: {_e}")

    # Derive inverses LAST so every edge producer above receives a canonical inverse in
    # the same pass.  This also means ownership inverses are derived only after agent
    # anchors exist and label resolution has completed.
    inv_count = derive_inverses(conn)
    conn.commit()
    if verbose:
        print(f"[brain] inverse derivation → {inv_count} inverse edges")

    # Export
    triples_count = export_triples_jsonl(conn)
    node_count = export_node_index_jsonl(conn)
    gj = export_graph_json(conn)
    if verbose:
        print(f"[brain] exported: triples_jsonl={triples_count} nodes_jsonl={node_count} graph_json={gj}")

    stats = DB.stats(conn)
    stats.update({
        "walk_seen": seen,
        "upserted": upserted,
        "skipped_hash": skipped_hash,
        "refused": refused,
        # Surfaced in the returned stats, not only on stderr, so the nightly's own log
        # line records that tonight's pass did not enforce the skip/privacy rules.
        "stale_sweep_skipped": stale_sweep_skipped,
        "session_harvest_failed": session_harvest_failed,
        "cap_warning": cap_warning,
        "edges_inserted": edges_inserted + syn_count + path_count + inv_count,
        "failures": failures,
        "elapsed_sec": round(time.time() - t0, 2),
        "finished_at": now_iso(),
    })
    # Kept out of ordinary graph traversal, exports, and maintenance. A direct
    # graph_builder invocation receives the same explicit opt-in as the nightly.
    from . import vega_index
    if vega_index.enabled() and max_files is None:
        stats["vega_desk"] = vega_index.auto_sync(conn)
        conn.commit()
    conn.close()
    return stats


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="ignore content hashes; reprocess all")
    ap.add_argument("--max-files", type=int, default=None)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-parallel", action="store_true",
                    help="disable multiprocessing extractor pool (Item 11)")
    ap.add_argument("--workers", type=int, default=None)
    args = ap.parse_args()
    stats = reindex(
        full=args.full, max_files=args.max_files,
        verbose=not args.quiet,
        parallel=not args.no_parallel, workers=args.workers,
    )
    print()
    print("== brain stats ==")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
