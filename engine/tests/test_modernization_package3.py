"""Finite context retention contracts; semantic quality is measured separately."""
import json
import pytest
import learning_core as LC
from engine import mcp_server as M, brain as B
from engine.tests.test_modernization_package2 import capsule

def test_preamble_and_whole_critical_conditions_survive():
    preamble='Use this procedure only with an active grant.'
    section='### Standard\n/Knowledge/Standards/rule.md\nApprove only if calibration is current; otherwise refuse.'
    r=LC.build_decision_pack_result(preamble+'\n'+section)
    assert r['critical_complete'] and not r['overflow']
    assert preamble in r['text'] and section in r['text']
    assert len(r['text'])<=2600

def test_critical_overflow_is_explicit_no_partial_conditions():
    source='### Standard\n/Knowledge/Standards/rule.md\n'+('condition '*500)+'unless approval has expired.'
    r=LC.build_decision_pack_result(source)
    assert r['overflow'] and not r['critical_complete'] and r['status']=='overflow'
    assert 'Do not act' in r['text'] and 'condition condition' not in r['text']
    assert r['omitted_sections'][0]['reason']=='critical_overflow'
    rendered=LC.render_harness_context(source,'apply','hermes',0)
    assert 'status="overflow"' in rendered and 'status="hit"' not in rendered

def test_optional_content_whole_when_fits_and_visible_when_omitted():
    section='### Copper history\nThe research recorded three independent batches.'
    r=LC.build_decision_pack_result(section,prompt='copper history')
    assert section in r['text'] and r['omitted_sections']==[]
    huge='\n'.join('### Topic '+str(i)+'\n'+'neutral descriptive material '*40 for i in range(10))
    r=LC.build_decision_pack_result(huge)
    assert not r['overflow'] and r['omitted_sections']
    assert '<pack-coverage' in r['text'] and len(r['text'])<=2600

def test_prompt_prioritizes_optional_whole_sections_without_inventing_mandates():
    filler='### Earlier unrelated\n'+'distant description '*100
    relevant='### Copper evidence\nCalibration observations recorded five samples.'
    r=LC.build_decision_pack_result(filler+'\n'+relevant,prompt='copper evidence')
    assert relevant in r['text'] and not r['overflow']
    assert 'Earlier unrelated' in r['text'] or r['omitted_sections']

def test_estimated_tokens_and_no_claim_of_universal_savings():
    raw='### Note\nA tiny fact.'
    r=LC.build_decision_pack_result(raw)
    assert r['output_tokens_est']>r['input_tokens_est']
    assert r['output_tokens_est']==(len(r['text'].encode())*2+6)//7

@pytest.mark.parametrize('field,value',[('constraints',['x'*1001]),('constraints',['x']*51),('goal','x'*4001),('next_action','x'*2001),('active_context_hashes',{'x':'y'*1001})])
def test_capsule_overflow_refuses_instead_of_silent_truncation(capsule,field,value):
    with pytest.raises(ValueError,match='refusing truncation'):
        capsule.save_capsule({field:value},'session','ALTAIR')
    assert not capsule.capsule_path('session').exists()

def test_structured_mcp_overflow_is_parseable_typed_no_preview(monkeypatch):
    monkeypatch.setattr(M,'RESPONSE_CAP_BYTES',400)
    monkeypatch.setattr(B,'query',lambda **_:B.ContextPack('topic',text_bundle='large '*1000))
    result=M.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'brain_query','arguments':{'topic':'topic','format':'json'}}})
    value=json.loads(result['result']['content'][0]['text'])
    assert value['error']=='response_overflow' and value['status']=='error'
    assert '_preview' not in value and 'sources' not in value

@pytest.mark.parametrize('marker',['[Policy|document]','[Law|governance]','[Commandment|governance]','[Rule|document]','[Governance|document]','mandatory: true'])
def test_explicit_governance_type_is_critical_without_keyword(capsule,marker):
    section='### Conduct '+marker+'\n'+('Records remain canonical. '*120)
    result=LC.build_decision_pack_result(section)
    assert result['overflow'] and result['omitted_sections'][0]['reason']=='critical_overflow'
    assert 'unknown prose' in result['critical_classification']
