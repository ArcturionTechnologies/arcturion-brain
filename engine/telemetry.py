"""Bounded telemetry classification; no source deletion or privacy authority."""
from pathlib import Path, PurePosixPath
import json
import re
from . import config as C

VERSION = 1
PREFIXES = (
    "07 ⚙️ OPS/state/vega/prospective_evidence/",
    "07 ⚙️ OPS/state/vega/watcher/",
    "07 ⚙️ OPS/state/universe_health/cadence_runs/",
)

def classify(path, parsed=None):
    value = str(path)
    if "\\" in value or ".." in PurePosixPath(value).parts:
        return None
    p = Path(value)
    if p.is_absolute():
        try:
            value = p.relative_to(C.UNIVERSE_ROOT).as_posix()
        except ValueError:
            return None
    prefix = next((x for x in PREFIXES if value.startswith(x)), None)
    if not prefix or not value.endswith(".json"):
        return None
    name = PurePosixPath(value).name
    # These collection paths and record names are machine evidence contracts.
    record_name = bool(re.fullmatch(r"[0-9a-f]{64}\.json", name)
                       or re.fullmatch(r"(attempt|input|output)_\d+\.json", name)
                       or (prefix.endswith("cadence_runs/") and name == "receipt.json"))
    keys = set(parsed) if isinstance(parsed, dict) else set()
    schema = ({"observed_at", "payload"} <= keys
              or {"started_at", "completed_at"} <= keys
              or {"execution_authorized", "analysis_status"} <= keys
              or {"observed_at", "task_boundary"} <= keys
              or (prefix == PREFIXES[1] and ({"at", "wake_score", "snapshot", "verdict"} <= keys
                  or {"fired", "reason", "score", "snapshot", "symbol"} <= keys
                  or {"at", "wake_score", "snapshot", "symbol", "status"} <= keys)))
    if not (record_name or schema):
        return None
    return {"record_class": "machine_telemetry", "classification_version": VERSION,
            "classification_basis": "collection-record-name" if record_name else "collection-observation-schema",
            "collection": prefix, "raw_evidence_retained": True}

def is_telemetry(metadata):
    try:
        m = json.loads(metadata) if isinstance(metadata, str) else metadata
        return isinstance(m, dict) and m.get("record_class") == "machine_telemetry"
    except (ValueError, TypeError):
        return False


def extraction_current(node, stored_metadata):
    try:
        old = json.loads(stored_metadata) if isinstance(stored_metadata, str) else (stored_metadata or {})
    except (ValueError, TypeError):
        return False
    new = node.get("metadata") or {}
    return (new.get("python_analysis", {}).get("version") == old.get("python_analysis", {}).get("version")
            and new.get("record_class") == old.get("record_class")
            and new.get("classification_version") == old.get("classification_version"))
