"""Finite, version-checked metadata repair. Caller owns transaction and journal."""
from pathlib import Path
import json
from ..extractors.markdown import extract_markdown
from ..source_integrity import filter_retrieval

def plan_or_apply(conn, paths: list[Path], *, apply: bool = False) -> dict:
    if apply and not conn.in_transaction:
        raise ValueError("apply requires a caller-owned transaction")
    if not paths or len(set(map(str, paths))) != len(paths):
        raise ValueError("provide a finite nonempty list of unique paths")
    journal, refused = [], []
    for path in paths:
        node, _ = extract_markdown(Path(path))
        rows = conn.execute("SELECT * FROM nodes WHERE path=?", (node["path"],)).fetchall()
        if len(rows) != 1:
            refused.append({"path": node["path"], "reason": "missing_or_ambiguous_index"})
            continue
        old = dict(rows[0])
        if old["id"] != node["id"] or old["content_hash"] != node["content_hash"]:
            refused.append({"path": node["path"], "reason": "source_version_changed"})
            continue
        if not filter_retrieval(conn, [old]):
            refused.append({"id": old["id"], "reason": "excluded"})
            continue
        metadata = old.get("metadata") or {}
        if isinstance(metadata, str): metadata = json.loads(metadata)
        updated = dict(metadata)
        for key in ("subject", "ownership_source", "ownership_conflict", "ownership_invalid"):
            updated.pop(key, None)
            if key in node["metadata"]: updated[key] = node["metadata"][key]
        if old.get("owner") == node.get("owner") and updated == metadata:
            continue
        journal.append({"id": old["id"], "path": old["path"], "content_hash": old["content_hash"],
                        "before": {"owner": old.get("owner"), "metadata": metadata, "metadata_raw": old.get("metadata")},
                        "after": {"owner": node.get("owner"), "metadata": updated}})
    if apply and refused:
        raise ValueError("metadata batch has refused entries; no updates applied")
    if apply:
        conn.execute("SAVEPOINT metadata_backfill")
        try:
            for entry in journal:
                changed = conn.execute(
                    "UPDATE nodes SET owner=?,metadata=? WHERE id=? AND content_hash=? AND owner IS ? AND metadata IS ?",
                    (entry["after"]["owner"], json.dumps(entry["after"]["metadata"]),
                     entry["id"], entry["content_hash"], entry["before"]["owner"],
                     # Compare the observed raw representation to avoid overwriting concurrent edits.
                     entry["before"]["metadata_raw"]))
                if changed.rowcount != 1: raise RuntimeError("metadata changed concurrently")
            conn.execute("RELEASE metadata_backfill")
        except Exception:
            conn.execute("ROLLBACK TO metadata_backfill")
            conn.execute("RELEASE metadata_backfill")
            raise
    return {"applied": apply, "changes": journal, "refused": refused,
            "scope": "owner and descriptive metadata only; no ownership-graph or ACL migration"}
