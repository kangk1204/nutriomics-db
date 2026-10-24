"""Meaning/source regressions; fixtures do not establish scientific accuracy."""
import hashlib
import importlib.util
import json

import pytest

from nutri_literature.chemical_v3 import augment_mentions, load_snapshot
from nutri_literature.dictionaries import _insert
from nutri_literature.extraction_v2 import RULES_VERSION as V2_VERSION
from nutri_literature.grammar_v3 import scopes
from scripts.run_development_v3 import select_documents


def test_frozen_v2_unchanged():
    assert V2_VERSION=='rules-sha256:11b4f00cf76a1b56b5cc273bd150474e021b4ba400f274b8b168f6f9486eae66'


def test_protected_test_rejected():
    with pytest.raises(ValueError,match='Protected'):
        select_documents({'split':'test','documents':[]},10)


def test_development_sample_ignores_document_order():
    documents=[{'pmid':str(i)} for i in range(30)]
    a=select_documents({'split':'development','documents':documents},10)
    b=select_documents({'split':'development','documents':list(reversed(documents))},10)
    assert a==b and len(a)==10


def test_nutrition_preview_never_selects_heldout():
    from scripts.preview_nutrition_v3 import select_development_preview
    values=[{'pmid':str(i),'split':'development' if i%2 else 'heldout','primary_topic':str(i%3)} for i in range(40)]
    selected=select_development_preview(values,12)
    assert len(selected)==12
    assert {d['primary_topic'] for d in selected}=={'0','1','2'}
    assert all(d['split']=='development' for d in selected)


def test_native_mesh_identity_not_chebi_equivalence():
    trie={}
    _insert(trie,'caffeine',{'id':'MESH:D002110'})
    legacy={'kind':'nutrient','start':0,'end':8,'text':'caffeine','normalized_id':'CHEBI:27732'}
    new=augment_mentions('caffeine',[],[legacy],{'trie':trie,'manifest':{'source':'official','snapshot_sha256':'fixture'}})
    assert new[0]['normalized_id']=='MESH:D002110'
    assert new[0]['payload']['prior_dictionary_mentions']==[legacy]
    assert legacy['normalized_id']=='CHEBI:27732'


def test_gene_overlap_not_relabelled_as_chemical():
    trie={}
    _insert(trie,'vitamin D receptor',{'id':'MESH:D-fixture'})
    gene={'kind':'gene','text':'vitamin D receptor','start':0,'end':18,'normalized_id':'HGNC:12679'}
    assert augment_mentions(gene['text'],[],[gene],{'trie':trie})==[gene]


def test_ambiguous_alias_abstains():
    trie={}
    for identity in ['MESH:D1','MESH:D2']:
        _insert(trie,'drugname',{'id':identity})
    assert augment_mentions('drugname',[],[],{'trie':trie})==[]


def test_dictionary_hash_tampering_rejected(tmp_path):
    path=tmp_path/'lexicon.json'
    path.write_text(json.dumps({'manifest':{'schema_version':'literature-mesh-chemicals-v3'},'records':[]}),encoding='utf-8')
    sha=hashlib.sha256(path.read_bytes()).hexdigest()
    load_snapshot(path,sha)
    path.write_text('{}')
    with pytest.raises(ValueError,match='hash mismatch'):
        load_snapshot(path,sha)


def test_negation_scope_starts_at_cue_and_stops_at_contrast():
    text='A did not reduce B but C increased D.'
    words=['A','did','not','reduce','B','but','C','increased','D','.']
    tokens=[]
    cursor=0
    for i,word in enumerate(words):
        start=text.index(word,cursor)
        cursor=start+len(word)
        tokens.append({'i':i,'text':word,'start':start,'end':cursor,'head':3,'dep':'dep','pos':'NOUN'})
    tokens[2].update(dep='neg')
    tokens[3].update(head=3,dep='ROOT',pos='VERB')
    tokens[7].update(head=3,dep='conj',pos='VERB')
    tokens[6]['head']=7
    tokens[8]['head']=7
    spans=scopes({'text':text,'start':0,'end':len(text),'label':'Results'},tokens)
    assert spans[0]['cue_start']==6
    assert spans[0]['text']=='not reduce B'


@pytest.mark.parametrize('section,phrase,expected',[
    ('Methods','blood pressure','planned_outcomes'),
    ('Results','blood pressure','outcomes'),
    ('Results','gene expression','unresolved_measurement'),
    ('Results','the samples','unresolved_measurement'),
])
def test_outcome_phase_and_clinical_measure_are_distinct(section,phrase,expected):
    from nutri_literature.pico_v3 import sentence_spans
    line='We measured '+phrase+'.'
    start=line.index(phrase)
    tokens=[{'i':0,'text':'measured','lemma':'measure','pos':'VERB','dep':'ROOT','head':0,'start':3,'end':11},
            {'i':1,'text':phrase,'lemma':phrase,'pos':'NOUN','dep':'dobj','head':0,'start':start,'end':start+len(phrase)}]
    values=sentence_spans({'text':line,'start':0,'end':len(line),'label':section},tokens)
    assert values[0]['label']==expected
    assert line[values[0]['start']:values[0]['end']]==values[0]['text']


@pytest.mark.skipif(importlib.util.find_spec('en_core_sci_sm') is None,reason='Actual pinned CPU parser not installed')
@pytest.mark.parametrize('line,expected',[
    ('Inhibition of VDR by caffeine was measured in human cells.',('CHEBI:27732','HGNC:12679','decrease')),
    ('Caffeine reduced VDR expression in human cells.',('CHEBI:27732','HGNC:12679','decrease')),
    ('VDR expression was reduced by caffeine in human cells.',('CHEBI:27732','HGNC:12679','decrease')),
])
def test_actual_cpu_noun_and_verb_roles(line,expected):
    from nutri_literature.dictionaries import Dictionaries
    from nutri_literature.extraction_v3 import extract
    genes,entities={},{}
    gene={'id':'HGNC:12679','symbol':'VDR','name':'vitamin D receptor','aliases':[]}
    _insert(genes,'VDR',{**gene,'term':'VDR','form_kind':'symbol'})
    _insert(entities,'caffeine',{'id':'CHEBI:27732','kind':'nutrient','term':'caffeine','label':'caffeine'})
    dictionary=Dictionaries('fixture',{'source':'synthetic software fixture'},genes,entities)
    result=extract(line,[],dictionary)
    candidates=[e for e in result['events'] if e['status']=='candidate']
    assert any((e['subject'],e['object'],e['direction'])==expected for e in candidates),result['abstentions']
    assert result['engine_version']=='heuristic-v3'
    assert result['manifest']['experimental']
    for value in result['benchmark_scopes']+result['annotated_spans']:
        assert line[value['start']:value['end']]==value['text']
