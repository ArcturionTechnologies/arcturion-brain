import hashlib,json
from engine import source_integrity as S,db as DB
from engine.extractors.config_file import extract_config
from engine.walker import walk_paths

def register(monkeypatch,tmp_path,value):
    raw=json.dumps(value,indent=2).encode()
    p=tmp_path/'registry.json'
    p.write_text(json.dumps({'content_hashes':[hashlib.sha256(raw).hexdigest()],
                            'canonical_json_identities':[S.canonical_json_identity(value)]}))
    monkeypatch.setattr(S,'RESTRICTED_ARTIFACT_REGISTRY',p)
    return raw

def test_private_metadata_inventory_cannot_be_rewrapped(monkeypatch,tmp_path):
    original=[{'id':'fixture','owner':'private-owner','status':'archived'}]
    raw=register(monkeypatch,tmp_path,original)
    for wrapped in ({'padding':'x'*8000,'saved':original},{'saved_json':raw.decode()}):
        p=tmp_path/'new-name.json';p.write_text(json.dumps(wrapped))
        n,_=extract_config(p)
        assert n['metadata']['restricted_artifact_reason']
        c=DB.connect(tmp_path/'fixture.sqlite');DB.init_db(c);DB.upsert_node(c,n)
        assert c.execute('SELECT body_excerpt FROM nodes WHERE id=?',(n['id'],)).fetchone()[0] is None
        assert c.execute('SELECT COUNT(*) FROM fts_nodes WHERE node_id=?',(n['id'],)).fetchone()[0]==0
        c.close()

def test_semantic_exclusion_beyond_excerpt_is_guarded(monkeypatch,tmp_path):
    register(monkeypatch,tmp_path,[])
    p=tmp_path/'new-name.json'
    p.write_text(json.dumps({'padding':'x'*8000,'nested':{'summary':'Synthetic Founder alcohol-history fixture.'}}))
    n,_=extract_config(p)
    assert n['metadata']['restricted_artifact_reason']=='wrapped-excluded-personal-source-2026-10-02'

def test_general_nonpersonal_owner_reference_is_retained(monkeypatch,tmp_path):
    register(monkeypatch,tmp_path,[])
    p=tmp_path/'science.json'
    p.write_text(json.dumps({'owner':'Founder','summary':'An alcohol thermometer measures temperature.'}))
    n,_=extract_config(p)
    assert not n['metadata'].get('restricted_artifact_reason')

def test_restricted_recovery_direct_and_symlink_walk_refused(monkeypatch,tmp_path):
    root=tmp_path/'recovery';root.mkdir();p=root/'fixture.json';p.write_text('{}')
    monkeypatch.setattr(S,'RESTRICTED_RECOVERY_ROOT',root)
    alias=tmp_path/'public.json';alias.symlink_to(p)
    assert list(walk_paths([p,alias,root]))==[]

def test_public_snapshot_identity_preserves_canonical_history(monkeypatch,tmp_path):
    register(monkeypatch,tmp_path,{'restricted_inventory':['synthetic-private-path']})
    recovery=tmp_path/'recovery';recovery.mkdir()
    monkeypatch.setattr(S,'RESTRICTED_RECOVERY_ROOT',recovery)
    canonical=tmp_path/'public.json'
    canonical.write_text(json.dumps({'summary':'Public engineering reference.'}))
    snapshot=recovery/'snapshot.json';snapshot.write_bytes(canonical.read_bytes())
    assert list(walk_paths([snapshot]))==[]
    assert len(list(walk_paths([canonical])))==1
    node,_=extract_config(canonical)
    assert not node['metadata'].get('restricted_artifact_reason')
    c=DB.connect(tmp_path/'canonical.sqlite');DB.init_db(c);DB.upsert_node(c,node)
    assert S.filter_retrieval(c,[node],historical=True)==[node]
    wrapper=tmp_path/'private-wrapper.json'
    wrapper.write_text(json.dumps({'padding':'x'*8000,'saved':{'summary':'Synthetic Founder alcohol-history fixture.'}}))
    denied,_=extract_config(wrapper);DB.upsert_node(c,denied)
    assert S.filter_retrieval(c,[denied],historical=True)==[]
    c.close()

def test_persistent_path_guard_does_not_deny_identical_public_bytes(monkeypatch,tmp_path):
    recovery=tmp_path/'recovery';recovery.mkdir()
    monkeypatch.setattr(S,'RESTRICTED_RECOVERY_ROOT',recovery)
    # Trigger installation also stores the root relative to the configured home.
    monkeypatch.setattr(S.Path,'home',classmethod(lambda cls:tmp_path))
    c=DB.connect(tmp_path/'path-guard.sqlite');DB.init_db(c)
    c.execute('INSERT INTO nodes(id,type,family,title,path,summary,content_hash) VALUES (?,?,?,?,?,?,?)',
              ('private-location','Config','code','Public reference',str(recovery/'before.json'),'Public engineering reference.','public-bytes'))
    assert c.execute('SELECT summary FROM nodes WHERE id=?',('private-location',)).fetchone()[0] is None
    public={'id':'canonical','type':'Config','family':'code','title':'Public reference',
            'path':str(tmp_path/'public.json'),'summary':'Public engineering reference.','content_hash':'public-bytes'}
    DB.upsert_node(c,public)
    assert S.filter_retrieval(c,[public],historical=True)==[public]
    assert c.execute('SELECT COUNT(*) FROM fts_nodes WHERE node_id=?',('canonical',)).fetchone()[0]==1
    c.close()

