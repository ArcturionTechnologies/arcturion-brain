"""Phase 2 orchestrator — run all 5 harvesters in priority order."""

from __future__ import annotations
import contextlib
import hashlib
import json
import time
from .. import db as DB
from ..util import now_iso
from . import agents, applications, events, external, concepts


_VOLATILE_RESULT_KEYS = {"elapsed_sec", "observed_at", "generated_at", "finished_at"}


def result_watermark(result: dict) -> str:
    """Return a producer-supplied digest of actual source content/state.

    Counts are deliberately insufficient: two different source states can create the same
    number of nodes.  A producer without this contract is stale/unknown, never fresh.
    """
    digest = result.get("source_digest") or result.get("event_watermark")
    return str(digest or "")


def _record_harvester_watermark(source_key: str, *, success: bool,
                                content_hash: str = "", event_watermark=None,
                                error: str | None = None,
                                scope_id: str | None = None) -> None:
    """Persist freshness evidence or raise; stale success must never survive silently."""
    conn = None
    try:
        conn = DB.connect()
        DB.init_db(conn)
        scoped_key = f"harvester:{source_key}"
        scoped_path = f"engine.harvesters.{source_key}"
        if scope_id is not None:
            scoped_key += f":node:{scope_id}"
            scoped_path += f"#node_id={scope_id}"
        DB.record_source_watermark(
            conn, source_key=scoped_key,
            source_kind="event" if source_key == "events" else "harvester",
            source_path=scoped_path, observed_at=now_iso(),
            success=success, content_hash=content_hash,
            event_watermark=event_watermark, error=error)
        conn.commit()
    except Exception:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.rollback()
        raise
    finally:
        if conn is not None:
            conn.close()


def main(concepts_limit: int = 500, concepts_source_id: str | None = None):
    if concepts_source_id is not None and (
            not isinstance(concepts_source_id, str)
            or not concepts_source_id
            or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-" for ch in concepts_source_id)):
        raise ValueError("concepts_source_id must be a non-empty exact node ID")
    t0 = time.time()
    results = []
    phase_label = ("PHASE 2 — CONCEPT SOURCE HARVEST"
                   if concepts_source_id is not None
                   else "PHASE 2 — UNIVERSAL FAMILY HARVEST")
    print("=" * 70)
    print(phase_label)
    print("=" * 70)

    if concepts_source_id is None:
        sources = [
            ("agents", "Agent/Workspace", lambda: agents.harvest()),
            ("applications", "Application/Service", lambda: applications.harvest()),
            ("events", "Event/Task", lambda: events.harvest()),
            ("external", "External/Bookmark", lambda: external.harvest()),
            ("concepts", "Concept/Entity (NER)", lambda: concepts.harvest(limit=concepts_limit)),
        ]
    else:
        # Scoped acceptance runs only Concepts for this exact public node; other
        # harvesters and the corpus-wide Concepts watermark remain untouched.
        sources = [
            ("concepts", "Concept/Entity (exact source)", lambda: concepts.harvest(source_id=concepts_source_id)),
        ]

    for source_key, name, fn in sources:
        scope_id = concepts_source_id if source_key == "concepts" else None
        print(f"\n--- {name} ---")
        try:
            result = fn()
        except Exception as exc:
            print(f"[ERROR] {name}: {exc}")
            results.append({"family": name, "error": str(exc)})
            try:
                _record_harvester_watermark(
                    source_key, success=False,
                    error=f"{type(exc).__name__}: {exc}", scope_id=scope_id)
            except Exception as persist_exc:
                raise RuntimeError(
                    f"cannot persist failed freshness for {source_key}: "
                    f"{type(persist_exc).__name__}: {persist_exc}") from persist_exc
            continue

        results.append(result)
        digest = result_watermark(result)
        success = (not bool(result.get("error"))
                   and result.get("status", "ok") in {"ok", "success"}
                   and bool(digest))
        try:
            _record_harvester_watermark(
                source_key, success=success, content_hash=digest,
                event_watermark=result.get("event_watermark"),
                error=result.get("error") or (
                    None if digest else "producer supplied no source digest"),
                scope_id=scope_id)
        except Exception as persist_exc:
            raise RuntimeError(
                f"cannot persist freshness for {source_key}: "
                f"{type(persist_exc).__name__}: {persist_exc}") from persist_exc

    print("\n" + "=" * 70)
    print(f"{phase_label} COMPLETE in {time.time() - t0:.1f}s")
    print("=" * 70)
    total_nodes = sum((r.get("nodes") or 0) for r in results)
    total_edges = sum((r.get("edges") or 0) for r in results)
    print(f"TOTAL: {total_nodes} new nodes, {total_edges} new edges")
    for r in results:
        print(f"  {r}")
    return results


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--concepts-limit", type=int, default=500)
    ap.add_argument("--concepts-source-id",
                    help="run only Concepts for one exact eligible Brain node; writes a source-scoped watermark")
    args = ap.parse_args()
    main(concepts_limit=args.concepts_limit, concepts_source_id=args.concepts_source_id)
