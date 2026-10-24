import copy,json,types,sys
from pathlib import Path
import pytest
from nutri_literature.repair_v6_gpu import common,text,identity,relations,calibration,semantics

def test_unicode_exact_spans_and_separate_intervals():
 s='α vitamin D lowers GENE. Later β GENE increases.'
 a={'kind':'chemical','start':2,'end':11,'text':s[2:11]};b={'kind':'gene','start':19,'end':23,'text':s[19:23]}
 views=text.pair_views(s,{'mentions':[a]},{'mentions':[b]})
 assert views and all(common.exact(s,v) for v in views[0]['segments'])
 assert '[C]' in views[0]['text'] and '[G]' in views[0]['text']
 assert not common.exact(s,{**a,'start':3})

def test_duplicate_normalized_source_never_crosses_fold():
 ds=[{'pmid':'1','text':'Ａ  Gene'},{'pmid':'2','text':'a gene'},{'pmid':'3','text':'different'}]
 m=common.foldmap(ds,3,'audit');assert m['1']==m['2']

def test_species_unknown_never_defaults_to_human():
 assert text.species('Gene expression in cultured cells.')==[]
 assert text.species('Human-derived cells were compared with mouse cells.')==[9606,10090]

def test_abbreviation_first_character_begins_a_word_and_single_word_definitions_work():
 assert text.definitions('We measured estrogen receptor (ER).')['er']=={'estrogen receptor'}
 assert text.definitions('rapamycin (RAPA) suppressed growth.')['rapa']=={'rapamycin'}
 assert text.definitions('peroxisome proliferator activated receptor gamma (PPARγ) changed.')['pparγ']=={'peroxisome proliferator activated receptor gamma'}

def test_mixed_species_unresolved_mentions_not_merged():
 s='Human and mouse ABC were tested. Human and mouse ABC changed.'
 starts=[s.index('ABC'),s.rindex('ABC')];ms=[{'kind':'gene','text':'ABC','start':a,'end':a+3} for a in starts]
 assert len(text.clusters(ms,s))==2

def test_relation_semantics_does_not_become_negative_due_to_wrong_ID():
 d={'text':'Drug changes GEN','entities':[{'kind':'chemical','start':0,'end':4,'native_ids':['D1']},{'kind':'gene','start':13,'end':16,'native_ids':['1']}],
 'relations':[{'entity1':'D1','entity2':'1','predicate':'Association','subject':'D1'}]}
 a={'kind':'chemical','mentions':[{'start':0,'end':4}],'selected_id':'WRONG'};b={'kind':'gene','mentions':[{'start':13,'end':16}],'selected_id':None}
 assert relations.semantic_types(d,a,b)=={'Association'}
 assert relations.direction_target(d,a,b)==0

def test_ambiguous_partial_gene_span_is_not_semantic_negative():
 d={'entities':[{'kind':'gene','start':0,'end':8,'native_ids':['1']}],'relations':[]}
 a={'kind':'gene','mentions':[{'start':0,'end':3}]};b={'kind':'chemical','mentions':[{'start':10,'end':14}]}
 assert relations.semantic_types(d,b,a) is None

def test_dictionary_restores_aliases_without_treating_every_MeSH_D_as_chemical(monkeypatch,tmp_path):
 import sqlite3
 from nutri_literature.repair_v6_gpu import dictionary_mentions as dm
 monkeypatch.setattr(dm,'HOME',tmp_path);common.atomic(tmp_path/'knowledge/chemical-categories.json',{'chemical_ids':['D1']})
 db=sqlite3.connect(':memory:');db.executescript('CREATE TABLE aliases(norm TEXT,kind TEXT,rid INTEGER);CREATE TABLE records(rid INTEGER,native_id TEXT,names TEXT);')
 rows=[(1,'D1',['vitamin D'],'chemical'),(2,'D2',['depression'],'chemical'),(3,'9',['CAT'],'gene')]
 for rid,native,names,kind in rows:
  db.execute('INSERT INTO records VALUES(?,?,?)',(rid,native,json.dumps(names)));db.execute('INSERT INTO aliases VALUES(?,?,?)',(dm.namekey(names[0]),kind,rid))
 kb=types.SimpleNamespace(base=db,overlay=db);s='CAT and cat respond to vitamin D in depression.';v=dm.extract(s,kb)
 assert any(m['text']=='vitamin D' for m in v)
 assert any(m['text']=='CAT' for m in v) and not any(m['text']=='cat' for m in v)
 assert not any(m['text']=='depression' for m in v)
 assert all(common.exact(s,m) and m['ner_score']==0. for m in v)

def test_small_ID_sample_cannot_pass_precision_gate():
 rows=[{'proposed_id':'1','previous_id':None,'priority':.99,'margin':4.,'strict_correct':True} for _ in range(10)]
 assert not identity.choose_policy(rows)['new']['enabled']
 assert common.wilson(10,10)<.9
 assert identity.choose_policy(rows*5)['new']['enabled']

def test_ID_abstention_preserves_previous_connection():
 disabled={'new':{'enabled':False},'change':{'enabled':False}}
 row={'previous_id':'OLD','proposed_id':'NEW','priority':.99,'margin':10.}
 assert identity.select(row,disabled)==('OLD','retained')
 assert identity.select({**row,'previous_id':None},disabled)==(None,'NIL')

def test_NIL_and_ID_numbers_are_not_candidate_text_features():
 from nutri_literature.repair_v6_gpu.knowledge import Knowledge
 kb=object.__new__(Knowledge);value=kb.candidate_text({'kind':'gene','taxon':9606,'native_id':'123456789','names':['example gene'],'description':'protein kinase'})
 assert '123456789' not in value and 'Homo sapiens' in value

def test_partial_entity_annotations_mask_other_head(monkeypatch):
 class Tokenizer:
  def __call__(self,*args,**kwargs):return {'input_ids':[[1,2,3]],'attention_mask':[[1,1,1]],'offset_mapping':[[(0,0),(0,3),(0,0)]]}
 monkeypatch.setitem(sys.modules,'nutri_literature.repair_v6_gpu.neural',types.SimpleNamespace(tokenizer=lambda:Tokenizer()))
 from nutri_literature.repair_v6_gpu.entities import rows
 d={'pmid':'x','text':'ABC','gene_annotation_complete':True,'entities':[{'kind':'gene','start':0,'end':3}]}
 labels=rows([d])[0]['labels'];assert labels[1]==[1,-100] and labels[0]==[-100,-100]
 d.update(gene_annotation_complete=False,entities=[{'kind':'chemical','start':0,'end':3}]);assert rows([d])[0]['labels'][1]==[-100,1]

def test_acceptance_consumes_both_semantic_models_and_ID_features():
 row={'views':[{'text':'Drug changes gene','same_sentence':True}],'id_features':[.4,.5,1.,1.,.8,.9],'identity_unresolved':False}
 doc={'exists':.5,'types':[.4]*8};nli={'exists':.6,'types':[.4]*8};f=calibration.feature(row,doc,nli,0)
 model={'coef':[1.]*len(f),'intercept':-10}
 a=calibration.priority(row,{'document':doc,'nli':nli},model,0)
 assert calibration.priority(row,{'document':{**doc,'exists':.9},'nli':nli},model,0)>a
 assert calibration.priority(row,{'document':doc,'nli':{**nli,'exists':.9}},model,0)>a
 assert calibration.priority({**row,'id_features':[1.]*6},{'document':doc,'nli':nli},model,0)>a

def test_event_order_independent_primary_type_and_strict_deduplication():
 row={'pmid':'1','ids':['D1','2']};a={'row':row,'predicate':'Association','priority':.7};b={'row':row,'predicate':'Bind','priority':.8}
 assert calibration.predicted([a,b],.5)==calibration.predicted([b,a],.5)=={('1','D1','2','Bind')}

def test_unknown_type_not_silently_converted_to_association():
 row={'pmid':'1','ids':[None,'2']};assert not calibration.predicted([{'row':row,'predicate':'Association','priority':.99}],.05)

def test_plan_never_marked_observed_and_scope_is_not_fabricated():
 s='We will measure gene expression after treatment.';m={'start':0,'end':len(s),'text':s}
 r=semantics.context([{'segments':[m],'endpoints':[]}])[0]
 assert r['planned_cue_present'] and not r['observed_result_confirmed'] and not r['auto_approved_evidence']

def test_immutable_results_cannot_be_overwritten(tmp_path):
 p=tmp_path/'r.json';common.atomic(p,{'v':1},True)
 with pytest.raises(ValueError):common.atomic(p,{'v':2},True)
 assert json.loads(p.read_text())=={'v':1}

def test_runtime_budget_cannot_be_reset_on_resume(tmp_path):
 b=common.RuntimeBudget(tmp_path);b.start('one');b.stop();v=common.read(tmp_path/'gpu-budget.json');v['used_seconds']=v['limit_seconds'];common.atomic(tmp_path/'gpu-budget.json',v)
 with pytest.raises(TimeoutError):common.RuntimeBudget(tmp_path).start('two')

def test_duplicate_prediction_manifest_cannot_open_scoring(monkeypatch,tmp_path):
 from nutri_literature.repair_v6_gpu import evaluation
 monkeypatch.setattr(evaluation,'HOME',tmp_path)
 docs=[{'pmid':str(i),'text':'Source'} for i in range(100)]
 common.atomic(tmp_path/'data/development-sources.json',{'documents':docs})
 common.atomic(tmp_path/'predictions/manifest.json',{'complete_before_scoring':True,'papers':100,'files':[{'path':'duplicate.json'}]*100})
 with pytest.raises(ValueError,match='누락/중복'):evaluation.verify_predictions()

def test_no_explicit_species_prevents_new_automatic_gene_ID():
 enabled={'new':{'enabled':True,'threshold':.5,'margin':0},'change':{'enabled':True,'threshold':.5,'margin':0}}
 row={'previous_id':None,'proposed_id':'HUMAN','priority':.99,'margin':10.,'species_evidence_compatible':False}
 assert identity.select(row,enabled)==(None,'NIL')

def test_unannotated_entity_type_is_not_a_false_ID_for_selection():
 d={'entities':[],'relation_annotation_complete':False,'gene_annotation_complete':True}
 assert identity.annotation_scope(d,'gene') and not identity.annotation_scope(d,'chemical')
 correct=[{'proposed_id':'1','previous_id':None,'priority':.99,'margin':4.,'strict_correct':True,'annotation_scope_complete':True} for _ in range(50)]
 unknown=[{**correct[0],'strict_correct':None,'annotation_scope_complete':False} for _ in range(500)]
 assert identity.choose_policy(correct+unknown)['new']['enabled']

def test_native_type_and_ID_scoring_matches_legacy_scorer_on_training_only():
 from nutri_literature.repair_v6_gpu.common import HOME,read
 p=HOME/'data/relations-train.json'
 if not p.exists():pytest.skip('local audited training cohort not installed')
 from nutri_literature.evaluation_v6 import relation_sets
 docs=read(p)['documents'];ours=calibration.truth(docs);old=set()
 for d in docs:
  native=copy.deepcopy(d)
  for e in native['entities']:
   if e['kind']=='chemical':e['kind']='component'
  old|=relation_sets(native,{'relations':[]})[0]
 assert {(p,tuple(sorted((a,b))),t) for p,a,b,t in ours}==old
 assert len(ours)==549
