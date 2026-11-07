"""Localhost HTTP REST server exposing the brain (for non-MCP clients).

Endpoints:
  GET  /health                       → {ok, nodes, edges, edges_walkable, edges_dangling}
  GET  /status                       → full DB.stats output + edge_health census
  GET  /query?topic=X&radius=2&budget=4000[&families=document,code][&predicates=depends_on,cites][&tier=0]
             [&since=7d&before=2026-06-30&order_by=recency]
                                     → text/markdown bundle (default) or application/json with ?format=json
  GET  /search?text=X&limit=10[&since=30d&before=2026-06-30&order=recency]
                                     → JSON list of matching nodes (incl. last_modified, created)
  POST /query  body: {topic, radius, budget, families, predicates, tier, since, before, order_by}
                                     → JSON pack

  POST /validate    body: {file_path, content, tool_name?}   (aliases: path/file, body/new_string/text, tool)
                                     → {decision, errors[]}  — 'block' means DO NOT WRITE
  POST /check_risk  body: {command, tool_name?, file_path?}  (aliases: cmd/shell/action)
                                     → {decision, rule_id, message, amber_warnings[]}
                                       'block' means DO NOT EXECUTE

  The two POST enforcement routes are Rung 2 of the universe's enforcement floor
  (Cross-Harness Universe Contract, Class 3). They dispatch to `engine.enforcement`
  — the SAME functions the MCP tools `brain_validate_write` / `brain_check_risk`
  call — so any harness with a shell gets a verdict identical to an MCP host's.
  Before 2026-08-04 these gates were MCP-only, which made the "universal floor"
  a Claude-and-Codex gate in practice.

  Temporal params (since/before): ISO date '2026-06-01', ISO datetime, or relative
  span '7d' / '2w' / '3mo' / '1y' / '24h'. order_by/order = 'recency' sorts newest-first.

Run:
    python3 -m engine.http_server [--port 8767] [--bind 127.0.0.1]

Use cases:
  - Codex / Antigravity / Gemini sessions can curl this from inside a shell tool.
  - Browser extensions, n8n flows, AppleScript handlers, Raycast scripts.
"""

from __future__ import annotations
import os
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import argparse
import json
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from . import brain as B
from . import config as C
from . import db as DB
from . import enforcement as E

# /health TTL cache (perf, 2026-06-20). DB.stats() runs COUNT(*) over the ~2.9M-row
# edges table; on a cold page cache that is ~2.5 s. The endpoint is polled by the
# universe heartbeat (and the RAG hook's liveness check), so a stale-for-60s count is
# fine and keeps every poll off the COUNT path. Node/edge totals drift slowly.
_HEALTH_CACHE: dict = {"ts": 0.0, "observed_at_epoch": None, "data": None,
                       "refreshing": False, "error": None, "retry_after": 0.0}
_HEALTH_LOCK = threading.Lock()
_HEALTH_TTL = 60.0


def _health_payload(conn) -> dict:
    """The one and only builder for the /health body — both the request handler and
    _warmup() fill the shared TTL cache through here.

    They used to build it separately, and the moment /health learned to report walkable
    edges the warmer kept pre-filling the cache with the raw-total-only `DB.stats()`,
    so every daemon restart served the old shape for 60 s. One writer, one shape.

    `edges` is the RAW total and reads healthy even when the graph has hollowed out —
    the universe heartbeat polls this endpoint, so the walkable count belongs here too.
    That is one extra edges scan, amortised over the TTL. The full per-predicate census
    (dangling_by_predicate, fully_dangling_predicates) is on /status.
    """
    data = DB.stats(conn)
    from .capacity_admission import deployment_status
    data["capacity_admission"] = deployment_status()
    eh = DB.edge_health(conn)
    data["edges_walkable"] = eh["edges_walkable"]
    data["edges_dangling"] = eh["edges_dangling"]
    data["structural_walkable_pct"] = eh["structural_walkable_pct"]
    from .source_integrity import health as source_health
    integrity = source_health(conn)
    data["source_integrity"] = {k: v for k, v in integrity.items() if k != "metadata_by_type"}
    return data


def _refresh_health_cache() -> None:
    """One census worker; SQL never holds the publication lock."""
    try:
        conn = DB.connect()
        try:
            data = _health_payload(conn)
        finally:
            conn.close()
    except Exception as exc:
        with _HEALTH_LOCK:
            _HEALTH_CACHE.update(error=type(exc).__name__, refreshing=False,
                                retry_after=time.monotonic() + _HEALTH_TTL)
        return
    with _HEALTH_LOCK:
        _HEALTH_CACHE.update(data=data, ts=time.monotonic(),
                            observed_at_epoch=time.time(), error=None,
                            refreshing=False, retry_after=0.0)


def _health_snapshot() -> dict:
    """Serve observed diagnostics immediately; one worker refreshes expired data."""
    now = time.monotonic()
    with _HEALTH_LOCK:
        expired = (_HEALTH_CACHE["data"] is None
                   or now - _HEALTH_CACHE["ts"] > _HEALTH_TTL)
        launch = (expired and now >= _HEALTH_CACHE["retry_after"]
                  and not _HEALTH_CACHE["refreshing"])
        if launch:
            _HEALTH_CACHE["refreshing"] = True
    if launch:
        try:
            threading.Thread(target=_refresh_health_cache,
                             name="brain-health-refresh", daemon=True).start()
        except Exception as exc:
            with _HEALTH_LOCK:
                _HEALTH_CACHE.update(error=type(exc).__name__, refreshing=False,
                                    retry_after=now + _HEALTH_TTL)
    with _HEALTH_LOCK:
        data = _HEALTH_CACHE["data"]
        age = max(0.0, now - _HEALTH_CACHE["ts"]) if data is not None else None
        return {
            **(dict(data) if data is not None else {}),
            "diagnostics_available": data is not None,
            "diagnostic_freshness": ("unknown" if data is None else
                                    "stale" if age > _HEALTH_TTL else "fresh"),
            "observed_at_epoch": _HEALTH_CACHE["observed_at_epoch"],
            "age_seconds": age,
            "refreshing": _HEALTH_CACHE["refreshing"],
            "refresh_error": _HEALTH_CACHE["error"],
        }

VIEWER_DIR = Path(__file__).parent / "graph_viewer"
VIEWER_ASSETS = {
    "viewer.js": "application/javascript; charset=utf-8",
    "viewer.css": "text/css; charset=utf-8",
    "index.html": "text/html; charset=utf-8",
}
# Static drops served from the LLM wiki. Apple Shortcuts on iOS expects the
# .shortcut MIME to be octet-stream and the URL extension to be ".shortcut"
# (the Shortcuts.app file handler keys off the extension, not the MIME).
LLM_WIKI_DIR = Path(
    os.environ.get("ARCTURION_UNIVERSE_ROOT",
                   str(C.WORK_ROOT / "Desktop" / "ARCTURION_HQ"))
) / "06_MEMORY_SYSTEM" / "0611_LLM_WIKI"
STATIC_DROPS = {
    "/Ask-Brain.shortcut": (
        LLM_WIKI_DIR / "Ask-Brain.shortcut",
        "application/octet-stream",
    ),
}


def _csv(qs: dict, key: str) -> list[str] | None:
    if key not in qs:
        return None
    raw = qs[key][0]
    return [x.strip() for x in raw.split(",") if x.strip()] or None


class Handler(BaseHTTPRequestHandler):
    server_version = "ArcturionBrain/1.0"

    def log_message(self, fmt, *args):
        # Silence default access log — heartbeat health checks would spam stderr.
        return

    def _send(self, status: int, body: str | bytes, ctype: str = "text/plain; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if urlparse(self.path).path.rstrip("/") == "/changes":
                self.send_header("Cache-Control", "no-store")
            else:
                self.send_header("Access-Control-Allow-Origin", "*")
            if urlparse(self.path).path.rstrip("/") in ("/query","/search","/receipt","/graph_subset","/changes"):
                self.send_header("X-Brain-Coverage","partial; sources outside admitted assignment scope excluded")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # Client hung up before we finished writing — e.g. the RAG hook's /query
            # timeout (1.5s) firing on a slow call. Nothing to send to a gone client;
            # swallow it so it doesn't spew a traceback into the daemon error log.
            pass

    def _json(self, status: int, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False, indent=2),
                   "application/json; charset=utf-8")

    def do_GET(self):
        try:
            return self._do_GET()
        except ValueError as exc:
            return self._json(400, {"status":"error","error":str(exc)})

    def _do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = u.path.rstrip("/")

        if path == "/changes":
            import ipaddress
            # Existing shared-account loopback baseline, not requester authentication.
            if not ipaddress.ip_address(self.server.server_address[0]).is_loopback:
                return self._json(403, {"status": "error", "error": "loopback_required"})
            if self.headers.get_all("Origin") is not None:
                return self._json(403, {"status": "error", "error": "origin_not_allowed"})
            hosts = self.headers.get_all("Host", [])
            address, port = self.server.server_address[:2]
            authority = ("[" + address + "]") if ":" in address else address
            allowed_hosts = {authority.lower() + ":" + str(port), "localhost:" + str(port)}
            if len(hosts) != 1 or hosts[0].lower() not in allowed_hosts:
                return self._json(403, {"status": "error", "error": "loopback_host_required"})
            from . import config as C
            if not C.CHANGE_FEED_ENABLED:
                return self._json(503, {"status": "unavailable", "error": "change_feed_disabled"})
            from .change_feed import http_response
            params = parse_qs(u.query, keep_blank_values=True, strict_parsing=True)
            return self._json(200, http_response(params))
        if path == "" or path == "/health":
            return self._json(200, {"ok": True, **_health_snapshot()})
        if path == "/status":
            return self._json(200, B.status())
        if path == "/receipt":
            try:
                return self._json(200, B.capture_receipt((qs.get("path") or [""])[0], capacity=(qs.get("capacity") or ["business"])[0], project_id=(qs.get("project_id") or [None])[0]))
            except ValueError as exc:
                return self._json(400, {"status": "error", "error": str(exc)})
        if path == "/search":
            text = (qs.get("text") or [""])[0]
            limit = int((qs.get("limit") or ["10"])[0])
            if not text:
                return self._json(400, {"error": "missing text"})
            # Optional temporal filter + ordering (last_modified). since/before accept
            # ISO dates/datetimes or relative spans (7d, 2w, 3mo, 1y); order=recency.
            since = B.resolve_time((qs.get("since") or [None])[0])
            before = B.resolve_time((qs.get("before") or [None])[0], end=True)
            order = (qs.get("order") or ["centrality"])[0]
            include_telemetry = (qs.get("include_telemetry") or ["false"])[0]
            if include_telemetry not in ("true", "false"):
                raise ValueError("include_telemetry must be true or false")
            conn = B.scoped_connection((qs.get("capacity") or ["business"])[0], (qs.get("project_id") or [None])[0], include_telemetry=include_telemetry == "true")
            try:
                q = f"%{text.lower()}%"
                where = ["(lower(title) LIKE ? OR lower(summary) LIKE ?)"]
                params: list = [q, q]
                if since:
                    where.append("last_modified >= ?")
                    params.append(since)
                if before:
                    where.append("last_modified <= ?")
                    params.append(before)
                order_sql = "last_modified DESC" if order == "recency" else "centrality DESC"
                rows = conn.execute(
                    "SELECT id, title, type, family, path, summary, centrality, "
                    "last_modified, created, content_hash, body_excerpt FROM nodes "
                    f"WHERE {' AND '.join(where)} ORDER BY {order_sql} LIMIT ?",
                    (*params, limit),
                ).fetchall()
                from .source_integrity import filter_retrieval
                rows = filter_retrieval(conn, [dict(r) for r in rows], historical=bool(before))
                for r in rows:
                    r.pop("body_excerpt", None)
                    r.pop("content_hash", None)
            finally:
                conn.close()
            return self._json(200, [dict(r) for r in rows])
        if path == "/viewer" or path == "/viewer/index.html":
            return self._serve_viewer_asset("index.html")
        if path.startswith("/viewer/"):
            asset = path.removeprefix("/viewer/")
            return self._serve_viewer_asset(asset)
        if path in STATIC_DROPS:
            return self._serve_static_drop(path)
        if path == "/m" or path == "/m/" or path == "/ask":
            return self._serve_mobile_page()
        if path == "/graph_subset":
            return self._serve_graph_subset(qs)
        if path == "/metrics":
            try:
                from . import metrics as _M
                days = int((qs.get("days") or ["7"])[0])
                return self._json(200, _M.aggregate(window_days=days))
            except Exception as e:
                return self._json(500, {"error": str(e)})
        if path == "/query":
            topic = (qs.get("topic") or [""])[0]
            since = (qs.get("since") or [None])[0]
            before = (qs.get("before") or [None])[0]
            order_by = (qs.get("order_by") or ["relevance"])[0]
            # Empty topic is allowed for a pure temporal listing (since/before/recency).
            if not topic and not (since or before or order_by == "recency"):
                return self._send(400, "missing topic (or pass since/before/order_by=recency for a temporal listing)")
            radius = int((qs.get("radius") or ["2"])[0])
            budget = int((qs.get("budget") or ["2000"])[0])
            tier = int((qs.get("tier") or ["0"])[0])
            families = _csv(qs, "families")
            predicates = _csv(qs, "predicates")
            fmt = (qs.get("format") or ["text"])[0]
            pack = B.query(topic=topic, radius=radius, token_budget=budget,
                           families=families, predicates=predicates,
                           governance_tier_min=tier, since=since, before=before,
                           order_by=order_by, source="http",
                           capacity=(qs.get("capacity") or ["business"])[0],
                           project_id=(qs.get("project_id") or [None])[0],
                           desk=(qs.get("desk") or [None])[0],
                           desk_hops=int(qs["desk_hops"][0]) if "desk_hops" in qs else None,
                           owner=(qs.get("owner") or [None])[0],
                           owner_strict=(qs.get("owner_strict") or ["false"])[0].lower() == "true")
            if fmt == "json":
                return self._json(200, pack.to_dict())
            return self._send(200, pack.text_bundle, "text/markdown; charset=utf-8")
        return self._send(404, "not found")

    def _serve_viewer_asset(self, asset: str):
        if asset not in VIEWER_ASSETS:
            return self._send(404, "not found")
        p = VIEWER_DIR / asset
        if not p.exists():
            return self._send(404, f"viewer asset missing: {asset}")
        try:
            data = p.read_bytes()
        except Exception as e:
            return self._send(500, f"read error: {e}")
        return self._send(200, data, VIEWER_ASSETS[asset])

    def _serve_static_drop(self, path: str):
        p, ctype = STATIC_DROPS[path]
        if not p.exists():
            return self._send(404, f"missing: {p.name}")
        try:
            data = p.read_bytes()
        except Exception as e:
            return self._send(500, f"read error: {e}")
        # Suggest a download filename so iOS Safari hands off to Shortcuts.app.
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         f'attachment; filename="{p.name}"')
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _serve_mobile_page(self):
        # Minimal mobile-optimized query page. iPhone-friendly fallback when the
        # Shortcut isn't installed yet. Add-to-Home-Screen-able.
        html = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>Ask Brain</title>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Ask Brain">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<style>
:root { --bg:#0b0e14; --panel:#131722; --text:#d1d4dc; --accent:#f7931a; --border:#2a2e3a; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 -apple-system,system-ui,sans-serif; padding: env(safe-area-inset-top) 14px env(safe-area-inset-bottom); }
h1 { font-size:18px; color:var(--accent); margin:14px 0 8px; }
form { display:flex; gap:8px; }
input[type=text] { flex:1; background:var(--panel); color:var(--text); border:1px solid var(--border); border-radius:8px; padding:10px 12px; font:15px -apple-system,system-ui; }
button { background:var(--accent); color:#000; border:none; border-radius:8px; padding:10px 14px; font:600 14px -apple-system,system-ui; }
pre { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:10px 12px; white-space:pre-wrap; word-break:break-word; font:12px ui-monospace,SF Mono,monospace; max-height:75vh; overflow:auto; }
small { color:#787b86; }
</style></head><body>
<h1>🧠 Ask Brain</h1>
<form id="f"><input type="text" id="q" placeholder="What do you want to know?" autofocus><button>Ask</button></form>
<small id="s"></small>
<pre id="r"></pre>
<script>
const f=document.getElementById('f'),q=document.getElementById('q'),r=document.getElementById('r'),s=document.getElementById('s');
f.addEventListener('submit',async e=>{
  e.preventDefault(); if(!q.value.trim()) return;
  s.textContent='querying…'; r.textContent='';
  const t=Date.now();
  try {
    const resp=await fetch('/query?topic='+encodeURIComponent(q.value.trim())+'&radius=2&budget=2000');
    const txt=await resp.text();
    r.textContent=txt;
    s.textContent=`${txt.length} chars · ${Date.now()-t} ms`;
  } catch(err){ s.textContent='error: '+err.message; }
});
</script>
</body></html>"""
        return self._send(200, html, "text/html; charset=utf-8")

    def _serve_graph_subset(self, qs):
        """Filtered subset suitable for the browser viewer.

        Query params:
          families=document,code,... (default: all node families)
          predicate_families=code,structure,... (default: all except 'semantic')
          min_centrality=0.001 (default: 0)
          limit=500 (default: 500, max: 5000)
          search=substr (default: none)
        """
        families = _csv(qs, "families")
        predicate_families = _csv(qs, "predicate_families")
        try:
            min_cent = float((qs.get("min_centrality") or ["0"])[0])
        except ValueError:
            min_cent = 0.0
        try:
            limit = int((qs.get("limit") or ["500"])[0])
        except ValueError:
            limit = 500
        limit = max(50, min(5000, limit))
        search = (qs.get("search") or [""])[0].strip().lower()

        conn = B.scoped_connection((qs.get("capacity") or ["business"])[0], (qs.get("project_id") or [None])[0])
        try:
            where = ["centrality >= ?", "status != 'superseded'"]
            params: list = [min_cent]
            if families:
                placeholders = ",".join("?" * len(families))
                where.append(f"family IN ({placeholders})")
                params.extend(families)
            if search:
                where.append("(lower(title) LIKE ? OR lower(summary) LIKE ?)")
                q = f"%{search}%"
                params.extend([q, q])
            where_sql = " AND ".join(where)
            node_rows = conn.execute(
                f"""SELECT id, family, type, title, centrality, governance_tier, path, summary, body_excerpt, content_hash
                    FROM nodes WHERE {where_sql}
                    ORDER BY centrality DESC LIMIT ?""",
                (*params, limit),
            ).fetchall()
            from .source_integrity import filter_retrieval
            node_rows = filter_retrieval(conn, [dict(r) for r in node_rows])
            for r in node_rows:
                for field in ("path", "summary", "body_excerpt", "content_hash"):
                    r.pop(field, None)
            node_ids = [r["id"] for r in node_rows]
            if not node_ids:
                return self._json(200, {"nodes": [], "links": []})
            placeholders = ",".join("?" * len(node_ids))
            edge_where = [
                f"from_id IN ({placeholders})",
                f"to_id IN ({placeholders})",
            ]
            edge_params: list = [*node_ids, *node_ids]
            if predicate_families is None:
                edge_where.append("family != 'semantic'")
            else:
                pf_ph = ",".join("?" * len(predicate_families))
                edge_where.append(f"family IN ({pf_ph})")
                edge_params.extend(predicate_families)
            edge_rows = conn.execute(
                f"""SELECT from_id, to_id, predicate, family, weight, confidence
                    FROM edges
                    WHERE {" AND ".join(edge_where)}
                    LIMIT 20000""",
                edge_params,
            ).fetchall()
        finally:
            conn.close()
        return self._json(200, {
            "nodes": [
                {
                    "id": r["id"],
                    "family": r["family"],
                    "type": r["type"],
                    "title": r["title"],
                    "centrality": r["centrality"],
                    "governance_tier": r["governance_tier"],
                }
                for r in node_rows
            ],
            "links": [
                {
                    "from": r["from_id"],
                    "to": r["to_id"],
                    "predicate": r["predicate"],
                    "family": r["family"],
                    "weight": r["weight"],
                    "confidence": r["confidence"],
                }
                for r in edge_rows
            ],
        })

    def _read_json_body(self):
        """Return (payload, None) or (None, error_response). Shared by every POST route."""
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(body)
        except Exception:
            return None, self._send(400, "bad json")
        if not isinstance(payload, dict):
            return None, self._send(400, "bad json: body must be a JSON object")
        return payload, None

    def _serve_gate(self, tool: str, payload: dict):
        """POST /validate and POST /check_risk — Rung 2 of the enforcement floor.

        Delegates to `engine.enforcement.run_gate`, which is the identical
        normalize→require→dispatch path the MCP transport uses. This handler
        deliberately contains NO policy: any rule logic living here would be a
        second floor that drifts from the first.

        HTTP status is always 200 on a well-formed request, including for
        `decision: "block"` — a block is a *verdict*, not a transport error, and
        a curl caller using `-f` must still be able to read the reason. A
        malformed request (missing required field) returns 400 with the same
        readable contract MCP returns.
        """
        try:
            result = E.run_gate(tool, payload)
        except Exception as e:
            # Fail-open matches the hook contract (availability over strictness;
            # agent-side hooks are defense in depth) but must never be silent.
            print(f"[brain-http] gate {tool} error, fail-open: {type(e).__name__}: {e}",
                  flush=True)
            return self._json(500, {"error": f"{type(e).__name__}: {e}", "tool": tool})
        if result.get("error") == "missing_required_arguments":
            return self._json(400, result)
        return self._json(200, result)

    def do_POST(self):
        try:
            return self._do_POST()
        except ValueError as exc:
            return self._json(400, {"status":"error","error":str(exc)})

    def _do_POST(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/")
        if path in ("/validate", "/check_risk"):
            payload, err = self._read_json_body()
            if payload is None:
                return err
            tool = "brain_validate_write" if path == "/validate" else "brain_check_risk"
            return self._serve_gate(tool, payload)
        if path != "/query":
            return self._send(404, "not found")
        payload, err = self._read_json_body()
        if payload is None:
            return err
        topic = payload.get("topic") or ""
        since = payload.get("since")
        before = payload.get("before")
        order_by = payload.get("order_by", "relevance")
        if not topic and not (since or before or order_by == "recency"):
            return self._send(400, "missing topic (or pass since/before/order_by=recency for a temporal listing)")
        pack = B.query(
            topic=topic,
            radius=int(payload.get("radius", 2)),
            token_budget=int(payload.get("budget", 2000)),
            families=payload.get("families"),
            predicates=payload.get("predicates"),
            governance_tier_min=int(payload.get("tier", 0)),
            since=payload.get("since"),
            before=payload.get("before"),
            order_by=payload.get("order_by", "relevance"),
            source="http",
            capacity=payload.get("capacity","business"),
            project_id=payload.get("project_id"),
            desk=payload.get("desk"),
            desk_hops=payload.get("desk_hops"),
            owner=payload.get("owner"),
            owner_strict=payload.get("owner_strict", False) is True,
        )
        return self._json(200, pack.to_dict())


def _warmup():
    """Pre-fault the query working set into mmap and pre-fill the /health cache so the
    first real request after a (rare) daemon restart is already warm. Runs in a daemon
    thread so serve_forever() starts accepting connections immediately. Best-effort."""
    try:
        _health_snapshot()  # One builder/refresh path shared with health requests.
        # Retrieval v2's query encoder costs ~5 s to import + load, ONCE per process, and
        # `vector_seed.similarity` deliberately refuses to block on it — a query arriving
        # mid-load degrades to lexical-only rather than stalling behind it. That makes loading
        # it HERE, off the request path, what decides how long after a restart the Brain is
        # answering at full quality. Measured 2026-08-04: with the load in the request path,
        # cold p95 was 6,497 ms against a RAG hook that gives up at 1,500 ms.
        try:
            from . import vector_seed as VS
            VS.prewarm()
        except Exception:
            pass
        # A hub topic touches the FTS index + a broad slice of the edge indexes, so one
        # throwaway query leaves the hot pages resident for subsequent real queries.
        B.query(topic="infrastructure", radius=2, token_budget=200, source="warmup")
    except Exception:
        pass


def main():
    import threading
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--bind", default="127.0.0.1")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    # This process is the fleet's per-turn RAG path (hook timeout 1.5 s), so it opts into
    # degrading to lexical-only while the query encoder loads instead of queueing behind it.
    # Deliberately NOT the library default — see vector_seed._NONBLOCKING.
    try:
        from . import vector_seed as VS
        VS.set_nonblocking(True)
    except Exception:
        pass
    threading.Thread(target=_warmup, name="brain-http-warmup", daemon=True).start()
    print(f"[brain-http] listening on http://{args.bind}:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
