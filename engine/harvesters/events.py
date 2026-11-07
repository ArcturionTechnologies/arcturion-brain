"""Event + Task + Approval_gate nodes.

Sources (TCC-resilient — works automatically from heartbeat context):
- `approvals.json` across every constellation agent (always available)
- Apple Reminders via the polaris-reminders stdio MCP binary
  (the binary already has TCC permission — heartbeat inherits via subprocess)
- macOS Calendar via icalBuddy CLI (graceful no-op if TCC denied;
  one-time grant: System Settings → Privacy & Security → Calendar → add icalBuddy)
"""

from __future__ import annotations
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from .. import config as C
from .. import db as DB
from ..util import slugify, short_hash, now_iso
from .mcp_client import StdioMCPClient, MCPClientError


POLARIS_REMINDERS_BIN = str(C.REMINDERS_CLI)

# Item 10: extract conference URLs from event notes/location.
_CONFERENCE_URL_RE = re.compile(
    r"https?://(?:[a-zA-Z0-9-]+\.)?(?:zoom\.us|meet\.google\.com|doxy\.me|"
    r"teams\.microsoft\.com|teams\.live\.com|whereby\.com|webex\.com|"
    r"gotomeeting\.com|calendly\.com|cal\.com)/[A-Za-z0-9\-_/?=.&%]*",
    re.IGNORECASE,
)


def _owner_node_id():
    if not C.OWNER_NODE_ID:
        raise ValueError("explicit owner node identity required for ownership edges")
    return C.OWNER_NODE_ID


def _run(cmd: list[str], timeout: int = 15) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, check=False).stdout
    except Exception:
        return ""


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()).hexdigest()


def harvest_approvals(conn, asserted_at: str) -> tuple[int, int, str]:
    """Harvest BOTH pending and resolved approval gates — resolved are durable decisions."""
    nodes = edges = 0
    evidence = []
    for p in C.DESKTOP_ROOT.glob("*/approvals.json"):
        agent = p.parent.name
        try:
            raw = p.read_bytes()
            data = json.loads(raw)
        except Exception as exc:
            raise RuntimeError(f"cannot read approvals source {p}: {exc}") from exc
        evidence.append({"path": str(p), "content_sha256": hashlib.sha256(raw).hexdigest()})
        for bucket, status in (("pending", "pending"), ("resolved", "resolved")):
            for entry in (data.get(bucket) or []):
                eid = entry.get("id") or f"appr_{short_hash(json.dumps(entry, sort_keys=True))}"
                node_id = f"approval_{slugify(agent)}_{slugify(eid)}"
                DB.upsert_node(conn, {
                    "id": node_id, "type": "Approval_gate", "family": "event",
                    "title": entry.get("title", f"{agent} approval {eid}"),
                    "path": f"{agent}/approvals.json#{eid}",
                    "owner": agent, "status": status, "governance_tier": 2,
                    "content_hash": None,
                    "summary": (entry.get("context") or "")[:300],
                    "body_excerpt": None, "centrality": 0.0,
                    "created": entry.get("requested_at"),
                    "last_modified": entry.get("resolved_at") or now_iso(),
                    "last_reviewed": None,
                    "metadata": {"actions": entry.get("actions") or [], "agent": agent,
                                 "bucket": bucket, "decision": entry.get("decision")},
                })
                nodes += 1
                if DB.insert_edge(conn, {
                    "from": node_id, "to": f"agent_{agent.lower()}",
                    "predicate": "requires_approval_from", "family": "operational",
                    "weight": 1.0, "confidence": 1.0,
                    "asserted_by": "HARVEST_EVENT_APPROVALS",
                    "asserted_at": asserted_at, "evidence_span": f"approvals.json:{bucket}",
                }):
                    edges += 1
    return nodes, edges, _digest(evidence)


_GROUP_ROW_RE = re.compile(
    r"^\s*[•\-\*]\s*(.+?)\s+\[(\d+)\s*items?\]\s*(?:·.*)?$"
)


def _list_populated_reminder_lists(client: StdioMCPClient) -> list[str]:
    """Parse the polaris-reminders pr_list_groups markdown into a list of
    list names that currently have >=1 item."""
    out = client.call_tool("pr_list_groups", {})
    if not isinstance(out, str) or not out.strip():
        raise RuntimeError("reminders list source returned an empty/invalid payload")
    lists: list[str] = []
    matched_rows = 0
    for line in out.splitlines():
        m = _GROUP_ROW_RE.match(line)
        if not m:
            continue
        matched_rows += 1
        name = m.group(1).strip()
        count = int(m.group(2))
        if count > 0:
            lists.append(name)
    if not matched_rows:
        raise RuntimeError("reminders list source returned no recognized list rows")
    return lists


def harvest_reminders(conn, asserted_at: str, max_lists: int | None = None) -> tuple[int, int, str]:
    """Reminders via the polaris-reminders stdio MCP binary.

    Bypasses AppleScript TCC issues entirely — the polaris-reminders binary
    has its own TCC grant from Founder and inherits cleanly through subprocess.
    """
    if not Path(POLARIS_REMINDERS_BIN).exists():
        raise RuntimeError(f"required reminders source unavailable: {POLARIS_REMINDERS_BIN}")
    nodes = edges = 0
    evidence = []
    try:
        with StdioMCPClient([POLARIS_REMINDERS_BIN]) as cli:
            populated = _list_populated_reminder_lists(cli)
            evidence.append({"populated_lists": populated})
            if max_lists:
                populated = populated[:max_lists]
            for list_name in populated:
                try:
                    out = cli.call_tool("pr_list_reminders", {"list": list_name},
                                        timeout=20)
                except MCPClientError as exc:
                    raise RuntimeError(f"reminders read failed for {list_name}: {exc}") from exc
                evidence.append({"list": list_name, "payload_sha256": hashlib.sha256(
                    out.encode()).hexdigest()})
                # The tool returns either JSON text or a markdown table.
                items = _parse_reminders_payload(out, list_name)
                # Ensure the list-as-project node exists once per list.
                list_proj_id = f"project_{slugify(list_name)}"
                DB.upsert_node(conn, {
                    "id": list_proj_id, "type": "Project", "family": "concept",
                    "title": list_name, "path": f"reminders://{list_name}",
                    "owner": "FOUNDER", "status": "active", "governance_tier": 0,
                    "content_hash": None,
                    "summary": f"Apple Reminders list: {list_name}",
                    "body_excerpt": None, "centrality": 0.0,
                    "created": None, "last_modified": now_iso(),
                    "last_reviewed": None,
                    "metadata": {"source": "apple_reminders", "list_name": list_name},
                })
                for item in items:
                    rid = item["id"]
                    title = item["title"]
                    notes = item.get("notes") or ""
                    due = item.get("due")
                    priority = item.get("priority", 0)
                    node_id = f"reminder_{slugify(list_name)}_{short_hash(rid)}"
                    DB.upsert_node(conn, {
                        "id": node_id, "type": "Reminder", "family": "event",
                        "title": title,
                        "path": f"reminders://{list_name}/{rid}",
                        "owner": "FOUNDER", "status": "open",
                        "governance_tier": 2 if priority >= 1 else 0,
                        "content_hash": None,
                        "summary": notes[:400] if notes else f"Apple Reminder: {list_name}",
                        "body_excerpt": notes if notes else None,
                        "centrality": 0.0,
                        # Fall back to harvest time so undated reminders are still
                        # temporally placeable (was: due → None for no-due-date items).
                        "created": None, "last_modified": due or asserted_at,
                        "last_reviewed": None,
                        "metadata": {"list": list_name, "reminder_id": rid,
                                     "due": due, "priority": priority},
                    })
                    nodes += 1
                    if DB.insert_edge(conn, {
                        "from": node_id, "to": list_proj_id,
                        "predicate": "participates_in", "family": "social",
                        "weight": 1.0, "confidence": 1.0,
                        "asserted_by": "HARVEST_REMINDERS_MCP_STDIO",
                        "asserted_at": asserted_at,
                        "evidence_span": "polaris-reminders/pr_list_reminders",
                    }):
                        edges += 1
                    if DB.insert_edge(conn, {
                        "from": node_id, "to": _owner_node_id(),
                        "predicate": "owned_by", "family": "structure",
                        "weight": 1.0, "confidence": 1.0,
                        "asserted_by": "HARVEST_REMINDERS_MCP_STDIO",
                        "asserted_at": asserted_at,
                        "evidence_span": "apple-reminders",
                    }):
                        edges += 1
    except MCPClientError as exc:
        raise RuntimeError(f"reminders source query failed: {exc}") from exc
    return nodes, edges, _digest(evidence)


def _parse_reminders_payload(text: str, list_name: str) -> list[dict]:
    """Parse pr_list_reminders output — handles both JSON array and markdown formats."""
    txt = text.strip()
    if not txt:
        raise RuntimeError(f"reminders source returned empty payload for {list_name}")
    if txt.startswith("["):
        try:
            data = json.loads(txt)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"malformed reminders JSON: {exc}") from exc
        if not isinstance(data, list) or any(not isinstance(r, dict) for r in data):
            raise RuntimeError(f"malformed reminders list structure for {list_name}")
        if any(not (r.get("id") or r.get("title")) for r in data):
            raise RuntimeError(f"reminders entry lacks id/title for {list_name}")
        return [{
            "id": r.get("id") or short_hash(r.get("title", "") + list_name),
            "title": r.get("title") or "(untitled)",
            "due": r.get("due"),
            "notes": r.get("notes"),
            "priority": r.get("priority", 0),
        } for r in (data if isinstance(data, list) else [])]
    # Markdown fallback — best-effort parse of one-reminder-per-line.
    items: list[dict] = []
    for line in txt.splitlines():
        line = line.strip()
        if line.startswith(("- ", "* ", "• ")):
            title = line[2:].strip()
            items.append({"id": short_hash(title + list_name), "title": title,
                          "due": None, "notes": None, "priority": 0})
    if not items and txt.lower() not in {"no reminders", "no reminders.", "[]"}:
        raise RuntimeError(f"unrecognized reminders payload for {list_name}")
    return items


def harvest_calendar(conn, asserted_at: str, lookback_days: int = 14,
                     lookahead_days: int = 90) -> tuple[int, int, str]:
    """Pull Calendar events via the EventKit framework (PyObjC).

    Inherits TCC permission from the LaunchAgent's launchd-spawned context.
    One-time grant required for the python3 binary: System Settings ->
    Privacy & Security -> Calendars -> python3.14 -> Options... -> Full Calendar
    access. The `brain-tcc-grant-helper.sh calendar` script opens the right pane.

    A failed source read raises so the event watermark cannot advance on stale DB state.
    """
    nodes = edges = 0
    try:
        import EventKit
        from Foundation import NSRunLoop, NSDate
    except Exception as exc:
        raise RuntimeError(f"EventKit source unavailable: {exc}") from exc

    store = EventKit.EKEventStore.alloc().init()
    grant = {"complete": False, "granted": False}

    def _handler(granted, error):
        grant["granted"] = bool(granted)
        grant["complete"] = True

    try:
        store.requestFullAccessToEventsWithCompletion_(_handler)
    except Exception as exc:
        raise RuntimeError(f"EventKit access request failed: {exc}") from exc

    # Spin the runloop briefly so the completion handler fires.
    for _ in range(50):
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
        if grant["complete"]:
            break
    if not grant["granted"]:
        raise RuntimeError("EventKit access was denied or did not complete")

    calendars = store.calendarsForEntityType_(EventKit.EKEntityTypeEvent)
    if not calendars:
        raise RuntimeError("EventKit returned no readable calendars")

    start = NSDate.dateWithTimeIntervalSinceNow_(-lookback_days * 86400)
    end = NSDate.dateWithTimeIntervalSinceNow_(lookahead_days * 86400)
    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        start, end, calendars
    )
    events = store.eventsMatchingPredicate_(predicate) or []

    evidence = []
    for ev in events:
        title = str(ev.title()) if ev.title() else "(no title)"
        eid = str(ev.eventIdentifier()) if ev.eventIdentifier() else short_hash(title)
        cal = ev.calendar()
        cal_name = str(cal.title()) if cal else "default"
        start_date = ev.startDate()
        start_str = str(start_date) if start_date else ""
        end_date = ev.endDate()
        end_str = str(end_date) if end_date else ""
        location = str(ev.location()) if ev.location() else ""
        notes = str(ev.notes()) if ev.notes() else ""
        is_all_day = bool(ev.isAllDay()) if hasattr(ev, "isAllDay") else False
        evidence.append({"uid": eid, "calendar": cal_name, "title": title,
                         "start": start_str, "end": end_str, "location": location,
                         "notes": notes, "all_day": is_all_day})

        node_id = f"event_{slugify(cal_name)}_{short_hash(eid)}"
        DB.upsert_node(conn, {
            "id": node_id, "type": "Event", "family": "event",
            "title": title,
            "path": f"calendar://{cal_name}/{eid}",
            "owner": "FOUNDER",
            "status": "scheduled",
            "governance_tier": 0,
            "content_hash": None,
            "summary": (notes or f"Calendar '{cal_name}' — {start_str} {location}").strip()[:400],
            "body_excerpt": notes or None,
            "centrality": 0.0,
            "created": start_str, "last_modified": now_iso(), "last_reviewed": None,
            "metadata": {
                "calendar": cal_name, "start": start_str, "end": end_str,
                "location": location, "all_day": is_all_day,
                "uid": eid, "source": "eventkit",
            },
        })
        nodes += 1

        if DB.insert_edge(conn, {
            "from": node_id, "to": _owner_node_id(),
            "predicate": "owned_by", "family": "structure",
            "weight": 1.0, "confidence": 1.0,
            "asserted_by": "HARVEST_CAL_EVENTKIT",
            "asserted_at": asserted_at, "evidence_span": "eventkit",
        }):
            edges += 1

        # Calendar-as-project: each calendar becomes a project node so events
        # naturally cluster by calendar context.
        cal_proj_id = f"project_calendar_{slugify(cal_name)}"
        DB.upsert_node(conn, {
            "id": cal_proj_id, "type": "Project", "family": "concept",
            "title": cal_name, "path": f"calendar://{cal_name}",
            "owner": "FOUNDER", "status": "active", "governance_tier": 0,
            "content_hash": None,
            "summary": f"macOS Calendar source: {cal_name}",
            "body_excerpt": None, "centrality": 0.0,
            "created": None, "last_modified": now_iso(), "last_reviewed": None,
            "metadata": {"source": "macos_calendar", "calendar_name": cal_name},
        })
        if DB.insert_edge(conn, {
            "from": node_id, "to": cal_proj_id,
            "predicate": "participates_in", "family": "social",
            "weight": 1.0, "confidence": 1.0,
            "asserted_by": "HARVEST_CAL_EVENTKIT",
            "asserted_at": asserted_at,
            "evidence_span": "eventkit-calendar-membership",
        }):
            edges += 1

        # Item 10: attendee participation edges
        try:
            attendees = list(ev.attendees() or [])
        except Exception:
            attendees = []
        for a in attendees:
            try:
                a_name = str(a.name()) if a.name() else None
            except Exception:
                a_name = None
            if not a_name:
                continue
            a_url = ""
            try:
                if a.URL():
                    a_url = str(a.URL())
            except Exception:
                pass
            email = a_url.lower().removeprefix("mailto:") if a_url.lower().startswith("mailto:") else ""
            contact_id = f"contact_{slugify(email or a_name)}"
            DB.upsert_node(conn, {
                "id": contact_id, "type": "Contact", "family": "concept",
                "title": a_name, "path": f"contact://{email or a_name}",
                "owner": "FOUNDER", "status": "active", "governance_tier": 0,
                "content_hash": None,
                "summary": f"Meeting attendee: {a_name}" + (f" <{email}>" if email else ""),
                "body_excerpt": None, "centrality": 0.0,
                "created": None, "last_modified": now_iso(), "last_reviewed": None,
                "metadata": {"source": "eventkit_attendee", "email": email},
            })
            if DB.insert_edge(conn, {
                "from": contact_id, "to": node_id,
                "predicate": "participates_in", "family": "social",
                "weight": 1.0, "confidence": 0.95,
                "asserted_by": "HARVEST_CAL_ATTENDEE",
                "asserted_at": asserted_at,
                "evidence_span": "eventkit-attendee",
            }):
                edges += 1

        # Item 10: conference URL cites
        url_search_text = (notes or "") + " " + (location or "")
        for m in _CONFERENCE_URL_RE.finditer(url_search_text):
            url = m.group(0).rstrip(".,;)]")
            url_id = f"url_{short_hash(url)}"
            DB.upsert_node(conn, {
                "id": url_id, "type": "URL", "family": "external",
                "title": url[:80], "path": url,
                "owner": "FOUNDER", "status": "active", "governance_tier": 0,
                "content_hash": None,
                "summary": f"Conference URL referenced from event {title!r}",
                "body_excerpt": None, "centrality": 0.0,
                "created": None, "last_modified": now_iso(), "last_reviewed": None,
                "metadata": {"source": "eventkit_notes", "url": url},
            })
            if DB.insert_edge(conn, {
                "from": node_id, "to": url_id,
                "predicate": "cites", "family": "evidence",
                "weight": 1.0, "confidence": 0.9,
                "asserted_by": "HARVEST_CAL_URL",
                "asserted_at": asserted_at,
                "evidence_span": "eventkit-conference-url",
            }):
                edges += 1
    return nodes, edges, _digest({"lookback_days": lookback_days,
                                  "lookahead_days": lookahead_days,
                                  "events": evidence})


def harvest(verbose: bool = True) -> dict:
    t0 = time.time()
    conn = DB.connect()
    asserted_at = now_iso()
    try:
        ap_n, ap_e, ap_h = harvest_approvals(conn, asserted_at)
        r_n, r_e, r_h = harvest_reminders(conn, asserted_at)
        c_n, c_e, c_h = harvest_calendar(conn, asserted_at)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    elapsed = time.time() - t0
    nodes = ap_n + r_n + c_n
    edges = ap_e + r_e + c_e
    if verbose:
        print(f"[harvest:events] approvals={ap_n}+e{ap_e} reminders={r_n} "
              f"calendar={c_n} | total {nodes} nodes, {edges} edges in {elapsed:.1f}s")
    source_digest = _digest({"approvals": ap_h, "reminders": r_h, "calendar": c_h})
    return {"family": "event", "status": "ok", "nodes": nodes, "edges": edges,
            "source_digest": source_digest, "event_watermark": source_digest,
            "elapsed_sec": round(elapsed, 2)}


if __name__ == "__main__":
    print(harvest())
