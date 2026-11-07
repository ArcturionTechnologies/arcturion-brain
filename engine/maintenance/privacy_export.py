"""Atomic streaming refresh of derived graph exports; no private content logged."""
import json
import os
import shutil
from pathlib import Path
from .. import db as DB
from .. import config as C

def run(recovery):
    recovery=Path(recovery); recovery.mkdir(parents=True,exist_ok=True)
    conn=DB.connect()
    conn.execute('BEGIN')
    denied={r[0] for r in conn.execute("SELECT n.id FROM nodes n JOIN node_retrieval_quarantine q ON q.node_id=n.id AND q.content_hash=COALESCE(n.content_hash,'') WHERE q.scope='all'")}
    outputs={}
    for name in ('node_index_brain_v2.jsonl','triples_brain_v2.jsonl','graph.json'):
        path=C.GRAPH_DIR/name
        backup=recovery/name
        if path.exists() and not backup.exists():
            shutil.copy2(path,backup); os.chmod(backup,0o600)
        temporary=path.with_name(name+'.privacy-refresh.tmp')
        nodes=edges=0
        with temporary.open('w',encoding='utf-8') as f:
            if name=='node_index_brain_v2.jsonl':
                for row in conn.execute('SELECT id,title,type,family,status,owner,path,summary,last_modified,centrality FROM nodes'):
                    if row['id'] in denied: continue
                    n=dict(row); n['node_type']=n.pop('type')
                    f.write(json.dumps(n,ensure_ascii=False)+'\n'); nodes+=1
            elif name=='triples_brain_v2.jsonl':
                for row in conn.execute('SELECT from_id,to_id,predicate,family,weight,confidence,asserted_by,asserted_at,evidence_span FROM edges'):
                    if row['from_id'] in denied or row['to_id'] in denied: continue
                    e=dict(row); e['subject']=e.pop('from_id'); e['object']=e.pop('to_id'); e['verb']=e.pop('predicate')
                    f.write(json.dumps(e,ensure_ascii=False)+'\n'); edges+=1
            else:
                f.write('{"nodes":[')
                for row in conn.execute('SELECT id,type,family,title,path,owner,status,governance_tier,centrality FROM nodes'):
                    if row['id'] in denied: continue
                    if nodes: f.write(',')
                    f.write(json.dumps(dict(row),ensure_ascii=False)); nodes+=1
                f.write('],"edges":[')
                for row in conn.execute('SELECT from_id,to_id,predicate,family,weight,confidence,asserted_by FROM edges'):
                    if row['from_id'] in denied or row['to_id'] in denied: continue
                    if edges: f.write(',')
                    f.write(json.dumps(dict(row),ensure_ascii=False)); edges+=1
                f.write('],"node_count":'+str(nodes)+',"edge_count":'+str(edges)+'}')
            f.flush(); os.fsync(f.fileno())
        os.replace(temporary,path)
        outputs[name]={'nodes_exported':nodes,'edges_exported':edges,'bytes':path.stat().st_size,'privacy_versions_omitted':len(denied)}
    conn.rollback(); conn.close()
    return outputs

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(); p.add_argument('--recovery',required=True)
    print(json.dumps(run(p.parse_args().recovery),indent=2))
