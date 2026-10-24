from scripts.analyze_chatgpt_repair_v4 import error_class,first_gap
from nutri_literature import evaluation_v6

def doc():
    return {'pmid':'p','entities':[
        {'kind':'component','start':0,'end':4,'native_ids':['D1']},
        {'kind':'gene','start':8,'end':12,'native_ids':['123']} ]}

def test_annotation_absence_is_not_biological_adjudication(monkeypatch):
    truth={('p',('123','D1'),'Positive_Correlation')}
    monkeypatch.setattr(evaluation_v6,'relation_sets',lambda d,p:(truth,set(),set(),set()))
    assert error_class(doc(),('p',('123','D1'),'Association'))=='relation_type_mismatch'
    assert error_class(doc(),('p',('123','D2'),'Association'))=='chemical_id_not_annotated'
    assert error_class(doc(),('p',('124','D1'),'Association'))=='gene_id_not_annotated'

def test_first_gap_preserves_all_gold_denominator():
    key=('p',('123','D1'),'Positive_Correlation')
    paper={'document':doc(),'mentions':[]}
    assert first_gap(key,paper,{},[],{key})=='matched'
    assert first_gap(key,paper,{},[],set())=='chemical_exact_boundary_not_detected'
