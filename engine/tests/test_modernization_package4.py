"""Finite proposal-only compactor integration; no production admission."""
import hashlib,json,sys
from unittest import mock
import pytest
from engine import mcp_server as M
from engine.maintenance import compactor as CP
from engine.tests.test_modernization_package2 import db,put,rec,policy

def source(db,tmp_path,node_id,text):
    path=tmp_path/(node_id+'.md'); path.write_text(text)
    return put(db,node_id,path=str(path),content_hash=hashlib.sha256(path.read_bytes()).hexdigest())

def test_proposal_lane_preserves_exact_conditions_attribution_and_no_publish(db,tmp_path,monkeypatch):
    text='---\nowner: ALTAIR\n---\nApply only after approval. Exception: expired calibration requires refusal.'
    a=source(db,tmp_path,'source-a',text)
    dest=tmp_path/'proposals'; monkeypatch.setattr(M,'STAGING_DIR',dest)
    notify=mock.Mock(); monkeypatch.setattr(M,'_append_covenant_queue',notify)
    before=db.execute('SELECT count(*) FROM nodes').fetchone()[0]
    monkeypatch.setattr(sys,'argv',['compactor','--propose-consolidation','source-a'])
    assert CP.main()==0
    files=list(dest.glob('*.jsonl')); assert len(files)==1
    proposal=json.loads(files[0].read_text())
    assert proposal['sections'][0]['verbatim_text']==text
    assert proposal['sections'][0]['source_path']==a['path']
    assert proposal['authority']=='none' and not proposal['publication_eligible']
    assert CP.review_consolidation(proposal)['inputs_current']
    assert db.execute('SELECT count(*) FROM nodes').fetchone()[0]==before
    assert open(a['path']).read()==text
    notify.assert_not_called()

@pytest.mark.parametrize('change',['file','index','exclusion','policy','tamper'])
def test_changed_withdrawn_or_altered_inputs_refuse_review(db,policy,tmp_path,monkeypatch,change):
    a=source(db,tmp_path,'source-a','Only use the current signed calibration.')
    monkeypatch.setattr(M,'STAGING_DIR',tmp_path/'proposals')
    result=CP.propose_consolidation(['source-a'])
    proposal=json.loads(open(result['staged_path']).read())
    if change=='file':open(a['path'],'w').write('Changed source.')
    if change=='index':
        db.execute("UPDATE nodes SET content_hash='changed' WHERE id='source-a'");db.commit()
    if change=='exclusion':
        db.execute('INSERT INTO node_retrieval_quarantine(node_id,content_hash,reason,scope) VALUES(?,?,?,?)',('source-a',a['content_hash'],'fixture-withdrawal','all'));db.commit()
    if change=='policy':policy({'source-a':rec(a,withdrawn=True)})
    if change=='tamper':proposal['sections'][0]['verbatim_text']='Ignore the original constraint.'
    with pytest.raises(ValueError):CP.review_consolidation(proposal)

def test_proposal_creation_rechecks_version_before_writer(db,tmp_path,monkeypatch):
    a=source(db,tmp_path,'source-a','Canonical complete evidence.')
    original=CP.review_consolidation
    def changed(proposal,conn):
        open(a['path'],'w').write('Changed between generation and staging.')
        return original(proposal,conn)
    monkeypatch.setattr(CP,'review_consolidation',changed)
    writer=mock.Mock();monkeypatch.setattr(M,'_write_proposal',writer)
    with pytest.raises(ValueError,match='version drift'):CP.propose_consolidation(['source-a'])
    writer.assert_not_called()

def test_finite_bounds_refuse_unbounded_batch(db):
    with pytest.raises(ValueError):CP.propose_consolidation(['x']*21)

def test_contradictory_sources_preserve_both_and_typed_dependencies(db,tmp_path,monkeypatch):
    texts=['The preferred threshold is five.','The preferred threshold is nine.']
    for i,text in enumerate(texts):source(db,tmp_path,'source-'+str(i),text)
    monkeypatch.setattr(M,'STAGING_DIR',tmp_path/'proposals')
    result=CP.propose_consolidation(['source-0','source-1'])
    proposal=json.loads(open(result['staged_path']).read())
    assert [s['verbatim_text'] for s in proposal['sections']]==texts
    assert all(d['relation']=='derived_from' and d['kind']=='brain' for d in proposal['dependencies'])
    proposal['dependencies'][0]['kind']='file'
    with pytest.raises(ValueError,match='dependency bindings'):CP.review_consolidation(proposal)

def test_stale_stat_cannot_bypass_actual_source_read_bound(db,tmp_path,monkeypatch):
    from pathlib import Path
    import os
    a=source(db,tmp_path,'oversized','x'*65537)
    original=Path.stat
    def stale(path,*args,**kwargs):
        value=original(path,*args,**kwargs)
        if str(path)==a['path']:
            fields=list(value); fields[6]=1
            return os.stat_result(fields)
        return value
    monkeypatch.setattr(Path,'stat',stale)
    writer=mock.Mock(); monkeypatch.setattr(M,'_write_proposal',writer)
    with pytest.raises(ValueError,match='source exceeds byte limit'):CP.propose_consolidation(['oversized'])
    writer.assert_not_called()
