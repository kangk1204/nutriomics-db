from nutri_literature.chatgpt_extraction import project, SCHEMA
from nutri_literature.chatgpt_id_ablation import restore_ids
from nutri_literature.chatgpt_grounding import Catalog
from nutri_literature.contract_v7 import canonical

TEXT='Human aspirin inhibits TP53.'
MODEL={'intercept':3.,'weights':{},'threshold':.5,'margin':.05}


def original(gene_id=None):
    return project(TEXT,canonical({'schema_version':SCHEMA,'entities':[
        {'key':'c','kind':'chemical','text':'aspirin','native_id':'D001241'},
        {'key':'g','kind':'gene','text':'TP53','native_id':gene_id}],
        'relations':[{'chemical':'c','gene':'g','benchmark_relation_type':'Negative_Correlation',
                      'role_direction':'chemical_to_gene','evidence':{'quote':TEXT}}]}))['chatgpt_only']


def catalog():
    return Catalog([{'kind':'gene','native_id':'7157','taxon':9606,'names':['TP53']},
                    {'kind':'chemical','native_id':'D001241','names':['aspirin']}])


def test_missing_id_can_create_a_true_native_pair_without_changing_relation():
    before=original()
    after=restore_ids(TEXT,before,catalog(),MODEL,'fill_missing')
    assert after['relations'][0]['entity2']=='7157'
    assert after['relations'][0]['predicate']==before['relations'][0]['predicate']
    assert after['relations'][0]['subject']=='D001241'
    assert before['relations'][0]['entity2'].startswith('UNRESOLVED')


def test_existing_id_change_is_a_separate_ablation():
    before=original('999')
    assert restore_ids(TEXT,before,catalog(),MODEL,'fill_missing')['relations'][0]['entity2']=='999'
    after=restore_ids(TEXT,before,catalog(),MODEL,'revalidate_all')
    assert after['relations'][0]['entity2']=='7157'
    assert any(c['action']=='changed_existing' for c in after['id_changes'])


def test_unresolved_placeholder_deduplication_is_preserved():
    before=original()
    after=restore_ids(TEXT,before,Catalog([]),MODEL,'fill_missing')
    assert after['relations']==before['relations']


def test_unaligned_entity_does_not_receive_a_guessed_id():
    before=original()
    before['relations'][0]['endpoints']['gene']['text']='not in source'
    after=restore_ids(TEXT,before,catalog(),MODEL,'fill_missing')
    assert after['relations'][0]['entity2'].startswith('UNRESOLVED')
