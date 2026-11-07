"""Verified journals for real records: decisions, project status and handoffs.

The graph-context pattern from the staged pilot, applied to real files. Each journal is a
typed record DERIVED from one source file. It quotes the source verbatim (never writes a
fact of its own), pins the source's SHA-256 and size, and links back to it:

    source --supports--> journal        (inserted by `link_edges`, asserted_by=journal-projection)
    journal --evidenced_by--> source    (frontmatter, survives every reindex)
    journal --supersedes--> older node  (frontmatter, ONLY for pairs the judge answered YES)

Journals are ordinary Markdown files under `<BRAIN_ROOT>/journals/<record_type>/`, so the
walker indexes them like any source and the nightly reindex/GC keeps them. A journal whose
source bytes changed is rebuilt; a journal whose source vanished is deleted.

`supersedes` is a RANKING signal (see `brain` / `rank_signals.journal_superseded`). It does
not write `valid_until`, which stays governance-owned (`maintenance.validity_pass`).

Run:
    python3 -m engine.journals build [--root DIR] [--dry-run]
    python3 -m engine.journals candidates      # (re)compute judge candidates for changed journals
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config as C

SCHEMA = "brain.journal.v1"
JOURNAL_ROOT = Path(os.environ.get("ARCTURION_BRAIN_JOURNAL_ROOT", str(C.BRAIN_ROOT / "journals")))
STATE_DIR = Path(os.environ.get("ARCTURION_BRAIN_JOURNAL_STATE", str(C.GRAPH_DIR / "journals")))

# Record types, chosen by stable source shelves (never by topic words).
RECORD_TYPES = (
    ("decision", "%/Memory/Decisions/%.md"),
    ("project_status", "%/Memory/Projects/%.md"),
    ("handoff", "%/HANDOFF%.md"),
)
# Ranking-only historical-copy classes; see rank_signals.is_historical_copy.
_STATE_RE = re.compile(
    r"\b(OPEN|SHIPPED|BLOCKED|DONE|LIVE|FIXED|CLOSED|PENDING|RESOLVED|SUPERSEDED|RETIRED|"
    r"FROZEN|HALTED|PAUSED|APPROVED|DENIED|LANDMINE|Landmine|Status|Next|GO)\b")
_ISO = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
_TRAILING_DATE = re.compile(r"[\s_\-]*20\d{2}[\-_]?\d{2}[\-_]?\d{2}\s*$")
MAX_STATE_LINES = 8


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def stem_key(path: str) -> str:
    """Basename without extension and trailing date decoration, lower-cased."""
    base = Path(path).stem
    base = re.sub(r"^20\d{2}[\-_]?\d{2}[\-_]?\d{2}[\s_\-]*", "", base)
    return _TRAILING_DATE.sub("", base).strip().lower()


def _as_of(fm: dict, body: str, mtime: float) -> tuple[str, str]:
    """The record's own date: an explicit frontmatter date, else the newest date written
    in the body (not in the future), else the file mtime. Returns (iso_date, source)."""
    md = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    for where, d in (("metadata.modified", md.get("modified")), ("modified", fm.get("modified")),
                     ("last_modified", fm.get("last_modified")), ("updated", fm.get("updated")),
                     ("metadata.verified", md.get("verified")), ("verified", fm.get("verified")),
                     ("date", fm.get("date")), ("created", fm.get("created"))):
        if d:
            m = _ISO.search(str(d))
            if m:
                return m.group(0), "frontmatter:" + where
    today = datetime.now().strftime("%Y-%m-%d")
    dates = sorted({m.group(0) for m in _ISO.finditer(body) if m.group(0) <= today})
    if dates:
        return dates[-1], "body:newest-date"
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d"), "file:mtime"


def _state_lines(body: str) -> list[tuple[int, str]]:
    out = []
    for i, line in enumerate(body.splitlines(), 1):
        s = line.strip()
        if len(s) < 12 or s.startswith("```"):
            continue
        if _STATE_RE.search(s):
            out.append((i, s[:320]))
            if len(out) >= MAX_STATE_LINES:
                break
    return out


def _yaml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def journal_id(record_type: str, source_id: str) -> str:
    return f"journal:{record_type}:{source_id}"


def sources(conn) -> list[dict]:
    """Admitted source nodes for each record type, live paths only, file present on disk."""
    from .rank_signals import is_historical_copy
    from .util import should_skip_path
    seen, out = set(), []
    for rtype, like in RECORD_TYPES:
        rows = conn.execute(
            "SELECT id, path, title, content_hash, last_modified FROM nodes "
            "WHERE path LIKE ? AND path NOT LIKE 'ARCTURION_Brain/journals/%'", (like,)).fetchall()
        for r in rows:
            r = dict(r)
            p = r["path"]
            if not p or p in seen or is_historical_copy(p) or should_skip_path(Path(p)):
                continue
            if rtype == "handoff" and not Path(p).name.upper().startswith("HANDOFF"):
                continue
            f = C.DESKTOP_ROOT / p
            if not f.is_file():
                continue
            seen.add(p)
            out.append({**r, "record_type": rtype, "file": f})
    return out


def render(src: dict, verdicts: dict | None = None) -> tuple[str, dict]:
    """Journal Markdown for one source. Every statement is a quote or a measured value."""
    from .util import parse_frontmatter
    raw = src["file"].read_bytes()
    text = raw.decode("utf-8", errors="replace")
    fm, body = parse_frontmatter(text)
    fm = fm if isinstance(fm, dict) else {}
    as_of, as_of_src = _as_of(fm, body, src["file"].stat().st_mtime)
    desc = fm.get("description") or fm.get("summary") or ""
    desc = desc if isinstance(desc, str) else str(desc)
    title = src.get("title") or Path(src["path"]).stem
    jid = journal_id(src["record_type"], src["id"])
    # A verdict holds only for the exact source bytes it judged, and only while the superseded
    # candidate is still the bytes the judge read (checked against the index by the caller).
    slot = (verdicts or {}).get(_sha(raw)) or {}
    live_hash = (verdicts or {}).get("_candidate_hashes", {})
    sup = sorted((v for v in slot.get("supersedes", []) if live_hash.get(v["id"]) == v.get("content_hash")),
                 key=lambda x: x["id"])
    verdicts = {"attestation": slot.get("attestation")} if sup else {}
    j = {"schema": SCHEMA, "record_type": src["record_type"], "source_path": src["path"],
         "source_node": src["id"], "source_sha256": _sha(raw), "source_bytes": len(raw),
         "as_of": as_of, "as_of_from": as_of_src,
         "supersedes": [s["id"] for s in sup],
         "judge": (verdicts or {}).get("attestation")}
    label = {"decision": "Decision record", "project_status": "Project status record",
             "handoff": "Handoff record"}[src["record_type"]]
    lines = ["---", f"id: {jid}", "type: Journal", f"title: {_yaml_str(label + ' — ' + title)}",
             f"description: {_yaml_str((desc or title)[:600])}", "status: active",
             f"last_modified: {as_of}", f"evidenced_by: {_yaml_str(src['id'])}"]
    if j["supersedes"]:
        lines.append("supersedes:")
        lines += [f"  - {_yaml_str(i)}" for i in j["supersedes"]]
    lines.append("journal: " + json.dumps(j, ensure_ascii=False, sort_keys=True))
    lines += ["---", "", f"# {label} — {title}", "",
              f"**Record type:** {src['record_type']} · **As of:** {as_of} ({as_of_src}) · "
              f"**Source:** `{src['path']}`", ""]
    if desc:
        lines += [f"**Source description (verbatim):** {desc}", ""]
    state = _state_lines(body)
    if state:
        lines.append("**State lines (verbatim from source):**")
        lines += [f"- L{n}: {s}" for n, s in state]
        lines.append("")
    if sup:
        lines.append("**Supersedes (judge-verified):**")
        lines += [f"- `{s['path']}` — {s.get('reason', '')}" for s in sup]
        lines.append("")
    lines.append(f"**Source receipt:** sha256 `{j['source_sha256']}` · {j['source_bytes']} bytes.")
    return "\n".join(lines) + "\n", j


def _out_path(root: Path, src: dict) -> Path:
    slug = re.sub(r"[^a-z0-9_\-]+", "-", src["id"].lower())[:150]
    return root / src["record_type"] / f"{slug}.md"


def load_verdicts() -> dict:
    """{journal_id: {source_sha256: {"supersedes": [YES verdicts], "attestation": {...}}}}."""
    from . import judge
    if not judge.cache_path().exists():
        return {}
    out: dict = {}
    for rec in judge.load_cache()["batches"].values():
        slot = out.setdefault(rec["journal"], {}).setdefault(
            rec["source_sha256"], {"supersedes": [], "attestation": rec["attestation"]})
        slot["supersedes"].extend(v for v in rec["verdicts"] if v["answer"] == "YES")
    return out


def build(conn, root: Path = JOURNAL_ROOT, dry_run: bool = False) -> dict:
    verdicts = load_verdicts()
    want = sorted({v["id"] for per in verdicts.values() for slot in per.values() for v in slot["supersedes"]})
    hashes = {}
    for i in range(0, len(want), 500):
        chunk = want[i:i + 500]
        hashes.update({r["id"]: r["content_hash"] for r in conn.execute(
            f"SELECT id, content_hash FROM nodes WHERE id IN ({','.join('?' * len(chunk))})", chunk)})
    srcs = sources(conn)
    wanted, written, unchanged = set(), [], 0
    manifest = []
    for src in srcs:
        jid = journal_id(src["record_type"], src["id"])
        text, j = render(src, {**verdicts.get(jid, {}), "_candidate_hashes": hashes})
        out = _out_path(root, src)
        wanted.add(out)
        manifest.append({"id": jid, "path": str(out.relative_to(root)), **{k: j[k] for k in
                         ("record_type", "source_path", "source_node", "source_sha256", "as_of", "supersedes")}})
        if out.exists() and out.read_text() == text:
            unchanged += 1
            continue
        written.append(str(out))
        if not dry_run:
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".tmp")
            tmp.write_text(text)
            tmp.replace(out)
    removed = 0
    if root.exists():
        for f in root.rglob("*.md"):
            if f not in wanted:
                removed += 1
                if not dry_run:
                    f.unlink()
    if not dry_run:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "manifest.json").write_text(json.dumps(
            {"schema": SCHEMA, "built_at": _now(), "journals": manifest}, ensure_ascii=False, indent=0))
    return {"sources": len(srcs), "written": len(written), "unchanged": unchanged, "removed": removed,
            "dry_run": dry_run, "written_paths": written}


_SOURCE_MARKERS = ("/Memory/Decisions/", "/Memory/Projects/")


def touches_sources(paths) -> bool:
    return any(m in str(p) for p in paths for m in _SOURCE_MARKERS) or any(
        Path(str(p)).name.upper().startswith("HANDOFF") for p in paths)


def refresh(embed: bool = True) -> dict:
    """Rebuild changed journals, ingest them, and link supports edges. Used by engine.watch after
    a source note changes, and by deployment. Rewrites only journals whose text changed."""
    from . import db as DB
    from .capacity_admission import scoped_connection
    from .ingest import ingest_paths
    conn = scoped_connection("business", None, domain_neutral=True)
    try:
        res = build(conn)
    finally:
        conn.close()
    res.pop("written_paths")
    # Hand every journal to ingest: unchanged files are skipped by content hash, and a DB that
    # has never seen them (first deploy, restored backup) gets all of them.
    every = sorted(str(f) for f in JOURNAL_ROOT.rglob("*.md")) if JOURNAL_ROOT.exists() else []
    import sqlite3
    import time as _t

    def _retry(fn, tries=12):
        # The live DB has many concurrent writers; a deferred transaction that must upgrade
        # after another commit gets SQLITE_BUSY immediately (busy_timeout cannot help). Every
        # step here is idempotent, so retrying is safe.
        for i in range(tries):
            try:
                return fn()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or i == tries - 1:
                    raise
                _t.sleep(min(2 + 2 * i, 15))
    if every:
        res["ingest"] = _retry(lambda: ingest_paths(every, embed=embed, verbose=False))

    def _link():
        conn = DB.connect()
        try:
            return link_edges(conn)
        finally:
            conn.close()
    res["link"] = _retry(_link)
    return res


def link_edges(conn) -> dict:
    """source --supports--> journal for every indexed journal whose source hash still matches."""
    from . import db as DB
    rows = conn.execute("SELECT id, metadata FROM nodes WHERE id LIKE 'journal:%'").fetchall()
    added = stale = 0
    for r in rows:
        meta = json.loads(r["metadata"] or "{}")
        j = meta.get("journal") or {}
        src = conn.execute("SELECT id, content_hash FROM nodes WHERE id=?", (j.get("source_node"),)).fetchone()
        if not src or src["content_hash"] != j.get("source_sha256"):
            stale += 1
            continue
        if DB.insert_edge(conn, {"from": src["id"], "to": r["id"], "predicate": "supports",
                                 "family": "evidence", "weight": 1.0, "confidence": 1.0,
                                 "asserted_by": "journal-projection", "asserted_at": _now(),
                                 "evidence_span": "sha256:" + j["source_sha256"], "metadata": {}}):
            added += 1
    conn.commit()
    return {"journals_indexed": len(rows), "supports_added": added, "source_hash_mismatch": stale}


def precompute(workers: int = 3, max_calls: int | None = None, deadline_s: float | None = None,
               limit: int | None = None) -> dict:
    """Offline: find older look-alikes for each journal with the Brain's own search, then ask
    the judge (cached; only new or changed batches cost a call)."""
    from . import brain as B, judge
    from .capacity_admission import scoped_connection
    from .rank_signals import is_historical_copy
    from .util import parse_frontmatter
    conn = scoped_connection("business", None, domain_neutral=True)
    try:
        srcs = sources(conn)
    finally:
        conn.close()
    cand_cache_p = STATE_DIR / "candidates.json"
    cand_cache = json.loads(cand_cache_p.read_text()) if cand_cache_p.exists() else {}
    batches = []
    for n, src in enumerate(srcs[:limit] if limit else srcs):
        raw = src["file"].read_bytes()
        sha = _sha(raw)
        text = raw.decode("utf-8", errors="replace")
        fm, body = parse_frontmatter(text)
        fm = fm if isinstance(fm, dict) else {}
        as_of, _ = _as_of(fm, body, src["file"].stat().st_mtime)
        desc = fm.get("description") or ""
        jid = journal_id(src["record_type"], src["id"])
        ck = jid + "@" + sha
        if ck not in cand_cache:
            topic = f"{src.get('title') or Path(src['path']).stem}. {desc if isinstance(desc, str) else ''}"[:300]
            pack = B.query(topic=topic, token_budget=2500, source="journal-candidates")
            picked = []
            for node in pack.nodes:
                p = node.get("path") or ""
                if (node["id"] == src["id"] or node["id"].startswith("journal:") or not p
                        or node.get("family") != "document" or p == src["path"]):
                    continue
                newer = str(node.get("last_modified") or "")[:10] > as_of
                if newer and not is_historical_copy(p):
                    continue
                picked.append({k: node.get(k) for k in ("id", "path", "title", "summary", "body_excerpt",
                                                         "last_modified", "content_hash")})
                if len(picked) >= 6:
                    break
            cand_cache[ck] = picked
            if n % 25 == 0:
                STATE_DIR.mkdir(parents=True, exist_ok=True)
                cand_cache_p.write_text(json.dumps(cand_cache, ensure_ascii=False))
        if cand_cache[ck]:
            batches.append({"journal": {"id": jid, "record_type": src["record_type"], "as_of": as_of,
                                        "source_sha256": sha},
                            "current_text": f"path: {src['path']}\n{text}", "candidates": cand_cache[ck]})
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cand_cache_p.write_text(json.dumps(cand_cache, ensure_ascii=False))
    res = judge.judge_batches(batches, workers=workers, max_calls=max_calls, deadline_s=deadline_s)
    return {"journals": len(srcs), "batches": len(batches), **res}


def _cli(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "link", "precompute", "refresh"])
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--max-calls", type=int)
    ap.add_argument("--deadline-s", type=float)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--root", default=str(JOURNAL_ROOT))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    from . import db as DB
    if a.cmd == "build":
        from .capacity_admission import scoped_connection
        conn = scoped_connection("business", None, domain_neutral=True)
        try:
            res = build(conn, Path(a.root), dry_run=a.dry_run)
            res.pop("written_paths", None)
            print(json.dumps(res))
        finally:
            conn.close()
    elif a.cmd == "refresh":
        print(json.dumps(refresh(), default=str))
    elif a.cmd == "precompute":
        print(json.dumps(precompute(a.workers, a.max_calls, a.deadline_s, a.limit)))
    else:
        conn = DB.connect()
        try:
            print(json.dumps(link_edges(conn)))
        finally:
            conn.close()


if __name__ == "__main__":
    sys.exit(_cli())
