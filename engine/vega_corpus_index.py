"""Transactional, isolated inventory and evidence index for the VEGA corpus."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path, PurePosixPath

from . import config as C
from . import vega_corpus as corpus
from . import vega_index as desk
from .validator import validate_desk_pack

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS vega_desk_inventory (path_hash TEXT PRIMARY KEY, area TEXT NOT NULL, disposition TEXT NOT NULL, reason TEXT NOT NULL, source_hash TEXT, canonical_bound INTEGER NOT NULL, accepted INTEGER NOT NULL, queued INTEGER NOT NULL, topic_edges INTEGER NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_vega_inventory_area ON vega_desk_inventory(area,disposition)",
)
DISPOSITIONS = frozenset({"eligible", "excluded", "unsupported", "removed", "read_error"})
AREAS = frozenset({"workspace", "other", "knowledge", "overview", "strategies", "execution", "macro", "crypto", "intelligence", "data_sources", "risk", "charts", "back_office", "deliverables"})


def _path(value):
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        raise ValueError("invalid corpus source")
    p = PurePosixPath(value)
    if p.is_absolute() or not p.parts or p.parts[0] != "VEGA" or any(x in {".", ".."} for x in value.split("/")) or p.as_posix() != value:
        raise ValueError("corpus source must be a normalized VEGA path")
    return value


def _bounded(value, default="other"):
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value) else default


def _selected(paths, root):
    if paths is None:
        return None
    base = Path(root).resolve()
    result = set()
    for value in paths:
        p = Path(value)
        if p.is_absolute():
            try:
                name = p.absolute().relative_to(Path(root).absolute()).as_posix()
            except ValueError:
                continue
        else:
            name = p.as_posix()
        if name == "VEGA" or name.startswith("VEGA/"):
            result.add(_path(name))
    return result


def _relevant(path, selected):
    return selected is None or any(path == p or path.startswith(p + "/") for p in selected)


def sync_corpus(conn, *, paths=None, root=None, pack_path=None, rules_path=None):
    """Replace only selected corpus evidence; full scans reconcile the inventory.

    No commit or implicit connection is opened. Excluded source names are held
    only while scanning; the durable inventory stores their path hashes. The
    workspace is scanned before the write reservation is taken.
    """
    options = {"paths": paths, "root": root, "pack_path": pack_path, "rules_path": rules_path}
    owned = not conn.in_transaction
    plan = prepare_corpus(conn, **options)
    if owned:
        conn.execute("BEGIN IMMEDIATE")
    try:
        result = apply_prepared_corpus(conn, plan, options)
        if owned and result.get("unchanged"):
            conn.rollback()
        return result
    except Exception:
        if owned:
            conn.rollback()
        raise


def _corpus_meta(conn):
    try:
        rows = conn.execute("SELECT key,value FROM vega_desk_meta WHERE key IN ('corpus_pack_sha256','corpus_version')").fetchall()
        return tuple(sorted((row[0], row[1]) for row in rows))  # plain tuples: sqlite3.Row is unorderable
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return ()


def _known(conn, source):
    try:
        return bool(conn.execute("SELECT 1 FROM vega_desk_inventory WHERE path_hash=?", (desk._hash(source),)).fetchone())
    except sqlite3.OperationalError:
        return False


def _corpus_db_inputs(conn, probes):
    """Every database read that shapes the scan plan, for re-checking under lock."""
    return (_corpus_meta(conn), tuple((source, _known(conn, source)) for source in probes))


def _sync_corpus_locked(conn, *, paths=None, root=None, pack_path=None, rules_path=None):
    return _apply_corpus(conn, prepare_corpus(conn, paths=paths, root=root, pack_path=pack_path, rules_path=rules_path))


def _rules_input(rules_path):
    """Resolve and fingerprint the effective caller-owned rules for a sync."""
    effective = rules_path if rules_path is not None else os.environ.get("ARCTURION_VEGA_RULES_PATH")
    if effective is not None and not Path(effective).is_absolute():
        raise ValueError("VEGA rules path must be absolute")
    data = corpus.load_rule_config(effective)
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    return data, (str(effective) if effective is not None else None, digest)


def corpus_version(rule_config):
    """Return the exact version bound to a validated effective rules configuration."""
    mode = ":configured:" if rule_config is not None else ":unavailable:"
    digest = hashlib.sha256(json.dumps(rule_config, sort_keys=True).encode()).hexdigest()
    return str(corpus.CORPUS_VERSION) + mode + digest


def apply_prepared_corpus(conn, plan, options):
    """Apply a prepared plan; rebuild it (under the caller's lock) if its inputs moved."""
    _, current_rules = _rules_input(options.get("rules_path"))
    if plan.get("rules_input") != current_rules:
        raise ValueError("VEGA rules changed after preparation; prepare the sync again")
    if "db_inputs" in plan and _corpus_db_inputs(conn, plan["probes"]) != plan["db_inputs"]:
        plan = prepare_corpus(conn, **options)
    result = _apply_corpus(conn, plan)
    result["optional_rules"] = "configured" if options.get("rules_path") is not None or os.environ.get("ARCTURION_VEGA_RULES_PATH") else "unavailable"
    return result


def prepare_corpus(conn, *, paths=None, root=None, pack_path=None, rules_path=None):
    """All workspace scanning and hashing for a corpus sync; performs no writes."""
    root = Path(root) if root is not None else C.DESKTOP_ROOT
    pack_path = Path(pack_path) if pack_path is not None else desk.PACK_PATH
    rule_config, rules_input = _rules_input(rules_path)
    raw_pack = pack_path.read_bytes()
    pack = json.loads(raw_pack.decode("utf-8"))
    validate_desk_pack("VEGA", pack)
    selected = _selected(paths, root)
    if selected == set():
        return {"rules_input": rules_input, "result": {"processed": 0, "unchanged": True, "optional_rules": "configured" if rule_config is not None else "unavailable"}}
    pack_hash = hashlib.sha256(raw_pack).hexdigest()
    version = corpus_version(rule_config)
    meta = _corpus_meta(conn)
    old = dict(meta)
    if rule_config is None and ":configured:" in old.get("corpus_version", ""):
        raise ValueError("VEGA rules were previously configured; supply the rules path before syncing")
    probes = []
    if old.get("corpus_pack_sha256") != pack_hash or old.get("corpus_version") != version:
        selected = None
    # A missing path not recorded as a file can be a removed directory. A full
    # snapshot is required to remove hashed descendant inventory safely.
    if selected is not None:
        for source in selected:
            target = root / source
            # Hashed inventory has no recoverable parent path; directory and
            # symlink transitions require a full snapshot to remove descendants.
            # Exception: an excluded directory (e.g. runtime `state/`) is a
            # single terminal record in a full scan, which never enumerates
            # under it, so selecting just that directory yields the same record.
            if target.is_symlink() or (target.is_dir() and not corpus.excluded_directory(source)):
                selected = None
                break
            if not target.exists():
                probes.append(source)
                if not _known(conn, source):
                    selected = None
                    break
    db_inputs = _corpus_db_inputs(conn, probes)
    records = list(corpus.scan(root, paths=None if selected is None else sorted(selected), pack=pack, rule_config=rule_config))
    concepts = {c["id"]: c for c in pack["concepts"]}
    predicates = {p["id"]: p for p in pack["predicates"]}
    for concept in corpus.corpus_concepts():
        if concept["id"] in concepts and concepts[concept["id"]] != concept:
            raise desk.PackError("corpus concept collides with seed vocabulary")
        concepts[concept["id"]] = concept
    for pred in corpus.corpus_predicates():
        if pred["id"] in predicates and predicates[pred["id"]] != pred:
            raise desk.PackError("corpus predicate collides with seed vocabulary")
        predicates[pred["id"]] = pred
    pin_fields = ("source", "quote", "from", "to", "predicate", "kind", "context", "source_hash")
    pins = {desk._json([r.get(k, {} if k == "context" else None) for k in pin_fields]) for r in corpus.verified_pinned_rules(rule_config)}
    rules, seen, source_records = [], set(), {}
    for rec in records:
        source = _path(rec.get("source"))
        if source in source_records:
            raise desk.PackError("duplicate source in corpus inventory")
        source_records[source] = rec
        disposition = rec.get("disposition")
        if disposition not in DISPOSITIONS:
            raise desk.PackError("unknown corpus disposition")
        if disposition != "eligible":
            if rec.get("source_hash") is not None or rec.get("concepts") or rec.get("rules"):
                raise desk.PackError("excluded corpus record carried content")
            continue
        desk._source_path(source)
        digest = rec.get("source_hash")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise desk.PackError("eligible corpus source lacks SHA-256")
        for c in rec.get("concepts", []):
            if not isinstance(c, dict) or not str(c.get("id", "")).startswith(("doc_", "shelf_")):
                raise desk.PackError("corpus concept must use a reserved namespace")
            if c["id"] in concepts and concepts[c["id"]] != c:
                raise desk.PackError("conflicting corpus concept definition")
            concepts[c["id"]] = c
        for r in rec.get("rules", []):
            if not isinstance(r, dict) or r.get("source") != source or r.get("kind") not in {"catalog", "definition", "hypothesis"}:
                raise desk.PackError("invalid source-local corpus rule")
            for flag in ("inferred", "ambiguous", "conflicting"):
                if flag in r and type(r[flag]) is not bool:
                    raise desk.PackError("corpus uncertainty flags must be booleans")
            pin = {**r, "source_hash": digest}
            pin_key = desk._json([pin.get(k, {} if k == "context" else None) for k in pin_fields])
            if r.get("kind") != "catalog" and pin_key not in pins:
                r = {**r, "inferred": True}
            key = desk._rule_id(r)
            if key not in seen:
                seen.add(key)
                rules.append(r)
    combined = {**pack, "concepts": list(concepts.values()), "predicates": list(predicates.values()), "rules": rules}
    validate_desk_pack("VEGA", combined)
    # Re-read each evidence source now (not under the write reservation); the
    # digest is compared with the scan's to flag sources edited mid-scan.
    file_cache = {}
    for r in rules:
        source = r["source"]
        if source in file_cache:
            continue
        try:
            raw = desk._absolute_source(root, source).read_bytes()
            file_cache[source] = (raw.decode("utf-8"), hashlib.sha256(raw).hexdigest())
        except (OSError, UnicodeError, desk.PackError):
            file_cache[source] = (None, None)
    return {"rules_input": rules_input, "probes": probes, "db_inputs": db_inputs, "selected": selected, "pack_hash": pack_hash,
            "version": version, "concepts": concepts, "predicates": predicates, "rules": rules,
            "source_records": source_records, "file_cache": file_cache}


def _apply_corpus(conn, plan):
    """Database-only half of a corpus sync: no workspace file is opened here."""
    if "result" in plan:
        return dict(plan["result"])
    selected, pack_hash, version = plan["selected"], plan["pack_hash"], plan["version"]
    concepts, predicates, rules = plan["concepts"], plan["predicates"], plan["rules"]
    source_records, file_cache = plan["source_records"], plan["file_cache"]
    conn.execute("SAVEPOINT vega_corpus_sync")
    try:
        for ddl in (*desk.SCHEMA, *SCHEMA):
            conn.execute(ddl)
        for c in concepts.values():
            conn.execute("INSERT INTO vega_desk_concepts VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET label=excluded.label,kind=excluded.kind,area=excluded.area,definition=excluded.definition,aliases_json=excluded.aliases_json", (c["id"], c["label"], c["kind"], c["area"], c["definition"], desk._json(c["aliases"])))
        for p in predicates.values():
            conn.execute("INSERT INTO vega_desk_predicates VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET label=excluded.label,from_kinds_json=excluded.from_kinds_json,to_kinds_json=excluded.to_kinds_json", (p["id"], p["label"], desk._json(p["from_kinds"]), desk._json(p["to_kinds"])))
        # Seed and authored declarations take precedence over an identical
        # passage/relation; separate documents retain their own evidence.
        other = conn.execute("SELECT v.source_path,v.quote,e.from_id,e.predicate,e.to_id FROM vega_desk_edges e JOIN vega_desk_evidence v ON v.id=e.evidence_id LEFT JOIN vega_desk_claim_origin o ON o.edge_id=e.id WHERE COALESCE(o.origin,'seed')!='corpus'").fetchall()
        other_keys = {tuple(row) for row in other}
        active = set()
        counts = {s: {"accepted": 0, "queued": 0, "topics": 0} for s in source_records}
        cached_source, cached_text, cached_digest, cached_node = None, None, None, None
        for r in rules:
            source = r["source"]
            key = (source, r["quote"], r["from"], r["predicate"], r["to"])
            if key in other_keys:
                continue
            rid = desk._rule_id(r)
            eid = "vds_" + desk._hash(desk._json([source, r["quote"]]))
            active.add(rid)
            if cached_source != source:
                cached_source = source
                cached_text, cached_digest = file_cache[source]
                cached_node = desk._source_node(conn, source)
            digest, node = cached_digest, cached_node
            start = cached_text.find(r["quote"]) if cached_text is not None else -1
            end = start + len(r["quote"]) if start >= 0 else None
            if cached_text is None:
                problem = "source_unavailable"
            elif start < 0:
                problem = "quote_missing"
            elif cached_text.find(r["quote"], start + 1) >= 0:
                problem = "ambiguous_passage"
            else:
                problem = None
            if start < 0:
                start = None
            reasons = []
            if problem:
                reasons.append(problem)
            if digest != source_records[source]["source_hash"]:
                reasons.append("source_changed_during_scan")
            if not node:
                reasons.append("source_node_unresolved")
            if r.get("inferred") or r.get("ambiguous") or r.get("conflicting"):
                reasons.append("candidate_claim")
            fingerprint = desk._hash(desk._json([rid, digest, start, end, node, sorted(reasons)]))
            prior = conn.execute("SELECT resolution,reviewed_fingerprint,reason FROM vega_desk_review_queue WHERE edge_id=?", (rid,)).fetchone()
            old_edge = conn.execute("SELECT fingerprint FROM vega_desk_edges WHERE id=?", (rid,)).fetchone()
            resolution = prior[0] if prior and prior[1] == fingerprint else None
            if prior and ((old_edge and old_edge[0] != fingerprint) or (not resolution and "review_changed" in prior[2].split(","))):
                reasons.append("review_changed")
            elif prior and resolution and not reasons:
                reasons.append("reviewed")
            status = "rejected" if resolution == "rejected" else ("accepted" if resolution == "accepted" or not reasons else "queued")
            conn.execute("INSERT INTO vega_desk_evidence VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET source_path=excluded.source_path,source_node_id=excluded.source_node_id,quote=excluded.quote,start_offset=excluded.start_offset,end_offset=excluded.end_offset,source_hash=excluded.source_hash", (eid, source, node, r["quote"], start, end, digest))
            conn.execute("INSERT INTO vega_desk_edges VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET from_id=excluded.from_id,to_id=excluded.to_id,predicate=excluded.predicate,kind=excluded.kind,context_json=excluded.context_json,evidence_id=excluded.evidence_id,status=excluded.status,fingerprint=excluded.fingerprint", (rid, r["from"], r["to"], r["predicate"], r["kind"], desk._json(r.get("context", {})), eid, status, fingerprint))
            conn.execute("INSERT INTO vega_desk_claim_origin VALUES (?, 'corpus') ON CONFLICT(edge_id) DO UPDATE SET origin='corpus'", (rid,))
            if reasons:
                conn.execute("INSERT INTO vega_desk_review_queue(edge_id,reason,resolution,reviewed_fingerprint) VALUES (?,?,?,?) ON CONFLICT(edge_id) DO UPDATE SET reason=excluded.reason,resolution=excluded.resolution,reviewed_fingerprint=excluded.reviewed_fingerprint,note=CASE WHEN vega_desk_review_queue.reviewed_fingerprint=excluded.reviewed_fingerprint THEN vega_desk_review_queue.note ELSE NULL END", (rid, ",".join(sorted(reasons)), resolution, fingerprint if resolution else None))
            elif prior:
                conn.execute("DELETE FROM vega_desk_review_queue WHERE edge_id=?", (rid,))
            if status in ("accepted", "queued"):
                counts[source][status] += 1
            if r["predicate"] == "vega_corpus_mentions":
                counts[source]["topics"] += 1
        old_edges = conn.execute("SELECT e.id,v.source_path FROM vega_desk_edges e JOIN vega_desk_evidence v ON v.id=e.evidence_id JOIN vega_desk_claim_origin o ON o.edge_id=e.id WHERE o.origin='corpus'").fetchall()
        for edge_id, source in old_edges:
            if _relevant(source, selected) and edge_id not in active:
                conn.execute("DELETE FROM vega_desk_review_queue WHERE edge_id=?", (edge_id,))
                conn.execute("DELETE FROM vega_desk_claim_origin WHERE edge_id=?", (edge_id,))
                conn.execute("DELETE FROM vega_desk_edges WHERE id=?", (edge_id,))
        conn.execute("DELETE FROM vega_desk_evidence WHERE id NOT IN (SELECT evidence_id FROM vega_desk_edges)")
        conn.execute("DELETE FROM vega_desk_concepts WHERE (id LIKE 'doc\\_%' ESCAPE '\\' OR id LIKE 'shelf\\_%' ESCAPE '\\') AND id NOT IN (SELECT from_id FROM vega_desk_edges UNION SELECT to_id FROM vega_desk_edges)")
        if selected is None:
            conn.execute("DELETE FROM vega_desk_inventory")
        for source, rec in source_records.items():
            path_hash = desk._hash(source)
            if rec["disposition"] == "removed":
                conn.execute("DELETE FROM vega_desk_inventory WHERE path_hash=?", (path_hash,))
                continue
            area = rec.get("area") if rec.get("area") in AREAS else "other"
            digest = rec.get("source_hash") if rec["disposition"] == "eligible" else None
            bound = bool(desk._source_node(conn, source)) if rec["disposition"] == "eligible" else False
            c = counts[source]
            conn.execute("INSERT INTO vega_desk_inventory VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(path_hash) DO UPDATE SET area=excluded.area,disposition=excluded.disposition,reason=excluded.reason,source_hash=excluded.source_hash,canonical_bound=excluded.canonical_bound,accepted=excluded.accepted,queued=excluded.queued,topic_edges=excluded.topic_edges", (path_hash, area, rec["disposition"], _bounded(rec.get("reason")), digest, int(bound), c["accepted"], c["queued"], c["topics"]))
        if selected is None:
            conn.execute("INSERT INTO vega_desk_meta VALUES ('corpus_pack_sha256',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (pack_hash,))
            conn.execute("INSERT INTO vega_desk_meta VALUES ('corpus_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (version,))
        summary = coverage(conn)
        conn.execute("RELEASE vega_corpus_sync")
        return summary
    except Exception:
        conn.execute("ROLLBACK TO vega_corpus_sync")
        conn.execute("RELEASE vega_corpus_sync")
        raise


def coverage(conn):
    """Counts only; private paths never leave the hashed inventory."""
    rows = conn.execute("SELECT area,disposition,reason,COUNT(*),SUM(canonical_bound),SUM(CASE WHEN queued>0 THEN 1 ELSE 0 END),SUM(CASE WHEN disposition='eligible' AND topic_edges=0 THEN 1 ELSE 0 END),SUM(accepted),SUM(queued) FROM vega_desk_inventory GROUP BY area,disposition,reason ORDER BY area,disposition,reason").fetchall()
    keys = ("area", "disposition", "reason", "count", "canonical_attached", "documents_queued", "documents_without_topic", "accepted_edges", "queued_edges")
    duplicates = conn.execute("SELECT area,SUM(n-1) FROM (SELECT area,source_hash,COUNT(*) AS n FROM vega_desk_inventory WHERE source_hash IS NOT NULL GROUP BY area,source_hash HAVING COUNT(*)>1) GROUP BY area").fetchall()
    return {"inventory": [dict(zip(keys, row)) for row in rows], "duplicate_sources_by_area": dict(duplicates), "query_visible": None, "visibility_note": "Use scoped retrieval coverage to count query-visible evidence."}
