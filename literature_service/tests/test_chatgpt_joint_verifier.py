import pytest

from nutri_literature.chatgpt_joint_verifier import project
from nutri_literature.contract_v7 import canonical

TEXT='Human aspirin inhibits TP53, but other pathways may not respond.'
CONFIG={'confidence':0,'species_conflict_gate':True,'type_cue_gate':False}


def payload(taxon=9606):
    return {'source':TEXT,'sentences':[{'key':'s0','start':0,'end':len(TEXT),'text':TEXT}],
            'proposals':[], 'mentions':[
                {'key':'u0','kind':'chemical','text':'aspirin','start':6,'end':13,
                 'candidates':[{'native_id':'D001241','taxon':None}]},
                {'key':'u1','kind':'gene','text':'TP53','start':23,'end':27,
                 'candidates':[{'native_id':'7157','taxon':taxon}]}]}


def response(**changes):
    r={'chemical':'u0','gene':'u1','chemical_id':'D001241','gene_id':'7157',
       'benchmark_relation_type':'Negative_Correlation','role_direction':'chemical_to_gene',
       'evidence':['s0'],'confidence':'high','decision':'accept','observation':'observed',
       'negated':False,'speculative':False}
    r.update(changes)
    return canonical({'schema_version':'joint-source-verification-v1','relations':[r]})


def test_reviewer_can_add_source_supported_pair_missing_from_proposals():
    result=project(payload(),response(),CONFIG)
    assert len(result['relations'])==1
    assert result['relations'][0]['subject']=='D001241'
    assert result['relations'][0]['review_status']=='unreviewed'


@pytest.mark.parametrize('changes,reason',[
    ({'gene_id':'999'},'gene_id_outside_official_menu'),
    ({'evidence':['s999']},'evidence_invalid'),
    ({'chemical':{}},'endpoint_invalid'),
    ({'confidence':[]},'below_training_selected_priority'),
    ({'decision':'uncertain'},'reviewer_rejected_or_uncertain'),
])
def test_reviewer_failure_states_remain_auditable(changes,reason):
    result=project(payload(),response(**changes),CONFIG)
    assert not result['relations']
    assert reason in result['decisions'][0]['reasons']


def test_species_conflict_is_not_assumed_human_by_default():
    result=project(payload(10090),response(),CONFIG)
    assert not result['relations']
    assert 'explicit_species_conflict' in result['decisions'][0]['reasons']


def test_bad_json_is_not_repaired():
    assert project(payload(),'[broken',CONFIG)['parse_failure']
