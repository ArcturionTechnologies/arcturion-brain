"""Handle source records using the configured exclusion policy."""
from __future__ import annotations

import json
import subprocess
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as C
from . import db as _DB
from .util import now_iso
from .watch import watch_dirs
from .resolver import is_archive_path

HEALTH_URL = "http://127.0.0.1:8090/health"
ASSERTION_PATH = C.GRAPH_DIR / "coverage_assertion.md"
OPERATING_INVENTORY_PATH = C.GRAPH_DIR / "operating_inventory.json"
SOURCE_STALE_HOURS = 28      # nightly reindex @02:30 → 28h leaves margin for a slow run
REQUIRED_HARVESTERS = ("agents", "applications", "events", "external", "concepts")
WATCH_LOG = C.DESKTOP_ROOT / "ARCTURION_HQ" / "11 \U0001F4DC LOGS" / "brain" / "memory-watch.log"

# A check result: (status, message). status ∈ {"PASS","WARN","FAIL"}.
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

# Edge-walkability thresholds, applied to the STRUCTURAL percentage (machine-derived
# similarity edges excluded — see db.DERIVED_PREDICATES for why). Calibrated against
# the two known states: the 2026-07-31 rot measured 8.9% and must FAIL loudly; the
# post-repair graph measures ~88% and should WARN, because the dangling remainder is
# real and someone should still be looking at it. FAIL is the level that alerts the
# COVENANT inbox and exits non-zero, so it is reserved for "the graph is hollow", not
# "the graph is dusty".
EDGE_WALKABLE_FAIL_PCT = 85.0
EDGE_WALKABLE_WARN_PCT = 95.0


def edge_walkability_result(health: dict) -> tuple[str, str, str]:
    """Turn a `db.edge_health()` census into a (check, status, detail) row.

    Pure function of the census — no DB, no clock — so the verdict boundaries are
    pinned by tests instead of only ever being observed on live data.
    """
    total = health.get("edges_total") or 0
    if not total:
        return ("edge-walkability", WARN, "no edges in the graph — nothing to walk")

    pct = health.get("structural_walkable_pct")
    struct_ok = health.get("structural_walkable") or 0
    struct_total = health.get("structural_total") or 0
    dead = health.get("fully_dangling_predicates") or []
    base = (f"{struct_ok:,}/{struct_total:,} structural edges walkable ({pct}%); "
            f"{health.get('edges_walkable', 0):,}/{total:,} overall")

    if pct is None:
        return ("edge-walkability", WARN,
                f"{total:,} edges but none are structural — the graph is all derived "
                "similarity, with no asserted relationships behind it")
    if pct < EDGE_WALKABLE_FAIL_PCT:
        return ("edge-walkability", FAIL,
                f"{base} — below {EDGE_WALKABLE_FAIL_PCT}%. Most edges point at node ids "
                "that no longer exist, so the graph is far smaller than it reports. Run "
                "the edge GC and find what is asserting edges against deleted nodes")
    if dead:
        return ("edge-walkability", WARN,
                f"{base}; relationship classes with ZERO walkable edges: "
                f"{', '.join(dead)} — dead vocabulary, not links")
    if pct < EDGE_WALKABLE_WARN_PCT:
        return ("edge-walkability", WARN,
                f"{base} — below {EDGE_WALKABLE_WARN_PCT}%; dangling edges are accumulating")
    return ("edge-walkability", PASS, base)


# --------------------------------------------------------------------------- helpers
# Desktop entries that carry an agent-shaped "000 */Memory" room but are NOT
# constellation agents — the prong roots themselves. HQ has a "000 🏛 EXECUTIVE
# OFFICES/Memory" room; the others don't today, but we list them all so a future
# room can't turn a prong root into a phantom agent. Derived from config so a prong
# rename can't desync this list.
_NON_AGENT_DIRS = {
    C.UNIVERSE_ROOT.name, C.BRAIN_ROOT.name, C.RUNTIME_ROOT.name, C.ATLAS_ROOT.name,
}


def _disk_agents_with_memory() -> set[str]:
    """Desktop folders that carry the canonical agent signature: a "000 */Memory" room,
    excluding the prong roots (HQ/Brain/Runtime/ATLAS) which are not constellation agents."""
    found: set[str] = set()
    for child in C.DESKTOP_ROOT.iterdir():
        if not child.is_dir() or child.name in _NON_AGENT_DIRS:
            continue
        try:
            if any(room.is_dir() for room in child.glob("000 */Memory")):
                found.add(child.name)
        except OSError:
            continue
    return found


def _memory_node_count(conn: sqlite3.Connection, agent: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM nodes WHERE path LIKE ? OR path LIKE ?",
        (f"{agent}/%/Memory/%", f"{agent}/memory/%"),
    ).fetchone()
    return int(row["c"]) if row else 0


def _hours_since(p: Path) -> float | None:
    try:
        return (time.time() - p.stat().st_mtime) / 3600.0
    except OSError:
        return None


def _age_hours(value: str | None, now: datetime | None = None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            return None
        delta = (now - dt.astimezone(now.tzinfo)).total_seconds() / 3600.0
        return delta if delta >= 0.0 else None
    except (TypeError, ValueError):
        return None


def operating_inventory_result(path: Path | None = None,
                               now: datetime | None = None) -> tuple[str, str, str]:
    """Validate the expiring manifest inventory itself, independent of file ingestion."""
    path = Path(path or OPERATING_INVENTORY_PATH)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        return ("operating-inventory", FAIL, "validation clock is timezone-naive")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return ("operating-inventory", FAIL,
                f"inventory missing/malformed: {type(exc).__name__}: {exc}")
    if (not isinstance(payload, dict)
            or type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != 1):
        return ("operating-inventory", FAIL, "inventory schema_version must be 1")
    records = payload.get("records")
    rejected = payload.get("rejected_manifests")
    if not isinstance(records, list) or not records:
        return ("operating-inventory", FAIL, "inventory has no manifest records")
    if not isinstance(rejected, list):
        return ("operating-inventory", FAIL, "rejected_manifests must be a list")
    if rejected:
        return ("operating-inventory", FAIL,
                f"{len(rejected)} manifest(s) rejected; inventory is incomplete")
    generated_age = _age_hours(payload.get("generated_at"), now)
    if generated_age is None:
        return ("operating-inventory", FAIL,
                "generated_at is missing, malformed, timezone-naive, or future")
    invalid: list[str] = []
    expired: list[str] = []
    for i, record in enumerate(records):
        if not isinstance(record, dict):
            invalid.append(f"row-{i}")
            continue
        label = str(record.get("label") or f"row-{i}")
        source_hash = record.get("source_sha256")
        observed_age = _age_hours(record.get("observed_at"), now)
        hash_ok = (isinstance(source_hash, str) and len(source_hash) == 64
                   and all(c in "0123456789abcdefABCDEF" for c in source_hash))
        if (record.get("state") != "configured" or not record.get("source_path")
                or not hash_ok or observed_age is None
                or observed_age > SOURCE_STALE_HOURS):
            invalid.append(label)
        try:
            observed = datetime.fromisoformat(
                str(record.get("observed_at")).replace("Z", "+00:00"))
            expiry = datetime.fromisoformat(
                str(record.get("expires_at")).replace("Z", "+00:00"))
            if observed.tzinfo is None or expiry.tzinfo is None:
                invalid.append(label)
            else:
                observed = observed.astimezone(now.tzinfo)
                expiry = expiry.astimezone(now.tzinfo)
                if expiry <= observed or expiry > observed + timedelta(hours=SOURCE_STALE_HOURS):
                    invalid.append(label)
                elif expiry <= now:
                    expired.append(label)
        except (TypeError, ValueError):
            invalid.append(label)
    if invalid:
        return ("operating-inventory", FAIL,
                f"{len(set(invalid))} record(s) have invalid source/hash/time/state fields")
    if expired:
        return ("operating-inventory", FAIL,
                f"{len(expired)} manifest record(s) expired")
    if generated_age > SOURCE_STALE_HOURS:
        return ("operating-inventory", FAIL,
                f"inventory generated {generated_age:.1f}h ago (> {SOURCE_STALE_HOURS}h)")
    return ("operating-inventory", PASS,
            f"{len(records)} configured manifest records; generated {generated_age:.1f}h ago; none rejected/expired")


def source_freshness_results(conn: sqlite3.Connection, active: list[str],
                             now: datetime | None = None) -> list[tuple[str, str, str]]:
    """Assess each source's proven ingestion, never the database file's mtime."""
    out: list[tuple[str, str, str]] = []
    try:
        conn.execute("SELECT 1 FROM source_ingest_watermarks LIMIT 1")
    except sqlite3.Error as exc:
        return [("source-watermarks", FAIL,
                 f"source_ingest_watermarks unavailable: {type(exc).__name__}: {exc}")]

    for agent in active:
        rows = conn.execute(
            """SELECT n.id,n.path,w.source_key,w.last_success_at,w.status,w.content_hash
                 FROM nodes n
                 LEFT JOIN source_ingest_watermarks w
                   ON w.node_id=n.id AND w.source_kind='file'
                WHERE n.path LIKE ? AND n.path NOT LIKE '%#%'
                  AND n.content_hash IS NOT NULL""",
            (f"{agent}/%",),
        ).fetchall()
        current = [dict(r) for r in rows if not is_archive_path(r["path"])]
        sources = len(current)
        never_ok = sum(1 for r in current if not r["source_key"] or not r["last_success_at"])
        errors = sum(1 for r in current if r["status"] == "error")
        unhashed = sum(1 for r in current if r["source_key"] and not r["content_hash"])
        ages = [_age_hours(r["last_success_at"], now) for r in current]
        invalid_times = sum(1 for a in ages if a is None)
        valid_ages = [a for a in ages if a is not None]
        age = min(valid_ages) if valid_ages else None
        oldest_age = max(valid_ages) if valid_ages else None
        if not sources or age is None or oldest_age is None or never_ok or invalid_times:
            out.append((f"source-freshness:{agent}", FAIL,
                        f"{never_ok} source(s) lack a successful watermark; "
                        f"{invalid_times} have invalid/future success timestamps; "
                        f"{sources} current canonical files assessed"))
        elif oldest_age > SOURCE_STALE_HOURS:
            out.append((f"source-freshness:{agent}", FAIL,
                        f"oldest successful source observation {oldest_age:.1f}h ago "
                        f"(> {SOURCE_STALE_HOURS}h), across {sources} sources"))
        elif errors:
            out.append((f"source-freshness:{agent}", WARN,
                        f"{sources} sources; latest success {age:.1f}h ago; "
                        f"{errors} currently erroring"))
        elif unhashed:
            out.append((f"source-freshness:{agent}", WARN,
                        f"{sources} sources fresh, but {unhashed} lack hashes"))
        else:
            out.append((f"source-freshness:{agent}", PASS,
                        f"{sources} source hashes observed successfully; newest {age:.1f}h ago"))

    for name in REQUIRED_HARVESTERS:
        row = conn.execute(
            "SELECT status,last_success_at,content_hash,event_watermark,last_error "
            "FROM source_ingest_watermarks WHERE source_key=?",
            (f"harvester:{name}",),
        ).fetchone()
        age = _age_hours(row["last_success_at"], now) if row else None
        missing_event_cursor = name == "events" and row and not row["event_watermark"]
        if not row or age is None or age > SOURCE_STALE_HOURS or not row["content_hash"] \
                or missing_event_cursor:
            detail = "missing watermark" if not row else (
                f"status={row['status']} age={age}h hash={bool(row['content_hash'])} "
                f"event_watermark={bool(row['event_watermark'])} error={row['last_error']}")
            out.append((f"harvester-freshness:{name}", FAIL, detail))
        elif row["status"] == "error":
            out.append((f"harvester-freshness:{name}", WARN,
                        f"last success {age:.1f}h ago; latest attempt failed: {row['last_error']}"))
        else:
            out.append((f"harvester-freshness:{name}", PASS,
                        f"successful content watermark {age:.1f}h ago"
                        + (" with event-state cursor" if name == "events" else "")))
    return out


def _watcher_alive() -> bool:
    try:
        r = subprocess.run(["/usr/bin/pgrep", "-f", "engine.watch"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:
        return False


def _health_ok() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=8) as resp:
            return resp.status == 200
    except Exception:
        return False


def _has_brain_query_wiring(agent: str) -> bool:
    """Cheap, bounded check that an agent is wired to READ the Brain: its CLAUDE.md or
    any Boot manifest mentions brain_query / the localhost query endpoint / port 8090."""
    needles = ("brain_query", "8090", "/query?topic")
    root = C.DESKTOP_ROOT / agent
    candidates: list[Path] = [root / "CLAUDE.md"]
    candidates += list(root.glob("000 */Boot/*.md"))
    for f in candidates:
        try:
            txt = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(n in txt for n in needles):
            return True
    return False


# --------------------------------------------------------------------------- checks
def run_checks() -> tuple[str, list[tuple[str, str, str]]]:
    """Return (overall_status, results) where results is [(check, status, detail), ...]."""
    roster = list(C.CONSTELLATION_AGENTS)
    active = list(C.ACTIVE_CONSTELLATION_AGENTS)
    excluded = dict(getattr(C, "EXCLUDED_AGENTS", {}))
    results: list[tuple[str, str, str]] = []

    # 1. Onboarding guard — the core fix.
    disk = _disk_agents_with_memory()
    accounted = set(roster) | set(excluded)
    unwired = sorted(disk - accounted)
    if unwired:
        results.append(("onboarding-guard", FAIL,
                        "agents have a Memory room but are NOT in CONSTELLATION_AGENTS "
                        f"or EXCLUDED_AGENTS: {', '.join(unwired)} — add to one of them "
                        "in engine/config.py"))
    else:
        results.append(("onboarding-guard", PASS,
                        f"{len(disk)} on-disk agents all accounted for "
                        f"(roster {len(roster)} + excluded {len(excluded)})"))

    # Phantom roster entries (in config, no on-disk home) — WARN.
    phantom = sorted(a for a in roster if not (C.DESKTOP_ROOT / a).is_dir())
    if phantom:
        results.append(("roster-phantoms", WARN,
                        f"roster lists agents with no Desktop home: {', '.join(phantom)}"))

    # 2. Watch coverage — every active agent has ≥1 watched dir.
    watched = watch_dirs()
    missing_watch = [a for a in active
                     if not any(w.startswith(str(C.DESKTOP_ROOT / a) + "/") for w in watched)]
    if missing_watch:
        results.append(("watch-coverage", FAIL,
                        f"active agents not watched by the live daemon: {', '.join(missing_watch)}"))
    else:
        results.append(("watch-coverage", PASS,
                        f"all {len(active)} active agents covered by {len(watched)} watched dirs"))

    # 3. Per-source freshness + per-agent memory representation. Inventory expiry is
    # checked independently so a freshly reingested stale Markdown twin cannot go green.
    results.append(operating_inventory_result())
    try:
        conn = sqlite3.connect(f"file:{C.BRAIN_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        results.extend(source_freshness_results(conn, active))
        empty = []
        counts = {}
        for agent in active:
            n = _memory_node_count(conn, agent)
            counts[agent] = n
            if n == 0:
                empty.append(agent)
        # 3b. Edge walkability — the health metric that was missing. A raw edge total
        # cannot tell you the graph has hollowed out; this can.
        try:
            results.append(edge_walkability_result(_DB.edge_health(conn)))
        except Exception as e:  # noqa: BLE001
            results.append(("edge-walkability", WARN,
                            f"could not census edges: {type(e).__name__}: {e}"))
        conn.close()
        if empty:
            results.append(("memory-indexed", WARN,
                            "active agents wired but with 0 indexed memory nodes "
                            f"(thin/empty memory, not a wiring fault): {', '.join(empty)}"))
        else:
            lo = min(counts, key=counts.get)
            results.append(("memory-indexed", PASS,
                            f"every active agent has indexed memory (lowest: {lo}={counts[lo]})"))
    except Exception as e:  # noqa: BLE001
        results.append(("memory-indexed", WARN, f"could not read brain.sqlite: {type(e).__name__}: {e}"))

    # 4. Server + watcher daemon (WARN — a down surface ≠ lost memory).
    health = _health_ok()
    results.append(("http-health", PASS if health else WARN,
                    "GET /health 200" if health else f"{HEALTH_URL} unreachable"))
    if _watcher_alive():
        results.append(("watcher-process", PASS, "engine.watch is running"))
    else:
        wl = _hours_since(WATCH_LOG)
        results.append(("watcher-process", WARN,
                        "engine.watch not found via pgrep"
                        + (f"; last log {wl:.1f}h ago" if wl is not None else "")))

    # 5. Cross-agent searchability (WARN).
    no_query = [a for a in active if not _has_brain_query_wiring(a)]
    if no_query:
        results.append(("searchability", WARN,
                        "active agents with no detectable brain_query wiring in "
                        f"CLAUDE.md/Boot: {', '.join(no_query)}"))
    else:
        results.append(("searchability", PASS, "all active agents reference brain_query"))

    # 6. Duplicate memory paths (WARN).
    dupes = []
    for agent in active:
        root = C.DESKTOP_ROOT / agent
        has_room = any(r.is_dir() for r in root.glob("000 */Memory"))
        has_low = (root / "memory").is_dir()
        if has_room and has_low:
            dupes.append(agent)
    if dupes:
        results.append(("duplicate-memory-path", WARN,
                        "agents carry BOTH '000 */Memory' and lowercase 'memory/' "
                        f"(split memory; consider consolidating): {', '.join(dupes)}"))
    else:
        results.append(("duplicate-memory-path", PASS, "no split memory paths"))

    overall = FAIL if any(s == FAIL for _, s, _ in results) else PASS
    return overall, results


# --------------------------------------------------------------------------- output
_ICON = {PASS: "✅", WARN: "⚠️", FAIL: "🛑"}


def write_assertion(overall: str, results: list[tuple[str, str, str]]) -> Path:
    excluded = dict(getattr(C, "EXCLUDED_AGENTS", {}))
    lines = [
        "# Brain — Memory-Ingestion Coverage Assertion",
        "",
        f"_generated: {now_iso()}_",
        "",
        f"## {_ICON[overall]} {overall}",
        "",
        "| Check | Status | Detail |",
        "|---|---|---|",
    ]
    for check, status, detail in results:
        lines.append(f"| `{check}` | {_ICON[status]} {status} | {detail} |")
    lines += [
        "",
        "## Deliberately excluded agents",
        "",
        "| Agent | Reason |",
        "|---|---|",
    ]
    for name, reason in excluded.items():
        lines.append(f"| `{name}` | {reason} |")
    lines += [
        "",
        "_Onboarding rule: any Desktop folder with a `000 */Memory` room must appear in "
        "`CONSTELLATION_AGENTS` or `EXCLUDED_AGENTS` (engine/config.py), else this "
        "assertion FAILs._",
        "",
    ]
    ASSERTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    ASSERTION_PATH.write_text("\n".join(lines), encoding="utf-8")
    return ASSERTION_PATH


def _emit_inbox_alert(results: list[tuple[str, str, str]]) -> None:
    """Append a governance alert to the COVENANT inbox on FAIL (mirrors reports.py)."""
    fails = [(c, d) for c, s, d in results if s == FAIL]
    if not fails:
        return
    inbox = C.COVENANT_INBOX
    try:
        inbox.parent.mkdir(parents=True, exist_ok=True)
        if not inbox.exists() or inbox.stat().st_size == 0:
            inbox.write_text(
                "---\ncreated: " + now_iso() + "\nowner: COVENANT\nttl_tier: 1\n"
                "status: active\n---\n\n# COVENANT Inbox\n\n## Active\n\n",
                encoding="utf-8",
            )
        block = [f"### 🚨 Brain Coverage FAIL — {now_iso()}", ""]
        for check, detail in fails:
            block.append(f"- **{check}**: {detail}")
        block.append(f"    - source: `index/coverage_assertion.md`")
        block.append("")
        with inbox.open("a", encoding="utf-8") as f:
            f.write("\n".join(block) + "\n")
    except Exception:
        pass  # alerting is best-effort; the non-zero exit + assertion file still stand


def main(argv: list[str] | None = None) -> int:
    overall, results = run_checks()
    path = write_assertion(overall, results)
    for check, status, detail in results:
        print(f"{_ICON[status]} {status:4} {check}: {detail}")
    print(f"\n{_ICON[overall]} OVERALL: {overall}  → {path}")
    if overall == FAIL:
        _emit_inbox_alert(results)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
