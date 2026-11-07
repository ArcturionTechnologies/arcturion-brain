"""Bounded current-source updates gated on exact per-file ingest success.

Partial ingestion failures are reported without recording authority supersession.
"""
import json
from pathlib import Path

from .. import db as DB
from .. import config as C
from ..ingest import ingest_paths
from ..walker import walk_paths
from ..extractors import route
from ..util import now_iso, short_hash
from .validity_pass import stamp_from_supersessions


_RETIRED = {"archived", "superseded", "retired", "privacy_excluded"}
_INGEST_COUNTS = ("files", "upserted", "skipped_hash", "refused", "edges",
                  "embedded", "failures")


def _changed_rows(path: str, content_hash: str) -> list[dict]:
    conn = DB.connect()
    try:
        DB.init_db(conn)
        rows = conn.execute("SELECT * FROM nodes WHERE path=?", (path,)).fetchall()
        return [
            dict(row) for row in rows
            if row["content_hash"] != content_hash
            and (row["status"] or "").strip().lower() not in _RETIRED
        ]
    finally:
        conn.close()


def _stage_same_id_versions(node: dict, old_rows: list[dict]) -> tuple[list[str], str | None]:
    staged = []
    for old in old_rows:
        if old["id"] != node["id"]:
            continue
        old_hash = old.get("content_hash") or short_hash(json.dumps(old, sort_keys=True))
        snapshot_id = old["id"] + "_version_" + old_hash[:12]
        snapshot = dict(old)
        snapshot["id"] = snapshot_id
        snapshot["status"] = "archived"
        metadata = snapshot.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}
        snapshot["metadata"] = {
            **metadata,
            "source_version_of": node["id"],
            "source_path": node["path"],
            "source_content_hash": old.get("content_hash"),
        }

        conn = DB.connect()
        try:
            DB.init_db(conn)
            existing = conn.execute(
                "SELECT content_hash FROM nodes WHERE id=?", (snapshot_id,)
            ).fetchone()
            if existing and existing["content_hash"] != old.get("content_hash"):
                conn.rollback()
                return staged, "snapshot_id_conflict"
            if not existing:
                DB.upsert_node(conn, snapshot)
                if old.get("valid_until") is not None:
                    conn.execute(
                        "UPDATE nodes SET valid_until=? WHERE id=?",
                        (old["valid_until"], snapshot_id),
                    )
            conn.commit()
            staged.append(snapshot_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return staged, None


def _current_node_matches(node: dict) -> bool:
    conn = DB.connect()
    try:
        DB.init_db(conn)
        row = conn.execute(
            "SELECT 1 FROM nodes WHERE id=? AND path=? AND content_hash=?",
            (node["id"], node["path"], node["content_hash"]),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _supersession_record(old_id: str, node: dict, timestamp: str) -> dict:
    new_id = node["id"]
    return {
        "id": "sup_oct2_" + short_hash(old_id + new_id),
        "status": "active",
        "old_id": old_id,
        "new_id": new_id,
        "old": old_id,
        "new": new_id,
        "created": timestamp,
        "activated": timestamp,
        "owner": "COVENANT",
        "source": "universe-remediation-20261002-01a0fc85",
        "source_path": node.get("path"),
        "source_content_hash": node.get("content_hash"),
        "summary": "Authorized current canonical source replaces preserved indexed source version.",
    }


def run(paths):
    items = list(walk_paths([Path(item) for item in paths]))
    totals = {key: 0 for key in _INGEST_COUNTS}
    snapshots = []
    records = []
    outcomes = []

    for path, family, node_type in items:
        try:
            node, _edges = route(path, family, node_type)
        except Exception as exc:
            totals["failures"] += 1
            outcomes.append({"path": str(path), "result": "route_failed",
                             "error": type(exc).__name__})
            continue
        if not node:
            totals["failures"] += 1
            outcomes.append({"path": str(path), "result": "no_node"})
            continue
        if (node.get("metadata") or {}).get("extract_error"):
            # ingest_paths currently upserts extract_error nodes before reporting failure.
            # Do not call it for this source; preserve the current authority unchanged.
            totals["failures"] += 1
            outcomes.append({"path": node.get("path"), "result": "extract_error"})
            continue

        old_rows = _changed_rows(node["path"], node.get("content_hash"))
        staged, stage_error = _stage_same_id_versions(node, old_rows)
        snapshots.extend(staged)
        if stage_error:
            totals["failures"] += 1
            outcomes.append({"path": node["path"], "result": stage_error,
                             "staged_snapshots": staged})
            continue

        # Per-file call makes the aggregate ingest result attributable to this source.
        stats = ingest_paths([path], embed=False, verbose=False)
        for key in _INGEST_COUNTS:
            totals[key] += int(stats.get(key, 0) or 0)
        successful = (
            stats.get("files") == 1
            and stats.get("refused", 0) == 0
            and stats.get("failures", 0) == 0
            and stats.get("upserted", 0) + stats.get("skipped_hash", 0) == 1
        )
        if not successful:
            partial_upsert = (bool(stats.get("upserted", 0))
                              and _current_node_matches(node))
            outcome = ("refused" if stats.get("refused", 0) else
                       "ingest_failed_after_upsert_preserved_version"
                       if partial_upsert else "ingest_failed")
            outcomes.append({"path": node["path"], "result": outcome,
                             "ingest": stats, "staged_snapshots": staged,
                             "partial_current_node_hash_present": partial_upsert,
                             "supersession_created": False,
                             "recovery_required": partial_upsert})
            continue
        if not _current_node_matches(node):
            totals["failures"] += 1
            outcomes.append({"path": node["path"], "result": "current_hash_verification_failed",
                             "ingest": stats, "staged_snapshots": staged})
            continue

        timestamp = now_iso()
        source_records = []
        conn = DB.connect()
        try:
            DB.init_db(conn)
            for old in old_rows:
                if old["id"] == node["id"]:
                    old_id = old["id"] + "_version_" + (
                        old.get("content_hash") or short_hash(json.dumps(old, sort_keys=True))
                    )[:12]
                    archived = conn.execute(
                        "SELECT content_hash FROM nodes WHERE id=?", (old_id,)
                    ).fetchone()
                    if not archived or archived["content_hash"] != old.get("content_hash"):
                        raise RuntimeError("preserved source version is missing or mismatched")
                else:
                    cur = conn.execute(
                        "UPDATE nodes SET status='superseded' "
                        "WHERE id=? AND path=? AND content_hash=? AND status=?",
                        (old["id"], old["path"], old.get("content_hash"), old.get("status")),
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError("source row changed during targeted ingest")
                    old_id = old["id"]
                source_records.append(_supersession_record(old_id, node, timestamp))
            conn.commit()
        except Exception as exc:
            conn.rollback()
            outcomes.append({"path": node["path"], "result": "supersession_stage_failed",
                             "error": type(exc).__name__, "ingest": stats,
                             "staged_snapshots": staged})
            continue
        finally:
            conn.close()

        records.extend(source_records)
        outcomes.append({"path": node["path"], "result": "current_hash_ingested",
                         "node_id": node["id"], "content_hash": node["content_hash"],
                         "supersession_records": [r["id"] for r in source_records]})

    if records:
        with (C.GRAPH_DIR / "supersessions.jsonl").open("a", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        conn = DB.connect()
        try:
            DB.init_db(conn)
            validity = stamp_from_supersessions(
                conn, C.GRAPH_DIR / "supersessions.jsonl",
                verbose=False, apply=True, records=records,
            )
        finally:
            conn.close()
    else:
        validity = {"stamped": 0, "updated": 0, "applied": False,
                    "selected_records": 0, "ids": []}

    return {
        "targeted_ingest": totals,
        "historical_version_rows": len(snapshots),
        "explicit_supersession_records_appended": len(records),
        "validity": validity,
        "source_paths": [str(path) for path, _family, _type in items],
        "source_results": outcomes,
    }


if __name__ == "__main__":
    import sys
    print(json.dumps(run(sys.argv[1:]), indent=2))
