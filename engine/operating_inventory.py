"""Generate an expiring operating inventory from authoritative local manifests.

The inventory describes configured services.  It does not infer runtime health from
the presence of a plist, and every record carries the source bytes' hash plus an
expiry so consumers cannot mistake an old map for current observation.
"""
from __future__ import annotations

import hashlib
import json
import plistlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as C

INVENTORY_JSON = C.GRAPH_DIR / "operating_inventory.json"
# The markdown twin lives on a walked Brain docs shelf. Reindex generates it before the
# static walk, so current inventory facts become searchable in the same cycle; index/
# artifacts are intentionally excluded from the walker.
INVENTORY_MD = C.BRAIN_ROOT / "docs" / "operating_inventory.md"
DEFAULT_TTL_HOURS = 28
COOWNERSHIP_CHARTER = str(C.UNIVERSE_ROOT / "governance/coownership.md")


def _utc(value: str | None = None) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else datetime.now(timezone.utc)
    return dt.replace(tzinfo=dt.tzinfo or timezone.utc).astimezone(timezone.utc)


def collect_launchagents(paths: list[Path], observed_at: str | None = None,
                         ttl_hours: int = DEFAULT_TTL_HOURS) -> tuple[list[dict], list[dict]]:
    observed = _utc(observed_at)
    expires = observed + timedelta(hours=ttl_hours)
    records: list[dict] = []
    rejected: list[dict] = []
    for path in sorted(paths, key=lambda p: str(p)):
        try:
            raw = path.read_bytes()
            data = plistlib.loads(raw)
            label = data.get("Label")
            argv = data.get("ProgramArguments") or []
            program = data.get("Program") or (argv[0] if argv else None)
            if not isinstance(label, str) or not label.strip():
                raise ValueError("missing Label")
            records.append({
                "id": f"service_{label.strip().lower().replace('.', '_').replace('-', '_')}",
                "label": label.strip(),
                "kind": "launchagent",
                "state": "configured",
                "program": program,
                "run_at_load": bool(data.get("RunAtLoad", False)),
                "keep_alive_configured": bool(data.get("KeepAlive", False)),
                "source_path": str(path),
                "source_sha256": hashlib.sha256(raw).hexdigest(),
                "source_mtime_ns": path.stat().st_mtime_ns,
                "observed_at": observed.isoformat(),
                "expires_at": expires.isoformat(),
            })
        except Exception as exc:  # one bad manifest cannot erase the others
            rejected.append({"source_path": str(path),
                             "error": f"{type(exc).__name__}: {exc}"})
    return records, rejected


def generate(paths: list[Path] | None = None, observed_at: str | None = None,
             ttl_hours: int = DEFAULT_TTL_HOURS,
             json_path: Path = INVENTORY_JSON, md_path: Path = INVENTORY_MD) -> dict:
    if paths is None:
        # Only top-level, currently installed universe LaunchAgents. Archive/disabled
        # trees and suffixes such as .bak are excluded; both Arcturion and Hermes
        # namespaces are required for the actual critical-service inventory.
        launchagents = C.WORK_ROOT / "Library" / "LaunchAgents"
        paths = [*launchagents.glob("com.arcturion*.plist"),
                 *launchagents.glob("ai.hermes*.plist")]
    generated = _utc(observed_at).isoformat()
    records, rejected = collect_launchagents(paths, generated, ttl_hours)
    payload = {
        "schema_version": 1,
        "generated_at": generated,
        "ttl_hours": ttl_hours,
        "meaning": "configured inventory; runtime health is not asserted",
        "records": records,
        "rejected_manifests": rejected,
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = ["# Expiring operating inventory", "",
             f"Generated: `{generated}` · TTL: `{ttl_hours}h`", "",
             "This map records configuration observed in source manifests. It does not assert health.",
             "",
             "## Brain ownership and evidence",
             "",
             f"The single canonical co-ownership charter is `{COOWNERSHIP_CHARTER}`. "
             "ALTAIR owns Brain engineering: ingestion, indexing, retrieval, service reliability, "
             "backups, recovery and security. COVENANT owns knowledge governance: accuracy, provenance, "
             "identity, contradictions and canon. Both review cross-domain changes and retain evidence that "
             "knowledge is captured, preserved and retrievable. This configured inventory is not proof of "
             "service health, backup success or recovery readiness.", "",
             "| Service | State | Program | Expires | Source hash |", "|---|---|---|---|---|"]
    for r in records:
        lines.append(f"| `{r['label']}` | {r['state']} | `{r['program'] or '—'}` | "
                     f"`{r['expires_at']}` | `{r['source_sha256'][:12]}` |")
    if rejected:
        lines += ["", f"Rejected manifests: **{len(rejected)}** (see JSON artifact)."]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload


if __name__ == "__main__":
    result = generate()
    print(f"operating inventory: {len(result['records'])} records, "
          f"{len(result['rejected_manifests'])} rejected → {INVENTORY_JSON}")
