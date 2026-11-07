"""Config-file extractor — JSON / YAML / TOML / .env.tpl."""

from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime
from .. import config as C
from ..util import make_node_id, content_hash, truncate, now_iso, slugify
from .markdown import _rel_to_universe


def extract_config(path: Path, default_type: str = "Config") -> tuple[dict, list[dict]]:
    try:
        raw = path.read_bytes()
    except Exception:
        raw = b""
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        text = ""

    stat = path.stat()
    rel = _rel_to_universe(path)
    title = path.name
    parsed = None
    ext = path.suffix.lower()
    if ext == ".json":
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
    elif ext in {".yaml", ".yml"}:
        try:
            import yaml
            parsed = yaml.safe_load(text)
        except Exception:
            parsed = None

    summary = path.stem.replace("_", " ").replace("-", " ").strip()
    if isinstance(parsed, dict):
        keys = ", ".join(list(parsed.keys())[:10])
        summary = f"Config keys: {keys}"

    node_id = make_node_id(path, title)
    node = {
        "id": node_id,
        "type": default_type,
        "family": "code",
        "title": title,
        "path": rel,
        "owner": None,
        "status": "active",
        "governance_tier": 0,
        "content_hash": content_hash(raw),
        "summary": summary,
        "body_excerpt": truncate(text, 4000),
        "centrality": 0.0,
        "created": None,
        "last_modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "last_reviewed": None,
        "metadata": {"size_bytes": stat.st_size, "ext": ext},
    }

    from ..source_integrity import artifact_privacy_reason
    reason = artifact_privacy_reason(parsed, node['content_hash'])
    if reason:
        node['metadata']['restricted_artifact_reason'] = reason

    from ..telemetry import classify
    classification = classify(path, parsed)
    if classification:
        node["type"] = "Event"
        node["family"] = "event"
        node["metadata"].update(classification)
        node["summary"] = "Machine telemetry: " + summary.removeprefix("Config keys: ")
        return node, []

    edges: list[dict] = []
    # Config file path patterns often imply a 'configures' edge to a target.
    asserted_at = now_iso()
    name = path.stem
    if name.endswith(".config") or name.endswith("_config"):
        target = name.rsplit(".config", 1)[0].rsplit("_config", 1)[0]
        if target:
            endpoint_id = f"service_{slugify(target)}"
            edges.append({
                "from": node_id, "to": endpoint_id,
                "predicate": "configures", "family": "code",
                "weight": 0.7, "confidence": 0.6,
                "asserted_by": "STATIC_NAME", "asserted_at": asserted_at,
                "evidence_span": "filename-pattern",
                "metadata": {
                    "endpoint_node": {
                        "id": endpoint_id,
                        "type": "Service",
                        "family": "application",
                        "title": target.replace("_", " ").replace("-", " "),
                        "path": None,
                        "status": "configured",
                        "summary": f"Service configured by {rel}",
                        "last_modified": asserted_at,
                        "metadata": {
                            "identity_basis": "configuration_filename",
                            "runtime_observed": False,
                        },
                    }
                },
            })
    return node, edges
