"""Offline integration and scoring contracts; no model-quality claims."""
import hashlib,io,json,sqlite3
from pathlib import Path
from unittest import mock
import pytest
from engine import brain as B,db as DB,config as C,http_server as H,mcp_server as M
from engine import retrieval_eval as RE
from engine.extractors import markdown as MD
from engine.maintenance.metadata_backfill import plan_or_apply
import arcturion_knowledge as AK
import learning_core as LC

@pytest.fixture
def db(tmp_path,monkeypatch):
    path=tmp_path/'fixture.sqlite'
    monkeypatch.setattr(C,'BRAIN_DB',path)
    c=DB.connect(path); DB.init_db(c)
    yield c
    c.close()

def note(tmp_path,extra='',body='A unique calibration condition requires the copper approval.'):
    p=tmp_path/'COVENANT'/'000 Covenant'/'Knowledge'/'Standards'/'note.md'
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text('---\ntitle: Calibration conditions\nagent: COVENANT\nsubject: calibration\ntype: knowledge\n'+extra+'---\n'+body)
    return p

def insert(c,p):
    n,_=MD.extract_markdown(p)
    DB.upsert_node(c,n); c.commit()
    return n

def handler_get(path):
    h=object.__new__(H.Handler); h.path=path
    h._json=lambda status,data:(status,data)
    h._send=lambda status,data,*rest:(status,data)
    return h.do_GET()

def local_http(url,timeout=None):
    from urllib.parse import urlsplit
    parsed=urlsplit(url)
    code,data=handler_get(parsed.path+'?'+parsed.query)
    if code!=200:
        import urllib.error
        raise urllib.error.HTTPError(url,code,'fixture',{},None)
    return io.BytesIO((json.dumps(data) if isinstance(data,dict) else data).encode())

def test_agent_fallback_and_conflict(tmp_path):
    p=note(tmp_path)
    node,_=MD.extract_markdown(p)
    assert node['owner']=='COVENANT'
    assert node['metadata']['ownership_source']=='agent'
    assert node['metadata']['subject']=='calibration'
    p=note(tmp_path,'owner: ALTAIR\n')
    n,_=MD.extract_markdown(p)
    assert n['owner']=='ALTAIR'
    assert n['metadata']['ownership_conflict']=={'selected_field':'owner','selected':'ALTAIR','agent':'COVENANT'}

def test_explicit_author_fallback_is_preserved(tmp_path):
    p=note(tmp_path,'authored_by: ALTAIR\n')
    n,_=MD.extract_markdown(p)
    assert n['owner']=='ALTAIR' and n['metadata']['ownership_source']=='authored_by'

def test_pack_structured_sources_preserve_text():
    pack=B.ContextPack('topic',nodes=[{'id':'a','path':'x','content_hash':'abc','metadata':'{"subject":"calibration"}'}],text_bundle='same')
    result=pack.to_dict()
    assert result['text_bundle']=='same'
    assert result['sources'][0]['subject']=='calibration'

@pytest.mark.parametrize('body,expected',[(b'# Context Pack','decode'),(b'[]','invalid_response_contract'),(b'{"error":"bad"}','invalid_response_contract')])
def test_query_bad_response_is_error(body,expected):
    with mock.patch.object(AK.urllib.request,'urlopen',return_value=io.BytesIO(body)):
        got=AK.brain_query('topic')
    assert got['status']=='error' and got['error']==expected

@pytest.mark.parametrize('exception,expected',[(TimeoutError(),'timeout'),(AK.urllib.error.URLError('offline'),'transport'),(AK.urllib.error.HTTPError('url',503,'down',{},None),'http_503')])
def test_query_transport_errors_distinguished(exception,expected):
    with mock.patch.object(AK.urllib.request,'urlopen',side_effect=exception):
        assert AK.brain_query('topic')['error']==expected

@pytest.mark.parametrize('sources,state',[([], 'empty'),([{'subject':'another'}],'empty'),([{'id':'a','subject':'calibration'}],'hit')])
def test_subject_hit_is_not_truthy_envelope(tmp_path,sources,state):
    with mock.patch.object(AK,'brain_query',return_value={'status':'ok','pack':{'sources':sources,'node_count':9},'error':None}):
        got=AK.query(tmp_path,'Calibration','question')
    assert got['brain_status']==state and got['brain_hit']==(state=='hit')

def test_missing_subject_contract_errors(tmp_path):
    with mock.patch.object(AK,'brain_query',return_value={'status':'ok','pack':{'citations':['x']},'error':None}):
        assert AK.query(tmp_path,'Calibration','x')['brain_status']=='error'

def test_error_cli_nonzero(tmp_path,capsys):
    room=tmp_path/'ALTAIR'/'000 Altair'; room.mkdir(parents=True)
    with mock.patch.object(AK,'brain_query',return_value={'status':'error','pack':None,'error':'decode'}):
        assert AK.main(['query','Calibration','x','--room',str(room)])==4
    assert '"brain_status": "error"' in capsys.readouterr().out

def test_actual_capture_receipt_lexical_and_semantic(db,tmp_path):
    p=note(tmp_path); n=insert(db,p)
    with mock.patch.object(AK.urllib.request,'urlopen',side_effect=local_http):
        result=AK._capture_visibility(p)
    assert result['status']=='hit'
    assert result['source']['id']==n['id']
    assert result['source']['content_hash']==hashlib.sha256(p.read_bytes()).hexdigest()
    assert result['source']['lexical_indexed'] is True
    assert result['source']['semantic_indexed'] is False

def test_same_title_and_wrong_version_cannot_pass_receipt(db,tmp_path):
    p=note(tmp_path); n=insert(db,p)
    p.write_text(p.read_text()+'\nChanged condition.')
    with mock.patch.object(AK.urllib.request,'urlopen',side_effect=local_http):
        assert AK._capture_visibility(p)['status']=='empty'
    decoy=tmp_path/'decoy.md'; decoy.write_text(p.read_text())
    with mock.patch.object(AK.urllib.request,'urlopen',side_effect=local_http):
        assert AK._capture_visibility(decoy)['status']=='empty'

def test_excluded_receipt_is_empty(db,tmp_path):
    p=note(tmp_path); n=insert(db,p)
    db.execute("INSERT INTO node_retrieval_quarantine(node_id,content_hash,reason,scope) VALUES (?,?,?,?)",(n['id'],n['content_hash'],'synthetic-withdrawal','all')); db.commit()
    assert B.capture_receipt(str(p))['status']=='empty'

def test_receipt_rejects_relative_paths(db):
    assert handler_get('/receipt?path=relative.md')[0]==400

def test_capture_and_query_end_to_end(db,tmp_path):
    p=note(tmp_path); n=insert(db,p)
    # Genuine retrieval through the existing engine, no mocked answer generation.
    with mock.patch.object(AK.urllib.request,'urlopen',side_effect=local_http):
        got=AK.query(p.parents[2],'Calibration','conditions copper approval')
    assert got['brain_status']=='hit'
    assert got['matched_sources'][0]['content_hash']==n['content_hash']

def test_http_owner_parameter_parity():
    pack=B.ContextPack('x')
    with mock.patch.object(B,'query',return_value=pack) as query:
        assert handler_get('/query?topic=x&format=json&owner=ALTAIR&owner_strict=true')[0]==200
        assert query.call_args.kwargs['owner']=='ALTAIR'
        assert query.call_args.kwargs['owner_strict'] is True
        h=object.__new__(H.Handler); h.path='/query'
        h._read_json_body=lambda:({'topic':'x','owner':'ALTAIR','owner_strict':True},None)
        h._json=lambda status,data:(status,data)
        h.do_POST()
        assert query.call_args.kwargs['owner']=='ALTAIR'
        assert query.call_args.kwargs['owner_strict'] is True

def test_mcp_format_and_receipt_parity(db,tmp_path):
    p=note(tmp_path); n=insert(db,p)
    result=M.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'brain_query','arguments':{'topic':n['id'],'format':'json'}}})
    pack=json.loads(result['result']['content'][0]['text'])
    assert any(s['id']==n['id'] for s in pack['sources'])
    result=M.handle({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'brain_capture_receipt','arguments':{'path':str(p)}}})
    receipt=json.loads(result['result']['content'][0]['text'])
    assert receipt['sources'][0]['content_hash']==n['content_hash']

def test_bounded_backfill_dry_run_apply_and_rollback(db,tmp_path):
    p=note(tmp_path); n=insert(db,p)
    db.execute('UPDATE nodes SET owner=NULL,metadata=? WHERE id=?',('{}',n['id'])); db.commit()
    proposal=plan_or_apply(db,[p])
    assert proposal['changes'][0]['after']['owner']=='COVENANT'
    assert db.execute('SELECT owner FROM nodes WHERE id=?',(n['id'],)).fetchone()[0] is None
    db.execute('BEGIN')
    result=plan_or_apply(db,[p],apply=True)
    assert result['applied']
    assert db.execute('SELECT owner FROM nodes WHERE id=?',(n['id'],)).fetchone()[0]=='COVENANT'
    db.rollback()
    assert db.execute('SELECT owner FROM nodes WHERE id=?',(n['id'],)).fetchone()[0] is None

def test_backfill_refuses_changed_source_batch(db,tmp_path):
    p=note(tmp_path); insert(db,p); p.write_text(p.read_text()+'\nchanged')
    with pytest.raises(ValueError):plan_or_apply(db,[p],apply=True)

def test_restore_fixture_keeps_exclusion_and_actual_retrieval(db,tmp_path,monkeypatch):
    p=note(tmp_path); allowed=insert(db,p)
    denied={**allowed,'id':'withdrawn-fixture','title':'Withdrawn fixture','content_hash':'withdrawn-hash','path':'withdrawn.md'}
    DB.upsert_node(db,denied)
    db.execute('INSERT INTO node_retrieval_quarantine(node_id,content_hash,reason,scope) VALUES (?,?,?,?)',(denied['id'],denied['content_hash'],'synthetic-withdrawal','all'));db.commit()
    restored=tmp_path/'restored-fixture.sqlite'
    target=sqlite3.connect(restored);db.backup(target);target.close()
    monkeypatch.setattr(C,'BRAIN_DB',restored)
    assert any(x['id']==allowed['id'] for x in B.query(allowed['id']).nodes)
    assert not B.query(denied['id']).nodes

def test_retrieval_evaluator_checks_versions_and_full_pack_exclusions(tmp_path):
    entry={'id':'lifecycle','class':'exact-token','q':'synthetic version','note':'Development scorer contract only','expect':['a'],'version_expectations':{'a':'new'},'forbid_anywhere':['old']}
    fixture=tmp_path/'development.jsonl';fixture.write_text(json.dumps(entry)+'\n')
    entry=RE.load_golden(fixture)[0]
    assert RE.score_entry(entry,[{'id':'a','content_hash':'new'}])['passed']
    assert not RE.score_entry(entry,[{'id':'a','content_hash':'stale'}])['passed']
    nodes=[{'id':'a','content_hash':'new'}]+[{'id':str(i)} for i in range(6)]+[{'id':'old'}]
    assert not RE.score_entry(entry,nodes)['passed']

def test_knowledge_lift_trace_scorer_rejects_false_and_missing_fields():
    case={'task_id':'trace-contract-unit','subject':'synthetic','prompt':'return JSON','criteria':['deny revoked action'],'trace_checks':[{'path':'action.allowed','equals':False}]}
    responder=mock.Mock(side_effect=['{"action":{"allowed":true}}','{"action":{"allowed":false}}'])
    judge=mock.Mock(side_effect=AssertionError('no model judge allowed'))
    got=LC.run_knowledge_lift_case(case,'synthetic revoked condition',responder,judge,with_is_label='A')
    assert got['with_knowledge']==10.0 and got['without_knowledge']==0.0
    assert got['evaluator']=='declared-output-contract'
    responder=mock.Mock(return_value='{"action":{}}')
    got=LC.run_knowledge_lift_case(case,'x',responder,judge)
    assert got['with_knowledge']==0.0
    assert not judge.called

def test_metadata_backfill_detects_change_after_read(db,tmp_path):
    p=note(tmp_path); n=insert(db,p)
    db.execute('UPDATE nodes SET owner=NULL,metadata=? WHERE id=?',('{}',n['id'])); db.commit()
    class ConcurrentEdit:
        @property
        def in_transaction(self): return db.in_transaction
        def execute(self,sql,args=()):
            if sql.startswith('UPDATE nodes SET owner='):
                db.execute('UPDATE nodes SET metadata=? WHERE id=?',('{"concurrent":true}',n['id']))
            return db.execute(sql,args)
    db.execute('BEGIN')
    with pytest.raises(RuntimeError,match='concurrently'):
        plan_or_apply(ConcurrentEdit(),[p],apply=True)
    assert db.execute('SELECT owner FROM nodes WHERE id=?',(n['id'],)).fetchone()[0] is None

@pytest.mark.parametrize('cause',['timeout','decode','http_503'])
def test_capture_deadline_exposes_typed_cause(tmp_path,cause):
    p=note(tmp_path)
    with mock.patch.object(AK,'_capture_visibility',return_value={'status':'error','error':cause}):
        ok,detail=AK.verify_capture(p,'ignored title',wait_s=0)
    assert ok is False and json.loads(detail)=={'status':'error','error':cause}

def test_capture_removed_source_is_explicit(tmp_path):
    assert AK._capture_visibility(tmp_path/'missing.md')=={'status':'error','error':'source_unreadable'}

def test_legacy_mcp_text_remains_text(db,tmp_path):
    n=insert(db,note(tmp_path))
    result=M.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'brain_query','arguments':{'topic':n['id']}}})
    assert result['result']['content'][0]['text'].startswith('# Context Pack')

def test_capture_reports_semantic_state(tmp_path):
    p=note(tmp_path)
    src={'id':'synthetic','absolute_path':str(p),'content_hash':hashlib.sha256(p.read_bytes()).hexdigest(),'lexical_indexed':True,'semantic_indexed':False}
    with mock.patch.object(AK,'_capture_visibility',return_value={'status':'hit','source':src}):
        ok,detail=AK.verify_capture(p,'ignored title',wait_s=0)
    assert ok and json.loads(detail)['semantic_indexed'] is False

def test_metadata_apply_requires_explicit_transaction(db,tmp_path):
    p=note(tmp_path);insert(db,p)
    with pytest.raises(ValueError,match='caller-owned'):
        plan_or_apply(db,[p],apply=True)

def test_json_request_explicit_format():
    with mock.patch.object(AK.urllib.request,'urlopen',return_value=io.BytesIO(b'{"sources":[]}')) as call:
        AK.brain_query('calibration')
    assert 'format=json' in call.call_args.args[0]
