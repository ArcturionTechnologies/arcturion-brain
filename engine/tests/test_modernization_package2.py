"""Finite synthetic lifecycle/enforcement tests; no model-quality claim."""
import hashlib
from unittest import mock
import pytest
import learning_core as LC

def test_registered_version_drift_refuses_compile_before_capture(tmp_path):
    room=tmp_path/'ALTAIR'/'000 Altair'
    (room/'Knowledge').mkdir(parents=True)
    source=tmp_path/'source.md'
    source.write_text('Approval requires calibration against the copper standard.')
    LC.create_course(room,'calibration',['Apply calibration'],[])
    record=LC.ingest_source(room,'calibration',str(source),'primary','owned')
    before=LC.load_course(room,'calibration')
    source.write_text('Approval requires calibration against the silver standard.')
    capture=mock.Mock()
    with pytest.raises(ValueError,match='source version changed'):
        LC.compile_units(room,'calibration',[{'unit_id':'new','source_id':record['source_id']}],capture)
    capture.assert_not_called()
    assert LC.load_course(room,'calibration')==before

@pytest.mark.parametrize('digest',[None,'','abc','0'*64])
def test_source_digest_missing_invalid_or_mismatch_refuses(tmp_path,digest):
    source=tmp_path/'source.md'; source.write_bytes(b'Exact version evidence.')
    with pytest.raises(ValueError,match='source (digest|version)'):
        LC._source_text({'origin':str(source),'sha256':digest})

def test_exact_source_digest_reads_same_bytes(tmp_path):
    source=tmp_path/'source.md'; raw=b'Exact version evidence.\r\n'
    source.write_bytes(raw)
    assert LC._source_text({'origin':str(source),'sha256':hashlib.sha256(raw).hexdigest()})==raw.decode()

import importlib.util
import io
import json
import os
import sqlite3
import sys
from pathlib import Path
from engine import brain as B, db as DB, config as C, http_server as H, mcp_server as M
from engine import memory_policy as MP
from engine.source_integrity import filter_retrieval

IDENTITY={"principal":"fixture-reader","capacity":"business"}
OTHER={"principal":"fixture-other","capacity":"business"}

@pytest.fixture
def db(tmp_path,monkeypatch):
    path=tmp_path/'fixture.sqlite'; monkeypatch.setattr(C,'BRAIN_DB',path)
    conn=DB.connect(path); DB.init_db(conn)
    yield conn
    conn.close()

def put(conn,node_id,**kw):
    node={"id":node_id,"type":"Document","family":"knowledge","title":node_id,
          "path":"/fixture/"+node_id,"content_hash":hashlib.sha256(node_id.encode()).hexdigest(),
          "status":"active","summary":"synthetic","metadata":{}}
    node.update(kw); DB.upsert_node(conn,node); conn.commit()
    return dict(conn.execute('SELECT * FROM nodes WHERE id=?',(node_id,)).fetchone())

def rec(node,**kw):
    return {"content_hash":node["content_hash"],**kw}

@pytest.fixture
def policy(tmp_path,monkeypatch):
    path=tmp_path/'policy.json'
    monkeypatch.setenv('ARCTURION_MEMORY_POLICY',str(path))
    monkeypatch.setenv('ARCTURION_MEMORY_PRINCIPAL',IDENTITY['principal'])
    monkeypatch.setenv('ARCTURION_MEMORY_CAPACITY',IDENTITY['capacity'])
    def save(records=None,grants=None,**kw):
        data={"version":1,**IDENTITY,"issuers":["fixture-issuer"],"records":records or {},"grants":grants or []}
        data.update(kw); path.write_text(json.dumps(data)); path.chmod(0o600)
        return data
    save.path=path; save()
    return save

def request():
    return {"grant_id":"g","grant_version":1,"action":"fixture-publish","scope":"fixture-report"}

def grant(**kw):
    return {"id":"g","version":1,"issuer":"fixture-issuer",**IDENTITY,
            "action":"fixture-publish","scope":"fixture-report","revoked":False,
            "valid_from":"2020-01-01T00:00:00Z","valid_until":"2100-01-01T00:00:00Z","dependencies":[],**kw}

def get(path):
    handler=object.__new__(H.Handler); handler.path=path
    handler._json=lambda status,data:(status,data)
    handler._send=lambda status,data,*rest:(status,data)
    return handler.do_GET()

def mcp(name,args):
    reply=M.handle({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":name,"arguments":args}})
    return json.loads(reply["result"]["content"][0]["text"])

def test_acl_all_retrieval_routes_do_not_trust_query_owner(db,policy):
    secret=put(db,'secret_fixture',title='Zircon restricted fixture',owner='COVENANT')
    policy({secret['id']:rec(secret,readers=[OTHER])})
    assert filter_retrieval(db,[secret])==[]
    assert filter_retrieval(db,[secret],historical=True)==[]
    assert B.query('secret_fixture',owner='COVENANT',owner_strict=True).nodes==[]
    assert mcp('brain_query',{'topic':'secret_fixture','format':'json','owner':'COVENANT','principal':'fixture-other','capacity':'test'})['sources']==[]
    assert get('/search?text=Zircon')[1]==[]
    assert mcp('brain_search',{'text':'Zircon'})==[]
    assert get('/graph_subset?search=zircon')[1]=={'nodes':[],'links':[]}
    assert B.capture_receipt(secret['path'])['sources']==[]

def test_denied_bridge_cannot_discover_allowed_neighbor(db,policy):
    a=put(db,'seed'); b=put(db,'bridge'); d=put(db,'remote')
    for left,right in (('seed','bridge'),('bridge','remote')):
        db.execute("INSERT INTO edges(from_id,to_id,predicate,family) VALUES(?,?,?,?)",(left,right,'relates_to','knowledge'))
    db.commit(); policy({'bridge':rec(b,readers=[OTHER])})
    reached,edges=B._expand(db,{'seed'},2,['relates_to'])
    assert reached=={'seed'} and edges==[]

def test_indexed_version_change_cannot_remove_restriction(db,policy):
    a=put(db,'secret'); policy({'secret':rec(a,readers=[OTHER])})
    changed=put(db,'secret',content_hash='new-version',metadata={})
    assert filter_retrieval(db,[changed])==[]

@pytest.mark.parametrize('problem',['missing','malformed','unsafe','identity','partial','symlink'])
def test_configured_policy_failure_refuses_retrieval(db,policy,monkeypatch,problem):
    node=put(db,'public')
    if problem=='missing':policy.path.unlink()
    if problem=='malformed':policy.path.write_text('{')
    if problem=='unsafe':policy.path.chmod(0o666)
    if problem=='identity':policy(principal='forged')
    if problem=='partial':monkeypatch.delenv('ARCTURION_MEMORY_CAPACITY')
    if problem=='symlink':
        target=policy.path.with_name('target.json'); policy.path.rename(target); policy.path.symlink_to(target)
    with pytest.raises(MP.PolicyError):
        B.query('public')
    with pytest.raises(MP.PolicyError):
        filter_retrieval(db,[node])

def test_full_row_metadata_narrows_projected_search(db,policy):
    node=put(db,'secret',title='Zircon',metadata={'memory_readers':[OTHER]})
    policy()
    assert get('/search?text=Zircon')[1]==[]
    assert mcp('brain_search',{'text':'Zircon'})==[]

@pytest.mark.parametrize('problem',['withdrawal','missing','drift','cycle'])
def test_dependency_closure_and_restore_preserve_independent_fact(db,policy,tmp_path,problem):
    a=put(db,'source'); b=put(db,'summary'); d=put(db,'independent')
    deps=[{'id':'source','content_hash':a['content_hash']}]
    records={'source':rec(a),'summary':rec(b,dependencies=deps)}
    snapshot=tmp_path/'restore.sqlite'
    with sqlite3.connect(snapshot) as restored:db.backup(restored)
    if problem=='withdrawal':records['source']['withdrawn']=True
    if problem=='missing':deps[0]['id']='missing'
    if problem=='drift':deps[0]['content_hash']='changed'
    if problem=='cycle':records['source']['dependencies']=[{'id':'summary','content_hash':b['content_hash']}]
    policy(records)
    assert [x['id'] for x in filter_retrieval(db,[b,d])]==['independent']
    restored=DB.connect(snapshot)
    try:assert [x['id'] for x in filter_retrieval(restored,[b,d],historical=True)]==['independent']
    finally:restored.close()

@pytest.mark.parametrize('change,reason',[
    ({'issuer':'unknown'},'unknown_issuer'),({'revoked':True},'revoked_or_unknown'),
    ({'valid_until':'2000-01-01T00:00:00Z'},'outside_validity'),
    ({'valid_from':'2200-01-01T00:00:00Z'},'outside_validity'),
    ({'valid_until':'invalid'},'invalid_grant'),({'version':2},'grant_version_changed'),
    ({'principal':'other'},'principal_capacity_mismatch'),
    ({'dependencies':[{'id':'gone','content_hash':'abc'}]},'dependency_unavailable')])
def test_current_action_failure_observed_callback_not_called(policy,change,reason):
    policy(grants=[grant(**change)]); p=MP.load_policy(); callback=mock.Mock()
    assert p.action_eligibility(request(),lambda x:None)['reason']==reason
    with pytest.raises(PermissionError):MP.guarded_call(p,request(),lambda x:None,callback)
    callback.assert_not_called()

def test_action_absence_and_positive_control(policy):
    p=MP.load_policy()
    assert p.action_eligibility(request(),lambda x:None)['reason']=='grant_absent_or_ambiguous'
    policy(grants=[grant()]); p=MP.load_policy(); callback=mock.Mock(return_value='observed')
    assert MP.guarded_call(p,request(),lambda x:None,callback)=='observed'
    callback.assert_called_once()

@pytest.fixture
def capsule(tmp_path,monkeypatch):
    root=Path(__file__).resolve().parents[3]
    source=root/'adaptive-context/context_control.py'
    spec=importlib.util.spec_from_file_location('modernization_context',source)
    cc=importlib.util.module_from_spec(spec); sys.modules[spec.name]=cc; spec.loader.exec_module(cc)
    state=tmp_path/'context'
    for name,value in [('STATE_ROOT',state),('CAPSULE_ROOT',state/'capsules'),('SESSION_ROOT',state/'sessions')]:
        monkeypatch.setattr(cc,name,value)
    monkeypatch.setenv('ARCTURION_BRAIN_CODE_ROOT',str(root/'brain'))
    return cc

def test_capsule_real_serialization_rechecks_grants_and_legacy_claims(capsule,policy):
    cc=capsule
    cc.save_capsule({'goal':'fixture','permissions':['publish anything'],'action_requests':[request()]},'session','ALTAIR')
    policy(grants=[grant()])
    text=cc.capsule_context('session')
    assert '"allowed": true' in text and '"permissions": []' in text
    assert '"omitted_permission_claims": 1' in text and 'publish anything' not in text
    policy(grants=[grant(revoked=True)])
    assert '"allowed": false' in cc.capsule_context('session')
    assert 'revoked_or_unknown' in cc.capsule_context('session')

def test_capsule_declared_transitive_withdrawal_refuses_ensure_and_serialization(capsule,policy,tmp_path):
    cc=capsule
    a=tmp_path/'source'; a.write_text('original')
    b=tmp_path/'summary'; b.write_text('derived')
    c=cc.save_capsule({'goal':'derived','action_requests':[request()]},'session','ALTAIR')
    digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    records={'a':{'content_hash':digest(a),'path':str(a),'withdrawn':True},
      'b':{'content_hash':digest(b),'path':str(b),'dependencies':[{'id':'a','content_hash':digest(a)}]},
      'capsule:'+c.stem:{'content_hash':digest(c),'path':str(c),'dependencies':[{'id':'b','content_hash':digest(b)}]}}
    policy(records,grants=[grant()])
    assert 'policy_refused' in cc.capsule_context('session')
    with pytest.raises(ValueError,match='refused resume'):
        cc.ensure_capsule({'session_id':'session'},'ALTAIR')

@pytest.mark.parametrize('field,value',[('version',True),('version','1')])
def test_malformed_grant_version_denies(policy,field,value):
    policy(grants=[grant(**{field:value})])
    assert MP.load_policy().action_eligibility(request(),lambda x:None)['allowed'] is False

@pytest.mark.parametrize('field,value',[('capacity','test'),('principal',' '),('grants',[{'id':''}])])
def test_malformed_policy_identity_is_typed(policy,field,value):
    policy(**{field:value})
    with pytest.raises(MP.PolicyError): MP.load_policy()

def test_capsule_brain_dependency_honors_current_quarantine_after_restore(db,capsule,policy,tmp_path,monkeypatch):
    a=put(db,'origin'); b=put(db,'derived')
    c=capsule.save_capsule({'goal':'fixture'},'session','ALTAIR')
    records={'origin':rec(a,kind='brain'),'derived':rec(b,kind='brain',dependencies=[{'id':'origin','content_hash':a['content_hash']}]),
        'capsule:'+c.stem:{'content_hash':hashlib.sha256(c.read_bytes()).hexdigest(),'path':str(c),'dependencies':[{'id':'derived','content_hash':b['content_hash']}]}}
    policy(records)
    before=c.read_bytes()
    assert capsule.ensure_capsule({'session_id':'session'},'ALTAIR')[0]==c
    assert c.read_bytes()==before
    assert capsule.ensure_capsule({'session_id':'session'},'ALTAIR')[0]==c
    assert c.read_bytes()==before
    assert 'policy_refused' not in capsule.capsule_context('session')
    db.execute("INSERT INTO node_retrieval_quarantine(node_id,content_hash,reason,scope) VALUES(?,?,?,?)",('origin',a['content_hash'],'fixture-current-exclusion','current'))
    db.commit()
    assert filter_retrieval(db,[a],historical=True) # permitted historical fact, never action
    assert 'policy_refused' in capsule.capsule_context('session')
    restored=tmp_path/'restored.sqlite'
    with sqlite3.connect(restored) as dest:db.backup(dest)
    monkeypatch.setattr(C,'BRAIN_DB',restored)
    assert 'policy_refused' in capsule.capsule_context('session')
    monkeypatch.setattr(C,'BRAIN_DB',tmp_path/'missing.sqlite')
    assert 'policy_refused' in capsule.capsule_context('session')

def test_actual_rehydrate_refuses_denied_source(capsule,policy,tmp_path,monkeypatch):
    cc=capsule; source=tmp_path/'source.md'; source.write_text('independent context')
    entry={'id':'fixture-entry','kind':'domain','canonical_path':str(source),'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
           'owner':'ALTAIR','estimated_tokens':10,'authority_tier':0}
    monkeypatch.setattr(cc,'_load_session_state',lambda _: {'selected':{entry['id']:entry['sha256']}})
    monkeypatch.setattr(cc,'load_catalog',lambda **_: {'entries':[entry]})
    monkeypatch.setattr(cc,'refresh_registered_mutable',lambda catalog,*_:catalog)
    policy({entry['id']:{'content_hash':entry['sha256'],'path':str(source),'readers':[OTHER]}})
    with pytest.raises(ValueError,match='policy refused rehydration'):
        cc.rehydrate_active_context('session','ALTAIR')

def test_stale_projected_version_cannot_borrow_current_authorization(db,policy):
    original=put(db,'source',summary='old-version')
    current=put(db,'source',summary='new-version',content_hash='new-version-hash')
    policy({'source':rec(current,readers=[IDENTITY])})
    assert filter_retrieval(db,[original])==[]
    assert filter_retrieval(db,[current])==[current]

def test_capsule_loaded_version_cannot_borrow_replacement_authorization(capsule,policy,monkeypatch):
    cc=capsule
    path=cc.save_capsule({'goal':'older secret'},'session','ALTAIR')
    original,_=cc.load_capsule('session')
    cc.save_capsule({'goal':'current permitted'},'session','ALTAIR')
    policy({'capsule:'+path.stem:{'content_hash':hashlib.sha256(path.read_bytes()).hexdigest(),'path':str(path)}})
    monkeypatch.setattr(cc,'load_capsule',lambda _:(original,None))
    output=cc.capsule_context('session')
    assert 'policy_refused' in output and 'older secret' not in output
