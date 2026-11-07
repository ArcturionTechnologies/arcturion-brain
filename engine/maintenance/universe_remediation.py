"""Reversible October 2 source integrity mitigation. No source originals are edited."""
from __future__ import annotations
import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from .. import db as DB
from .. import config as C
from ..source_integrity import source_path, record_privacy, health
from ..resolver import is_archive_path
from . import validity_pass, edge_endpoint_resolve

def run(output: Path, apply=False):
    conn = DB.connect()
    if apply:
        DB.init_db(conn)
    before_health = DB.edge_health(conn)
    counts = Counter()
    before_images = []
    changed_paths = []
    paths_seen = set()
    absent_paths = set()
    for r in conn.execute('SELECT * FROM nodes').fetchall():
        n = dict(r)
        path = n.get('path') or ''
        original = {k:n.get(k) for k in ('id','status','owner','last_modified','valid_until')}
        updates = {}
        if apply and record_privacy(conn, n):
            counts['privacy_versions_tombstoned'] += 1
        elif not apply:
            from ..source_integrity import privacy_reason
            counts['privacy_versions_candidates'] += bool(privacy_reason(n))
        if is_archive_path(path) and n.get('status') not in ('archived','superseded','retired'):
            updates['status'] = 'archived'
            counts['history_nodes_archived'] += 1
        p = source_path(path)
        # Only file-backed indexed records: sessions and virtual entity paths are not files.
        file_backed = p and p.suffix.lower() in {'.md','.py','.json','.jsonl','.yaml','.yml','.toml','.plist','.sh','.txt','.js','.ts','.html','.css'}
        if file_backed:
            paths_seen.add(path)
            if not p.exists():
                absent_paths.add(path)
                if n.get('status') in ('active','canonical','verified','open') and not is_archive_path(path):
                    counts['absent_current_nodes_quarantined'] += 1
                    if apply:
                        conn.execute('INSERT OR IGNORE INTO node_retrieval_quarantine(node_id,content_hash,reason,scope) VALUES (?,?,?,?)',
                                     (n['id'],n.get('content_hash') or '', 'source-absent-2026-10-02', 'current'))
            else:
                if not n.get('owner') and path.split('/')[0] in C.CONSTELLATION_AGENTS:
                    updates['owner'] = path.split('/')[0]
                    counts['owners_inferred_from_agent_source_path'] += 1
                if not n.get('last_modified'):
                    updates['last_modified'] = datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec='seconds')
                    counts['modified_dates_from_real_file_mtime'] += 1
        if updates:
            before_images.append(original)
            if apply:
                conn.execute('UPDATE nodes SET '+','.join(k+'=?' for k in updates)+' WHERE id=?', [*updates.values(),n['id']])
    conn.commit()
    if apply:
        for r in conn.execute("SELECT * FROM nodes WHERE type='Entity'").fetchall():
            if record_privacy(conn, dict(r)):
                counts['derived_private_versions_tombstoned'] += 1
        conn.commit()
    validity = validity_pass.run(conn,apply=apply,propose=False,verbose=False)
    edge_result = edge_endpoint_resolve.run(conn,['depends_on','depended_on_by','tested_by','configured_by','hosts','runs_on','installs','installed_by'],dry_run=not apply)
    quarantined_edges = 0
    for r in conn.execute("SELECT e.* FROM edges e WHERE predicate IN ('depends_on','depended_on_by') AND (NOT EXISTS(SELECT 1 FROM nodes n WHERE n.id=e.from_id) OR NOT EXISTS(SELECT 1 FROM nodes n WHERE n.id=e.to_id))"):
        if apply:
            edge = dict(r)
            edge['from'],edge['to'] = edge['from_id'],edge['to_id']
            DB.quarantine_edge(conn,edge,'unresolved-dependency-endpoint-2026-10-02')
        quarantined_edges += 1
    conn.commit()
    result = {'applied':apply,'counts':dict(counts),'file_backed_source_paths_checked':len(paths_seen),
              'absent_file_backed_paths':len(absent_paths),'edge_health_before':before_health,
              'edge_health_after':DB.edge_health(conn),'supersession_validity':validity,
              'endpoint_resolution':edge_result,'unresolved_dependency_edges_retained_and_quarantined':quarantined_edges,
              'source_integrity':health(conn),'dedup_merges':0,
              'rollback':'Use restricted online backup for offline full recovery; restore field before-images for scoped node reversal; remove only this dated quarantine reason and newly recorded endpoint edge IDs after reviewing concurrent writes.'}
    if apply:
        output.mkdir(parents=True,exist_ok=True)
        (output/'brain-node-fields-before.json').write_text(json.dumps(before_images,indent=2)+'\n')
        (output/'brain-migration.json').write_text(json.dumps(result,indent=2)+'\n')
    conn.close()
    return result

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--apply',action='store_true')
    a=p.parse_args()
    result=run(a.output,apply=a.apply)
    print(json.dumps({k:v for k,v in result.items() if k in ('applied','counts','file_backed_source_paths_checked','absent_file_backed_paths','unresolved_dependency_edges_retained_and_quarantined')},indent=2))

if __name__=='__main__': main()
