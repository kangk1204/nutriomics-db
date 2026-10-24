import pytest
from nutri_literature.identity_v7 import choose, ListwiseRanker
from nutri_literature.corpora_v7 import parse_standoff


def test_nil_can_beat_candidate_and_unknown_species_never_defaults_human():
    records=[{'id':'NCBIGene:1','native_id':'1','taxon_id':9606}]
    assert choose(records,[.2,.8],{'eligible':True,'taxon_id':9606},.1,0) is None
    assert choose(records,[.99,.01],{'eligible':False,'taxon_id':None},.9,.1) is None
    assert choose(records,[.99,.01],{'eligible':True,'taxon_id':9606},.9,.1)==records[0]


def test_standoff_preserves_nested_event_roles_and_negation():
    text='A reduces B expression.'
    a1='T1\tSimple_chemical 0 1\tA\nT2\tGene_or_gene_product 10 11\tB'
    a2='T3\tNegative_regulation 2 9\treduces\nT4\tGene_expression 12 22\texpression\nE1\tGene_expression:T4 Theme:T2\nE2\tNegative_regulation:T3 Theme:E1 Cause:T1\nM1\tNegation E2'
    result=parse_standoff(text,a1,a2)
    assert result['events']['E2']['arguments'][0]['target']=='E1'
    assert result['modifications']==[{'type':'Negation','target':'E2'}]
    with pytest.raises(ValueError,match='quotation'):
        parse_standoff(text,a1.replace('0 1','0 2'),a2)


def test_listwise_ranker_learns_candidate_vs_nil():
    pytest.importorskip('numpy')
    pytest.importorskip('scipy')
    pytest.importorskip('sklearn')
    examples=[{'features':[{'exact':1.},{'wrong':1.},{'nil':1.}], 'labels':[1,0]},
              {'features':[{'wrong':1.},{'nil':1.}], 'labels':[0]}]*12
    head=ListwiseRanker().fit(examples)
    a=head.predict(examples[0]['features'])
    b=head.predict(examples[1]['features'])
    assert a[0]>a[1] and a[0]>a[2] and b[-1]>b[0]
