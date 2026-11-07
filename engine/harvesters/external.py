"""External nodes — Chrome bookmarks (always free, JSON), Safari bookmarks (plist),
Raindrop optional (requires API token from 1Password).

We always have Chrome bookmarks; Raindrop is best-effort.
"""

from __future__ import annotations
import hashlib
import json
import plistlib
import subprocess
import time
from pathlib import Path
from .. import config as C
from .. import db as DB
from ..util import slugify, short_hash, now_iso


CHROME_BOOKMARKS = C.WORK_ROOT / "browser-data/chrome-bookmarks"
SAFARI_BOOKMARKS = C.WORK_ROOT / "browser-data/safari-bookmarks.plist"


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()).hexdigest()


def _walk_chrome(node, out, parent_folder=""):
    if not isinstance(node, dict):
        return
    if node.get("type") == "url":
        out.append({
            "title": node.get("name") or "",
            "url": node.get("url") or "",
            "folder": parent_folder,
            "id": node.get("guid") or node.get("id") or "",
        })
    elif node.get("type") == "folder":
        new_parent = f"{parent_folder}/{node.get('name', '')}" if parent_folder else (node.get("name") or "")
        for child in (node.get("children") or []):
            _walk_chrome(child, out, new_parent)


def harvest_chrome(conn, asserted_at: str, limit: int | None = None) -> tuple[int, int, str]:
    if not CHROME_BOOKMARKS.exists():
        return 0, 0, _digest({"source": str(CHROME_BOOKMARKS), "state": "absent"})
    try:
        raw = CHROME_BOOKMARKS.read_bytes()
        data = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"cannot read Chrome bookmarks: {exc}") from exc
    out: list[dict] = []
    for root_key in ("bookmark_bar", "other", "synced"):
        _walk_chrome((data.get("roots") or {}).get(root_key), out, root_key)
    if limit:
        out = out[:limit]
    nodes = edges = 0
    for b in out:
        if not b["url"]:
            continue
        bid = f"bookmark_chrome_{short_hash(b['url'])}"
        DB.upsert_node(conn, {
            "id": bid, "type": "Bookmark", "family": "external",
            "title": (b["title"] or b["url"])[:160], "path": b["url"],
            "owner": "FOUNDER", "status": "active", "governance_tier": 0,
            "content_hash": None,
            "summary": f"Chrome bookmark in '{b['folder']}'",
            "body_excerpt": None, "centrality": 0.0,
            "created": None, "last_modified": now_iso(), "last_reviewed": None,
            "metadata": {"folder": b["folder"], "url": b["url"], "browser": "chrome"},
        })
        nodes += 1
        if DB.insert_edge(conn, {
            "from": bid, "to": "tool_chrome", "predicate": "bookmarked_in",
            "family": "external", "weight": 1.0, "confidence": 1.0,
            "asserted_by": "HARVEST_EXT_CHROME", "asserted_at": asserted_at,
            "evidence_span": "Chrome/Default/Bookmarks",
        }):
            edges += 1
    return nodes, edges, hashlib.sha256(raw).hexdigest()


def harvest_raindrop(conn, asserted_at: str) -> tuple[int, int, str]:
    """Best-effort: only if 1Password has 'Raindrop API Token'."""
    try:
        proc = subprocess.run(
            [str(C.PROVIDER_CLI), "item", "get",
             "Raindrop API Token", "--fields", "credential", "--reveal"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        token = proc.stdout.strip() if proc.returncode == 0 else ""
    except FileNotFoundError:
        return 0, 0, _digest({"source": "raindrop", "state": "credential_provider_absent"})
    except Exception as exc:
        raise RuntimeError(f"Raindrop credential lookup failed: {exc}") from exc
    if not token:
        return 0, 0, _digest({"source": "raindrop", "state": "credentials_absent"})
    import urllib.request
    try:
        req = urllib.request.Request(
            "https://api.raindrop.io/rest/v1/raindrops/0?perpage=50",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as exc:
        raise RuntimeError(f"Raindrop source query failed: {exc}") from exc
    nodes = edges = 0
    for item in (data.get("items") or []):
        url = item.get("link") or ""
        if not url:
            continue
        bid = f"bookmark_raindrop_{short_hash(url)}"
        DB.upsert_node(conn, {
            "id": bid, "type": "Bookmark", "family": "external",
            "title": (item.get("title") or url)[:160], "path": url,
            "owner": "FOUNDER", "status": "active", "governance_tier": 0,
            "content_hash": None,
            "summary": (item.get("excerpt") or "")[:300],
            "body_excerpt": None, "centrality": 0.0,
            "created": item.get("created"), "last_modified": now_iso(),
            "last_reviewed": None,
            "metadata": {"collection_id": item.get("collectionId"),
                         "tags": item.get("tags") or [], "url": url},
        })
        nodes += 1
        if DB.insert_edge(conn, {
            "from": bid, "to": "tool_raindrop", "predicate": "bookmarked_in",
            "family": "external", "weight": 1.0, "confidence": 1.0,
            "asserted_by": "HARVEST_EXT_RAINDROP", "asserted_at": asserted_at,
            "evidence_span": "raindrop.io api",
        }):
            edges += 1
    return nodes, edges, _digest(data)


def harvest_safari(conn, asserted_at: str) -> tuple[int, int, str]:
    if not SAFARI_BOOKMARKS.exists():
        return 0, 0, _digest({"source": str(SAFARI_BOOKMARKS), "state": "absent"})
    try:
        raw = SAFARI_BOOKMARKS.read_bytes()
        data = plistlib.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"cannot read Safari bookmarks: {exc}") from exc
    nodes = edges = 0

    def _walk(node, folder=""):
        nonlocal nodes, edges
        if not isinstance(node, dict):
            return
        if node.get("WebBookmarkType") == "WebBookmarkTypeLeaf":
            url = node.get("URLString") or ""
            title = (node.get("URIDictionary") or {}).get("title") or url
            if not url:
                return
            bid = f"bookmark_safari_{short_hash(url)}"
            DB.upsert_node(conn, {
                "id": bid, "type": "Bookmark", "family": "external",
                "title": title[:160], "path": url, "owner": "FOUNDER",
                "status": "active", "governance_tier": 0, "content_hash": None,
                "summary": f"Safari bookmark in '{folder}'",
                "body_excerpt": None, "centrality": 0.0,
                "created": None, "last_modified": now_iso(), "last_reviewed": None,
                "metadata": {"folder": folder, "url": url, "browser": "safari"},
            })
            nodes += 1
            if DB.insert_edge(conn, {
                "from": bid, "to": "tool_safari", "predicate": "bookmarked_in",
                "family": "external", "weight": 1.0, "confidence": 1.0,
                "asserted_by": "HARVEST_EXT_SAFARI", "asserted_at": asserted_at,
                "evidence_span": "Safari/Bookmarks.plist",
            }):
                edges += 1
        elif node.get("WebBookmarkType") == "WebBookmarkTypeList":
            new_folder = f"{folder}/{node.get('Title', '')}" if folder else (node.get("Title") or "")
            for child in (node.get("Children") or []):
                _walk(child, new_folder)

    _walk(data)
    return nodes, edges, hashlib.sha256(raw).hexdigest()


def harvest_pocket(conn, asserted_at: str) -> tuple[int, int, str]:
    """Best-effort: only if 1Password has 'Pocket Consumer Key' + 'Pocket Access Token'.

    Item 15. Halts gracefully when credentials are absent — does NOT generate.
    """
    try:
        consumer_proc = subprocess.run(
            [str(C.PROVIDER_CLI), "item", "get",
             "Pocket Consumer Key", "--fields", "credential", "--reveal"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        access_proc = subprocess.run(
            [str(C.PROVIDER_CLI), "item", "get",
             "Pocket Access Token", "--fields", "credential", "--reveal"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        consumer = consumer_proc.stdout.strip() if consumer_proc.returncode == 0 else ""
        access = access_proc.stdout.strip() if access_proc.returncode == 0 else ""
    except FileNotFoundError:
        return 0, 0, _digest({"source": "pocket", "state": "credential_provider_absent"})
    except Exception as exc:
        raise RuntimeError(f"Pocket credential lookup failed: {exc}") from exc
    if not consumer or not access:
        return 0, 0, _digest({"source": "pocket", "state": "credentials_absent"})
    import urllib.request
    try:
        req = urllib.request.Request(
            "https://getpocket.com/v3/get",
            data=json.dumps({
                "consumer_key": consumer, "access_token": access,
                "detailType": "complete", "count": 100,
            }).encode(),
            headers={"Content-Type": "application/json; charset=UTF-8",
                     "X-Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as exc:
        raise RuntimeError(f"Pocket source query failed: {exc}") from exc
    nodes = edges = 0
    items = data.get("list") or {}
    if isinstance(items, dict):
        items = list(items.values())
    for item in items:
        url = item.get("resolved_url") or item.get("given_url") or ""
        if not url:
            continue
        bid = f"bookmark_pocket_{short_hash(url)}"
        DB.upsert_node(conn, {
            "id": bid, "type": "Article", "family": "external",
            "title": (item.get("resolved_title") or item.get("given_title") or url)[:160],
            "path": url, "owner": "FOUNDER", "status": "active",
            "governance_tier": 0, "content_hash": None,
            "summary": (item.get("excerpt") or "")[:300],
            "body_excerpt": None, "centrality": 0.0,
            "created": item.get("time_added"), "last_modified": now_iso(),
            "last_reviewed": None,
            "metadata": {"item_id": item.get("item_id"), "url": url,
                         "word_count": item.get("word_count"),
                         "tags": list((item.get("tags") or {}).keys())},
        })
        nodes += 1
        if DB.insert_edge(conn, {
            "from": bid, "to": "tool_pocket", "predicate": "bookmarked_in",
            "family": "external", "weight": 1.0, "confidence": 1.0,
            "asserted_by": "HARVEST_EXT_POCKET", "asserted_at": asserted_at,
            "evidence_span": "getpocket.com/v3/get",
        }):
            edges += 1
    return nodes, edges, _digest(data)


def harvest_zotero(conn, asserted_at: str) -> tuple[int, int, str]:
    """Read ~/Zotero/zotero.sqlite directly. Item 15.

    Only runs if the Zotero local DB exists. Read-only open so no conflict
    with running Zotero app.
    """
    import sqlite3
    from pathlib import Path
    zot_db = C.WORK_ROOT / "Zotero" / "zotero.sqlite"
    if not zot_db.exists():
        return 0, 0, _digest({"source": str(zot_db), "state": "absent"})
    try:
        conn_zot = sqlite3.connect(f"file:{zot_db}?mode=ro&immutable=1", uri=True, timeout=5)
        conn_zot.row_factory = sqlite3.Row
    except Exception as exc:
        raise RuntimeError(f"cannot open Zotero source: {exc}") from exc
    nodes = edges = 0
    try:
        rows = conn_zot.execute("""
            SELECT i.itemID, i.dateAdded,
              MAX(CASE WHEN f.fieldName='title' THEN idv.value END) AS title,
              MAX(CASE WHEN f.fieldName='url' THEN idv.value END) AS url,
              MAX(CASE WHEN f.fieldName='DOI' THEN idv.value END) AS doi,
              MAX(CASE WHEN f.fieldName='abstractNote' THEN idv.value END) AS abstract
            FROM items i
            LEFT JOIN itemData id ON id.itemID = i.itemID
            LEFT JOIN fields f ON f.fieldID = id.fieldID
            LEFT JOIN itemDataValues idv ON idv.valueID = id.valueID
            WHERE i.itemTypeID NOT IN (
              SELECT itemTypeID FROM itemTypes WHERE typeName IN ('attachment','note'))
            GROUP BY i.itemID
            LIMIT 1000
        """).fetchall()
    except Exception as exc:
        conn_zot.close()
        raise RuntimeError(f"cannot query Zotero source: {exc}") from exc
    finally:
        try:
            conn_zot.close()
        except Exception:
            pass
    for r in rows:
        title = r["title"] or "(untitled)"
        url = r["url"] or (f"doi:{r['doi']}" if r["doi"] else "")
        zid = r["itemID"]
        bid = f"paper_zotero_{zid}"
        DB.upsert_node(conn, {
            "id": bid, "type": "Paper", "family": "external",
            "title": title[:160], "path": url or f"zotero://item/{zid}",
            "owner": "FOUNDER", "status": "active", "governance_tier": 0,
            "content_hash": None,
            "summary": (r["abstract"] or "")[:400],
            "body_excerpt": None, "centrality": 0.0,
            "created": r["dateAdded"], "last_modified": now_iso(),
            "last_reviewed": None,
            "metadata": {"zotero_item_id": zid, "url": url, "doi": r["doi"]},
        })
        nodes += 1
        if DB.insert_edge(conn, {
            "from": bid, "to": "tool_zotero", "predicate": "bookmarked_in",
            "family": "external", "weight": 1.0, "confidence": 1.0,
            "asserted_by": "HARVEST_EXT_ZOTERO", "asserted_at": asserted_at,
            "evidence_span": "zotero.sqlite",
        }):
            edges += 1
    evidence = [{k: r[k] for k in r.keys()} for r in rows]
    return nodes, edges, _digest(evidence)


def harvest(verbose: bool = True) -> dict:
    t0 = time.time()
    conn = DB.connect()
    asserted_at = now_iso()
    try:
        c_n, c_e, c_h = harvest_chrome(conn, asserted_at)
        s_n, s_e, s_h = harvest_safari(conn, asserted_at)
        r_n, r_e, r_h = harvest_raindrop(conn, asserted_at)
        p_n, p_e, p_h = harvest_pocket(conn, asserted_at)
        z_n, z_e, z_h = harvest_zotero(conn, asserted_at)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    elapsed = time.time() - t0
    nodes = c_n + s_n + r_n + p_n + z_n
    edges = c_e + s_e + r_e + p_e + z_e
    if verbose:
        print(f"[harvest:external] chrome={c_n}+e{c_e} safari={s_n}+e{s_e} "
              f"raindrop={r_n}+e{r_e} pocket={p_n}+e{p_e} zotero={z_n}+e{z_e} "
              f"| total {nodes} nodes, {edges} edges in {elapsed:.1f}s")
    source_digest = _digest({"chrome": c_h, "safari": s_h, "raindrop": r_h,
                             "pocket": p_h, "zotero": z_h})
    return {"family": "external", "status": "ok", "nodes": nodes, "edges": edges,
            "source_digest": source_digest,
            "elapsed_sec": round(elapsed, 2)}


if __name__ == "__main__":
    print(harvest())
