import json

import pytest

from nutri_literature.chatgpt_grounding import Catalog, mapped_key, source_payload, select
from nutri_literature.chatgpt_tuning import project, score_rows, criterion
from nutri_literature.contract_v7 import canonical
from nutri_literature.text import sentence_spans


@pytest.fixture
def catalog():
    return Catalog([
        {'kind':'gene','native_id':'7157','taxon':9606,'names':['TP53','tumor protein p53']},
        {'kind':'gene','native_id':'22059','taxon':10090,'names':['Tp53','p53']},
        {'kind':'chemical','native_id':'D001241','names':['aspirin']},
        {'kind':'chemical','native_id':'D014807','names':['vitamin D']},
        {'kind':'chemical','native_id':'D002762','names':['cholecalciferol','vitamin D3']},
        {'kind':'chemical','native_id':'D002117','names':['calcitriol']},
    ])


MODEL = {'intercept':0.,'weights':{'exact_case':3.,'local_tax_match':3.,'local_tax_conflict':-5.},
         'threshold':.5,'margin':.05}
CONFIG = {'id_policy':'model_menu','gate':'anchored','minimum_confidence':0}
TEXT = 'In human cells, aspirin inhibited TP53.'


def payload(text=TEXT):
    sentences = [{'key':'s'+str(i),**s} for i,s in enumerate(sentence_spans(text))]
    mentions=[]
    for identifier,kind,term,native,taxon in [('m0','chemical','aspirin','D001241',None),('m1','gene','TP53','7157',9606)]:
        start=text.index(term)
        mentions.append({'key':identifier,'kind':kind,'text':term,'start':start,'end':start+len(term),
                         'selected_id':native,'candidates':[{'native_id':native,'taxon':taxon,'priority':.9}]})
    return {'source':text,'sentences':sentences,'mentions':mentions}


def response(**changes):
    relation={'chemical':'m0','gene':'m1','chemical_id':'D001241','gene_id':'7157',
              'benchmark_relation_type':'Negative_Correlation','role_direction':'chemical_to_gene',
              'evidence':['s0'],'confidence':'high','negated':False,'speculative':False,'observation':'observed'}
    relation.update(changes)
    return canonical({'schema_version':'literature-grounded-relations-v1','additional_mentions':[],'relations':[relation]})


def test_unicode_casefold_expansion_maps_to_original():
    value, offsets=mapped_key('  Straße\tTP53')
    assert value=='strasse tp53'
    start,end=offsets[0][0],offsets[6][1]
    assert '  Straße\tTP53'[start:end]=='Straße'


def test_dictionary_does_not_merge_family_and_metabolites(catalog):
    for term,expected in [('vitamin D','D014807'),('vitamin D3','D002762'),('calcitriol','D002117')]:
        m={'kind':'chemical','text':term,'start':0,'end':len(term)}
        rows=catalog.candidates(m,term)
        assert {r['native_id'] for r in rows}=={expected}


def test_species_features_do_not_default_to_human(catalog):
    m={'kind':'gene','text':'p53','start':0,'end':3}
    rows=catalog.candidates(m,'p53 protein expression.')
    assert rows[0]['features']['taxon_unknown']==1
    assert rows[0]['features']['local_tax_match']==0
    assert not any('7157' in k or '22059' in k for k in rows[0]['features'])


def test_description_overlap_is_not_dominated_by_shared_surface_alias():
    index=Catalog([{'kind':'gene','native_id':'1','taxon':9606,'names':['MAP','mitogen activated kinase']},
                   {'kind':'gene','native_id':'2','taxon':9606,'names':['MAP','microtubule associated protein']}])
    rows=index.candidates({'kind':'gene','text':'MAP','start':0,'end':3},'MAP kinase phosphorylation increased.')
    by_id={r['native_id']:r for r in rows}
    assert by_id['1']['features']['name_overlap']>by_id['2']['features']['name_overlap']
    assert 'mitogen activated kinase' in by_id['1']['names']


def test_source_payload_never_forwards_annotations(catalog):
    document={'pmid':'fixture','text':TEXT,'entities':[{'text':'GOLD_ONLY_POISON'}],
              'relations':[{'predicate':'GOLD_ONLY_POISON'}]}
    result=source_payload(document,catalog,{'model':MODEL})
    assert 'GOLD_ONLY_POISON' not in canonical(result)
    assert all(TEXT[m['start']:m['end']]==m['text'] for m in result['mentions'])


def test_strong_evidence_and_direction_preserved(catalog):
    result=project(payload(),response(),catalog,MODEL,CONFIG)
    assert result['relations'][0]['subject']=='D001241'
    assert result['relations'][0]['entity2']=='7157'
    assert result['relations'][0]['evidence'][0]['text']==TEXT


def test_uncertainty_in_other_clause_is_not_sentence_veto(catalog):
    text='In human cells, aspirin inhibited TP53, but another pathway may not respond.'
    result=project(payload(text),response(speculative=False),catalog,MODEL,{**CONFIG,'gate':'scoped'})
    assert len(result['relations'])==1


def test_planning_is_distinct_from_observed(catalog):
    result=project(payload(),response(observation='planned'),catalog,MODEL,{**CONFIG,'gate':'scoped'})
    assert not result['relations']
    assert 'planned_only' in result['decisions'][0]['reasons']


@pytest.mark.parametrize('changes,reason',[
    ({'evidence':['s999']},'invalid_evidence'),
    ({'benchmark_relation_type':'UnknownType'},'unsupported_type'),
    ({'chemical':'missing'},'endpoint_missing_or_wrong_type'),
])
def test_invalid_candidates_cannot_become_verified(catalog,changes,reason):
    result=project(payload(),response(**changes),catalog,MODEL,CONFIG)
    assert not result['relations']
    assert reason in result['decisions'][0]['reasons']


def test_id_candidate_threshold_margin_retains_nil(catalog):
    assert select([{'native_id':'7157','features':{}},{'native_id':'22059','features':{}}],MODEL) is None
    data=payload()
    data['mentions'][1]['selected_id']=None
    data['mentions'][1]['candidates']=[]
    result=project(data,response(gene_id='invented'),catalog,MODEL,CONFIG)
    assert not result['relations']
    assert 'gene_id_unresolved' in result['decisions'][0]['reasons']


def test_rejection_keeps_gold_denominator_and_undefined_precision(catalog):
    document={'pmid':'fixture','entities':[{'kind':'component','native_ids':['D001241']},
                                           {'kind':'gene','native_ids':['7157']}],
              'relations':[{'entity1':'D001241','entity2':'7157','predicate':'Negative_Correlation'}]}
    rows=score_rows([document],{'fixture':{'relations':[]}})
    assert rows['fixture']['relation']['fn']==1
    assert rows['fixture']['relation']['precision'] is None
    assert not criterion(rows['fixture']['relation'])[0]


def test_malformed_json_is_failure_not_repair(catalog):
    assert project(payload(),'```json {} ```',catalog,MODEL,CONFIG)['parse_failure']
    assert project(payload(),json.dumps({'relations':[]}),catalog,MODEL,CONFIG)['parse_failure']
