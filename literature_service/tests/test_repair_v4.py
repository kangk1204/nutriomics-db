import json
import pytest
from nutri_literature.repair_v4 import review
from nutri_literature.chatgpt_repair_io import BudgetExceeded

def supplied():
    return {'source':'α drug reduces GENE expression.','mentions':[
        {'key':'m0','candidates':[{'native_id':'D1'}]},
        {'key':'m1','candidates':[{'native_id':'123'}]}],
        'sentences':[{'key':'s0'}]}

def response():
    return {'schema_version':'repair-evidence-v4','mentions':[
        {'key':'m0','eligible':True,'selected_id':'D1','id_evidence':['α drug']},
        {'key':'m1','eligible':True,'selected_id':'123','id_evidence':['GENE']}],
        'events':[{'chemical':'m0','gene':'m1','benchmark_relation_type':'Negative_Correlation',
                   'decision':'supported','evidence':['s0']}]}

def test_exact_unicode_and_coverage():
    assert review.validate(response(),supplied())
    bad=response();bad['mentions'][0]['id_evidence']=['drug GENE']
    with pytest.raises(ValueError,match='id_quote'):review.validate(bad,supplied())
    bad=response();bad['mentions'].pop()
    with pytest.raises(ValueError,match='mention_coverage'):review.validate(bad,supplied())

def test_unknown_is_not_association():
    value=response();value['events'][0]['benchmark_relation_type']=None
    assert review.validate(value,supplied())['events'][0]['benchmark_relation_type'] is None

def test_off_menu_identity_is_quarantined_not_whole_paper_failure():
    value=response();value['mentions'][1]['selected_id']='C105934'
    raw=review.validate(value,supplied())
    assert raw['mentions'][1]['selected_id']=='C105934'
    # It remains raw model data; it has not become an accepted gene ID.
    assert 'C105934' not in {c['native_id'] for c in supplied()['mentions'][1]['candidates']}

def test_original_budget_and_repair_lineage_phase(tmp_path):
    b=review.SharedBudget(tmp_path/'budget.sqlite3')
    for i in range(400):b.reserve(str(i),'repair-v4-train','sol')
    with pytest.raises(BudgetExceeded):b.reserve('new','repair-v4-train','sol')
    # Changed request hashes are allowed only within the separate reserve.
    for i in range(53):b.reserve('repair'+str(i),'repair-v4-repair','luna')
    with pytest.raises(BudgetExceeded):b.reserve('one-more','repair-v4-repair','luna')
    assert b.status()['attempts']==453

def test_one_uses_official_client_and_cached_response(tmp_path,monkeypatch):
    from nutri_literature.contract_v7 import immutable
    from nutri_literature import chatgpt_client
    monkeypatch.setattr(review,'HOME',tmp_path)
    monkeypatch.setattr(review,'PAYLOAD_HOME',tmp_path/'payloads-canonical')
    monkeypatch.setattr(review,'INFERENCE_HOME',tmp_path/'inference-canonical')
    p=supplied();p['source_sha256']='source'
    immutable(tmp_path/'payloads-canonical/train/paper-1.json',p)
    class Auth:
        def status(self):return {'active_registration_id':'profile'}
    class Client:
        calls=0
        def __init__(self,auth,model,registration_id,reasoning_effort):
            assert registration_id=='profile'
        def __call__(self,instructions,input):
            Client.calls+=1
            return {'text':json.dumps(response()),'actual_model':'gpt-6.1-sol','status':'completed',
                    'response_id':'fake-test-response'}
    monkeypatch.setattr(chatgpt_client,'ChatGPTPlanClient',Client)
    b=review.SharedBudget(tmp_path/'ledger.sqlite3')
    assert review.one('1','train','gpt-6.1-sol','medium',Auth(),b)=='completed'
    assert review.one('1','train','gpt-6.1-sol','medium',Auth(),b)=='cached'
    assert Client.calls==1 and b.status()['attempts']==1

def test_four_types_compete_before_supported_mask():
    from nutri_literature.repair_v4.learning import predict_types
    from nutri_literature.repair_v4.representation import TYPES
    model={'classes':list(TYPES),'heads':[{'intercept':0.,'weights':{}} for _ in TYPES]}
    scores=predict_types(model,{})
    assert set(scores)==set(TYPES)
    assert all(v==pytest.approx(.25) for v in scores.values())
    # A sole remaining supported Association must still be .25, not 1.0.
    masked={t:v for t,v in scores.items() if t=='Association'}
    assert masked['Association']==pytest.approx(.25)

def scientific_event(measurement='expression',change='increase',kind='Positive_Correlation',condition='A'):
    return {'key':measurement+change+condition,'chemical':'m0','gene':'m1','origin':'gpt-6.1-sol',
        'native_type':kind,'decision':'supported','measurement':measurement,'change':change,
        'perturbation':'none','observation':'observed','role_direction':'chemical_to_gene',
        'negated':False,'non_significant':False,'speculative':False,'condition':condition,
        'evidence':[{'start':0,'end':4,'text':'test'}],'counterevidence':[],
        'document_level':False,'entity_roles':{}}

def test_event_permutation_and_condition_conjunction():
    from nutri_literature.repair_v4.representation import features
    a=scientific_event()
    b=scientific_event('ubiquitination','decrease','Association','B')
    assert features([a,b])==features([b,a])
    f=features([a,b])
    assert not any('expression|decrease' in k for k in f)

def test_strict_id_counts_boundary_mismatch_as_failure():
    from nutri_literature.repair_v4.representation import strict_ids
    d={'entities':[{'kind':'gene','start':1,'end':5,'native_ids':['1']}]}
    assert strict_ids({'kind':'gene','start':1,'end':5},d)=={'1'}
    assert not strict_ids({'kind':'gene','start':0,'end':5},d)

def test_changed_request_repairs_use_original_reserve(tmp_path,monkeypatch):
    from nutri_literature.contract_v7 import immutable
    from nutri_literature import chatgpt_client
    monkeypatch.setattr(review,'HOME',tmp_path)
    monkeypatch.setattr(review,'PAYLOAD_HOME',tmp_path/'payloads-canonical')
    monkeypatch.setattr(review,'INFERENCE_HOME',tmp_path/'inference-canonical')
    p=supplied();p['source_sha256']='source'
    immutable(tmp_path/'payloads-canonical/train/paper-2.json',p)
    class Auth:
        def status(self):return {'active_registration_id':'profile'}
    class Client:
        calls=0
        def __init__(self,*args,**kwargs):pass
        def __call__(self,instructions,input):
            Client.calls+=1
            return {'text':'{' if Client.calls==1 else json.dumps(response()),'actual_model':'gpt-6.1-sol',
                    'status':'completed','response_id':'test-'+str(Client.calls)}
    monkeypatch.setattr(chatgpt_client,'ChatGPTPlanClient',Client)
    b=review.SharedBudget(tmp_path/'ledger.sqlite3')
    assert review.one('2','train','gpt-6.1-sol','medium',Auth(),b)=='completed'
    with b.connect() as db:
        rows=list(db.execute('select request_hash,phase from attempts order by id'))
    assert [r[1] for r in rows]==['repair-v4-train','repair-v4-repair']
    assert rows[0][0]!=rows[1][0]
    r=json.loads((tmp_path/'inference-canonical/train/gpt-6.1-sol-medium/responses/paper-2-repair1.json').read_text())
    assert r['parent_request_sha256']==rows[0][0]

def test_full_official_aliases_restore_exact_and_expansion_features():
    from nutri_literature.chatgpt_grounding import Catalog
    from nutri_literature.repair_v4.aliases import CanonicalRetriever
    c=Catalog([{'kind':'chemical','native_id':'D123','names':['SC 58635','An extremely long systematic chemical name','SC58635','Celecoxib']}])
    retriever=CanonicalRetriever(c)
    text='Celecoxib (CE) inhibits a protein.'
    direct={'kind':'chemical','text':'Celecoxib','start':0,'end':9,'candidates':[
        {'native_id':'D123','names':['SC 58635','SC58635'],'features':{'exact_alias':0.,'old_id':1.,'historical':1.}}]}
    m=retriever.enrich(direct,text)
    assert m['candidates'][0]['features']['exact_alias']==1.
    assert m['candidates'][0]['names'][0]=='Celecoxib'
    assert 'old_id' not in m['candidates'][0]['features']
    short={**direct,'text':'CE','start':11,'end':13}
    assert retriever.enrich(short,text)['candidates'][0]['features']['expansion']==1.

def test_threshold_sweep_keeps_full_gold_denominator_and_weights(monkeypatch):
    from nutri_literature import evaluation_v6
    from nutri_literature.repair_v4.learning import threshold_sweep
    wanted={('a',('D','G'),'Association'),('a',('D','H'),'Positive_Correlation')}
    monkeypatch.setattr(evaluation_v6,'relation_sets',lambda d,p:(wanted if d['pmid']=='a' else set(),set(),set(),set()))
    papers=[{'pmid':'a','document':{'pmid':'a'},'sample_weight':1.},
            {'pmid':'b','document':{'pmid':'b'},'sample_weight':3.}]
    predictions={'a':{'relations':[{'entity1':'D','entity2':'G','predicate':'Association','priority':.3},
                                  {'entity1':'X','entity2':'G','predicate':'Association','priority':.9}]},
                 'b':{'relations':[{'entity1':'X','entity2':'G','predicate':'Association','priority':.3}]}}
    grid=threshold_sweep(papers,predictions,.1)
    m=next(r for r in grid if r['threshold']==.3)
    assert (m['actual_reviewed_metrics']['tp'],m['actual_reviewed_metrics']['fp'],m['actual_reviewed_metrics']['fn'])==(1,2,1)
    assert m['weighted_estimate']['precision']==pytest.approx(.2)
    assert m['weighted_estimate']['recall']==pytest.approx(.5)

def test_new_menu_profile_makes_new_request_with_parent_not_old_response_reuse(tmp_path,monkeypatch):
    from nutri_literature.contract_v7 import immutable,canonical,sha
    from nutri_literature import chatgpt_client
    monkeypatch.setattr(review,'HOME',tmp_path)
    monkeypatch.setattr(review,'PAYLOAD_HOME',tmp_path/'payloads-canonical')
    monkeypatch.setattr(review,'INFERENCE_HOME',tmp_path/'inference-canonical')
    p=supplied();p['source_sha256']='source'
    immutable(tmp_path/'payloads-canonical/train/paper-3.json',p)
    legacy={'instructions':review.PROMPT,'input':'old incomplete alias menu'}
    immutable(tmp_path/'inference/train/gpt-6.1-sol-medium/requests/paper-3.json',legacy)
    class Auth:
        def status(self):return {'active_registration_id':'profile'}
    class Client:
        def __init__(self,*args,**kwargs):pass
        def __call__(self,instructions,input):
            assert input==canonical(p)
            return {'text':json.dumps(response()),'actual_model':'gpt-6.1-sol','status':'completed','response_id':'fresh-profile-response'}
    monkeypatch.setattr(chatgpt_client,'ChatGPTPlanClient',Client)
    b=review.SharedBudget(tmp_path/'ledger.sqlite3')
    review.one('3','train','gpt-6.1-sol','medium',Auth(),b)
    with b.connect() as db:assert db.execute('select phase from attempts').fetchone()[0]=='repair-v4-repair'
    r=json.loads((tmp_path/'inference-canonical/train/gpt-6.1-sol-medium/responses/paper-3.json').read_text())
    assert r['parent_request_sha256']==sha(canonical(legacy))

def test_literal_type_witness_requires_same_event_measurement_and_source_predicate():
    from nutri_literature.repair_v4.representation import literal_native_witness,model_key
    text='Drug reduces GENE expression.'
    e=scientific_event('expression','decrease','Association')
    e['evidence']=[{'start':0,'end':len(text),'text':text}]
    c={'start':0,'end':4};g={'start':13,'end':17}
    assert literal_native_witness(e,c,g)[0]=='Negative_Correlation'
    e['measurement']='ubiquitination'
    assert literal_native_witness(e,c,g) is None
    e['measurement']='expression';e['negated']=True
    assert literal_native_witness(e,c,g) is None
    assert model_key('source_rule_v4')=='source_rule'
