"""Version-bound retrieval quarantine. Originals remain local; never log matches."""
from __future__ import annotations
import re
import json
import os
import sqlite3
from pathlib import Path
from . import config as C

SCHEMA = """
CREATE TABLE IF NOT EXISTS node_retrieval_quarantine (
 node_id TEXT NOT NULL, content_hash TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL,
 scope TEXT NOT NULL, recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(node_id, content_hash, reason)
);
CREATE INDEX IF NOT EXISTS idx_retrieval_quarantine_node ON node_retrieval_quarantine(node_id);
"""
_topic_policy = C.SENSITIVE_POLICY["privacy_topic_pattern"]
_person_policy = C.SENSITIVE_POLICY["privacy_person_pattern"]
_path_marker = C.SENSITIVE_POLICY["privacy_path_marker"]
_TOPIC = re.compile(_topic_policy, re.I)
_PERSON = re.compile(_person_policy, re.I)
_FIRST_PERSON = re.compile(r"\b(?:i|my)\b", re.I)

def _restricted_aliases():
    """Absolute and canonical stored-path spellings for configured roots."""
    aliases = set()
    for root in (RESTRICTED_RECOVERY_ROOT, _LEGACY_PROTECTED_ROOT):
        aliases.add(str(root))
        for base in (C.UNIVERSE_ROOT, C.DESKTOP_ROOT, C.WORK_ROOT):
            if root.is_relative_to(base):
                aliases.add(str(root.relative_to(base)))
    return aliases


def install_privacy_guards(conn):
    """Persist the guard in SQLite so already-running clients see redacted rows."""
    have_fts = conn.execute("SELECT 1 FROM sqlite_master WHERE name='fts_nodes'").fetchone()
    fts_delete = 'DELETE FROM fts_nodes WHERE node_id=NEW.id;' if have_fts else ''
    clauses = []
    for alias in sorted(_restricted_aliases()):
        if alias == '.':
            clauses.append("(COALESCE(NEW.path,'') != '' AND substr(NEW.path,1,1) != '/')")
            continue
        literal = alias.replace("'", "''")
        prefix = (alias + '/').replace("'", "''")
        clauses.append(f"(NEW.path = '{literal}' OR substr(NEW.path,1,{len(alias)+1}) = '{prefix}')")
    private_path = ' OR '.join(clauses)
    for event in ('INSERT','UPDATE'):
        conn.execute(f'DROP TRIGGER IF EXISTS privacy_node_after_{event.lower()}')
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS privacy_node_after_{event.lower()}
        AFTER {event} ON nodes
        WHEN (EXISTS(SELECT 1 FROM node_retrieval_quarantine q WHERE
          (q.node_id=NEW.id OR (q.content_hash!='' AND q.reason!='restricted-rollback-source-2026-10-02'))
          AND q.content_hash=COALESCE(NEW.content_hash,'') AND q.scope IN ('all','restricted-source')) OR ({private_path}))
        AND (COALESCE(NEW.title,'')!='[Privacy-excluded source]' OR NEW.summary IS NOT NULL
          OR NEW.body_excerpt IS NOT NULL OR NEW.path IS NOT NULL OR NEW.owner IS NOT NULL)
        BEGIN
          INSERT OR IGNORE INTO node_retrieval_quarantine(node_id,content_hash,reason,scope)
            SELECT NEW.id,COALESCE(NEW.content_hash,''),'restricted-rollback-source-2026-10-02','restricted-source'
            WHERE {private_path};
          UPDATE nodes SET title='[Privacy-excluded source]', summary=NULL, body_excerpt=NULL,
            path=NULL, owner=NULL, status='privacy_excluded', metadata='{{"privacy_excluded":true}}'
            WHERE id=NEW.id;
          {fts_delete}
          DELETE FROM embeddings WHERE node_id=NEW.id;
        END""")
        conn.execute(f'DROP TRIGGER IF EXISTS privacy_embedding_after_{event.lower()}')
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS privacy_embedding_after_{event.lower()}
        AFTER {event} ON embeddings
        WHEN EXISTS(SELECT 1 FROM nodes n JOIN node_retrieval_quarantine q
          ON q.content_hash=COALESCE(n.content_hash,'') AND
            (q.node_id=n.id OR (q.content_hash!='' AND q.reason!='restricted-rollback-source-2026-10-02'))
          WHERE n.id=NEW.node_id AND q.scope IN ('all','restricted-source'))
        BEGIN DELETE FROM embeddings WHERE node_id=NEW.node_id; END""")

def redact_private_versions(conn):
    """Remove only denied derived versions from the live cache, never originals."""
    rows = conn.execute("SELECT n.id FROM nodes n JOIN node_retrieval_quarantine q ON q.node_id=n.id AND q.content_hash=COALESCE(n.content_hash,'') WHERE q.scope='all'").fetchall()
    ids = sorted({r[0] for r in rows})
    for nid in ids:
        conn.execute("UPDATE nodes SET title='[Privacy-excluded source]',summary=NULL,body_excerpt=NULL,path=NULL,owner=NULL,status='privacy_excluded',metadata='{\"privacy_excluded\":true}' WHERE id=?",(nid,))
        conn.execute('DELETE FROM embeddings WHERE node_id=?',(nid,))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='fts_nodes'").fetchone():
            conn.execute('DELETE FROM fts_nodes WHERE node_id=?',(nid,))
    conn.commit()
    return len(ids)

def privacy_reason(node):
    text = ' '.join(str(node.get(k) or '') for k in ('title', 'summary', 'body_excerpt'))
    # Most nodes contain no excluded topic. Avoid a second scan and path work
    # there; when present, retain attribution across EVERY topic occurrence.
    if not _TOPIC.search(text):
        return None
    path = str(node.get('path') or '')
    lower_path = path.lower()
    personal_path = any(x in lower_path for x in ('founder', '/preferences/', '/personal/', '/health/'))
    attributed = any(_PERSON.search(text[max(0, m.start()-160):m.end()+160]) for m in _TOPIC.finditer(text))
    if attributed or (personal_path and _FIRST_PERSON.search(text)) or _path_marker in path:
        return 'founder-personal-exclusion-2026-10-02'
    return None

def record_privacy(conn, node):
    reason = privacy_reason(node)
    md = node.get('metadata') or {}
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except ValueError:
            md = {}
    if isinstance(md,dict) and md.get('restricted_artifact_reason'):
        reason = md['restricted_artifact_reason']
    if restricted_source_path(node.get('path')):
        reason = 'restricted-rollback-source-2026-10-02'
    source_id = md.get('first_seen_in') if isinstance(md, dict) else None
    if not reason and source_id:
        reason = 'derived-excluded-personal-source-2026-10-02' if conn.execute(
            "SELECT 1 FROM node_retrieval_quarantine q JOIN nodes n ON n.id=q.node_id AND COALESCE(n.content_hash,'')=q.content_hash WHERE q.node_id=? AND q.scope='all'",
            (source_id,)).fetchone() else None
    if reason:
        conn.execute(SCHEMA.split(';')[0])
        scope='restricted-source' if reason=='restricted-rollback-source-2026-10-02' else 'all'
        conn.execute('INSERT OR IGNORE INTO node_retrieval_quarantine(node_id,content_hash,reason,scope) VALUES (?,?,?,?)',
                     (node['id'], node.get('content_hash') or '', reason, scope))
    return reason

def filter_retrieval(conn, nodes, historical=False):
    from .memory_policy import load_policy
    policy = load_policy()
    try:
        denied = {}
        denied_hashes = set()
        for r in conn.execute('SELECT node_id,content_hash,scope,reason FROM node_retrieval_quarantine'):
            key = (r[0], r[1])
            if denied.get(key) != 'all':
                denied[key] = r[2]
            if r[2] == 'all' and r[1] and r[3]!='restricted-rollback-source-2026-10-02':
                denied_hashes.add(r[1])
    except sqlite3.Error as exc:
        raise RuntimeError('Retrieval quarantine unavailable; refusing context output') from exc
    def privacy_allowed(node):
        if privacy_reason(node):
            return False
        scope = denied.get((node['id'], node.get('content_hash') or ''))
        if scope in ('all','restricted-source') or node.get('content_hash') in denied_hashes or (scope == 'current' and not historical):
            return False
        return True
    indexed = {}
    def resolve(node_id):
        if node_id not in indexed:
            row = conn.execute('SELECT * FROM nodes WHERE id=?', (node_id,)).fetchone()
            indexed[node_id] = dict(row) if row else None
        node = indexed[node_id]
        return node if node is not None and privacy_allowed(node) else None
    out = []
    for node in nodes:
        if not privacy_allowed(node):
            continue
        # Full indexed rows prevent projection-only routes from omitting restrictions.
        full = resolve(node['id'])
        if full is not None and node.get('content_hash') != full.get('content_hash'):
            continue
        # Synthetic caller rows remain supported only without configured policy.
        if full is None and not policy.data and indexed.get(node['id']) is None:
            full = node if privacy_allowed(node) else None
        if full is not None and policy.allows(full, resolve):
            out.append(node)
    return out

def source_path(stored):
    if not stored or str(stored).startswith(('http:', 'https:', 'session:')):
        return None
    path = Path(stored)
    if path.is_absolute():
        return path
    if path.parts and (path.parts[0] in C.CONSTELLATION_AGENTS or path.parts[0] in
                       ('ATLAS','ARCTURION_Brain','ARCTURION_EXTERNAL_RUNTIME','ARCTURION_HQ')):
        return C.DESKTOP_ROOT / path
    if str(path).startswith('.claude/'):
        return C.WORK_ROOT / path
    return C.UNIVERSE_ROOT / path

def health(conn):
    """Type-specific completeness. Missing dates never imply freshness."""
    types = {}
    for r in conn.execute("SELECT type,COUNT(*) n,SUM(CASE WHEN owner IS NULL OR owner='' THEN 1 ELSE 0 END) missing_owner,SUM(CASE WHEN last_reviewed IS NULL OR last_reviewed='' THEN 1 ELSE 0 END) unknown_review,SUM(CASE WHEN created IS NULL OR created='' THEN 1 ELSE 0 END) unknown_created,SUM(CASE WHEN last_modified IS NULL OR last_modified='' THEN 1 ELSE 0 END) unknown_modified,SUM(CASE WHEN content_hash IS NULL OR content_hash='' THEN 1 ELSE 0 END) missing_hash FROM nodes GROUP BY type"):
        types[r['type']] = dict(r)
        authored = r['type'] in {'Source','Document','Decision','Preference','Policy','Reflection','Lesson','Rule','Commandment','Law'}
        types[r['type']]['review_required'] = authored
        types[r['type']]['required_fields'] = ['owner','last_modified','content_hash'] + (['last_reviewed'] if authored else [])
        types[r['type']]['applicability'] = 'Maintained authored document review' if authored else 'Type-specific source/event evidence; human review date not assumed applicable'
    watermarks = {}
    quarantine = {}
    try:
        watermarks = {r[0]: r[1] for r in conn.execute("SELECT CASE WHEN last_error LIKE 'admission-refused:%' THEN 'refused' ELSE status END,COUNT(*) FROM source_ingest_watermarks GROUP BY 1")}
        quarantine = {r[0]: r[1] for r in conn.execute('SELECT reason,COUNT(*) FROM node_retrieval_quarantine GROUP BY reason')}
    except Exception:
        pass
    return {'schema':'brain.source-integrity.v1', 'metadata_by_type':types,
            'source_watermark_status':watermarks, 'quarantined_versions_by_reason':quarantine,
            'unknown_review_total':sum(x['unknown_review'] or 0 for x in types.values()),
            'authored_unknown_review':sum(x['unknown_review'] or 0 for x in types.values() if x['review_required']),
            'superseded_without_valid_until':conn.execute("SELECT count(*) FROM nodes WHERE status='superseded' AND valid_until IS NULL").fetchone()[0],
            'unknown_is_fresh':False}

# Restricted rollback artifacts are evidence, never a new retrieval source.
RESTRICTED_RECOVERY_ROOT = Path(C.SENSITIVE_POLICY["restricted_recovery_root"])
RESTRICTED_ARTIFACT_REGISTRY = RESTRICTED_RECOVERY_ROOT / "restricted-artifacts.json"
_LEGACY_PROTECTED_ROOT = Path(C.SENSITIVE_POLICY["legacy_protected_root"])

def restricted_source_path(path):
    if not path:
        return False
    raw = str(path)
    p = Path(raw)
    for alias in _restricted_aliases():
        if alias == '.':
            if not p.is_absolute():
                return True
        elif raw == alias or raw.startswith(alias + '/'):
            return True
    if not p.is_absolute():
        p = source_path(p)
    return bool(p and any(p.absolute().is_relative_to(root) or p.resolve().is_relative_to(root)
                          for root in (RESTRICTED_RECOVERY_ROOT, _LEGACY_PROTECTED_ROOT)))

def canonical_json_identity(value):
    import hashlib
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,
                                     separators=(',',':')).encode()).hexdigest()

def artifact_privacy_reason(parsed,raw_hash):
    """Recognize exact private bytes and normalized JSON nested in a new wrapper."""
    import hashlib
    try:
        registry=json.loads(RESTRICTED_ARTIFACT_REGISTRY.read_text())
    except FileNotFoundError:
        registry={}
    except (OSError,ValueError):
        raise RuntimeError('Restricted artifact registry unavailable; refusing extraction')
    denied_raw=set(registry.get('content_hashes',[]))
    denied_json=set(registry.get('canonical_json_identities',[]))
    if raw_hash in denied_raw:
        return 'restricted-audit-artifact-2026-10-02'
    pending=[parsed]
    while pending:
        value=pending.pop()
        if isinstance(value,(dict,list)):
            if denied_json and canonical_json_identity(value) in denied_json:
                return 'wrapped-restricted-audit-artifact-2026-10-02'
            if isinstance(value,dict):
                if privacy_reason(value):
                    return 'wrapped-excluded-personal-source-2026-10-02'
                pending.extend(value.values())
            else:
                pending.extend(value)
        elif isinstance(value,str):
            if hashlib.sha256(value.encode()).hexdigest() in denied_raw:
                return 'wrapped-restricted-audit-artifact-2026-10-02'
            if privacy_reason({'body_excerpt':value}):
                return 'wrapped-excluded-personal-source-2026-10-02'
            if denied_json and value.lstrip().startswith(('[','{')):
                try:
                    decoded=json.loads(value)
                except ValueError:
                    continue
                if isinstance(decoded,(dict,list)):
                    pending.append(decoded)
    return None
