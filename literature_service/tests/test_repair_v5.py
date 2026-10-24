import copy
from nutri_literature.repair_v5.retrieval import Search,normal,variants

def test_full_alias_and_word_order(tmp_path):
    s=Search(tmp_path/'aliases.db');s.create()
    s.add({'kind':'gene','native_id':'1','taxon':9606,'names':['ABC','histamine receptor H3','an alias beyond the first display name']})
    s.db.commit()
    assert s.search('gene','histamine H(3)-receptors',())
    assert s.search('gene','an alias beyond the first display name',())
    assert not s.search('chemical','ABC',())
    s.close()

def test_empty_grams_do_not_hide_known(tmp_path):
    s=Search(tmp_path/'aliases.db');s.create();s.add({'kind':'chemical','native_id':'D1','names':['nitroindazole-7'],'taxon':None});s.db.commit()
    assert s.search('chemical','nitroindazolexyz',())
    s.close()

def test_orthography_is_retrieval_only():
    assert normal('H(3)-receptors')=='h3 receptor'
    assert variants('α receptor') & variants('alpha receptor')

def test_budget_original_global_and_phase_caps(tmp_path):
    from nutri_literature.repair_v5.review import Budget
    from nutri_literature.chatgpt_repair_io import BudgetExceeded
    import pytest
    b=Budget(tmp_path/'ledger.db')
    with b.connect() as db:
        db.executemany('INSERT INTO attempts(request_hash,phase,model,state,started) VALUES(?,?,?,?,?)',
            [(str(i),'old','test','completed','old') for i in range(2975)])
    for i in range(16):b.reserve('train'+str(i),'repair-v5-train','test')
    for i in range(6):b.reserve('dev'+str(i),'repair-v5-development','test')
    for i in range(3):b.reserve('train0','repair-v5-train','test')
    assert b.status()['attempts']==3000
    with pytest.raises(BudgetExceeded):b.reserve('no-more','repair-v5-retry','test')

def test_no_witness_cannot_create_new_type():
    from nutri_literature.repair_v5.learning import choose_rows
    anchor={'ids':['C1','1'],'native':'Association','anchor':True,'priority':.7,'source_witness':False}
    unsupported={**anchor,'native':'Negative_Correlation','anchor':False,'priority':.99}
    policy={'anchor_threshold':0.,'new_threshold':.5,'relabel_margin':.1}
    assert choose_rows([anchor,unsupported],policy)==[anchor]

def test_supported_relabel_and_order_invariant():
    from nutri_literature.repair_v5.learning import choose_rows
    a={'ids':['C1','1'],'native':'Association','anchor':True,'priority':.7,'source_witness':False}
    b={**a,'native':'Negative_Correlation','anchor':False,'priority':.95,'source_witness':True}
    policy={'anchor_threshold':0.,'new_threshold':.5,'relabel_margin':.1}
    assert choose_rows([a,b],policy)==choose_rows([b,a],policy)==[b]

def test_anchor_survives_nil_without_fresh_events(monkeypatch):
    from nutri_literature.repair_v5 import representation as rep
    source='Drug reduces GENE.'
    c={'key':'c','kind':'chemical','start':0,'end':4,'text':'Drug','old_ids':['C1'],'candidates':[{'native_id':'C1','features':{}}],'reviews':{}}
    g={'key':'g','kind':'gene','start':13,'end':17,'text':'GENE','old_ids':['1'],'candidates':[{'native_id':'1','features':{}}],'reviews':{}}
    endpoints=[{k:m[k] for k in ('kind','start','end','text')} for m in (c,g)]
    anchor={'entity1':'C1','entity2':'1','predicate':'Negative_Correlation','endpoints':endpoints,'evidence':[{'start':0,'end':len(source),'text':source}]}
    monkeypatch.setattr(rep,'anchor_rows',lambda p,policy:[anchor])
    monkeypatch.setattr(rep.oldlearn,'choose',lambda *a,**kw:{'selected_id':None,'proposed_id':None,'selection_reason':'nil'})
    p={'pmid':'test','document':{'text':source},'mentions':[c,g],'events':[]}
    rows=rep.candidates(p,{},{});assert len(rows)==1 and rows[0]['anchor'] and rows[0]['ids']==['C1','1']

def test_ids_and_pmid_not_learning_features():
    from nutri_literature.repair_v5.representation import source_feature
    values=source_feature([],'Association')
    assert not any('pmid' in k or 'native_id' in k for k in values)

def test_literal_source_predicates_do_not_copy_old_type():
    from nutri_literature.repair_v5.source_rules import witness
    text='Drug down-regulated GENE expression.'
    event={'decision':'supported','native_type':'Association','negated':False,'observation':'observed',
        'evidence':[{'start':0,'end':len(text),'text':text}]}
    c={'start':0,'end':4};g={'start':20,'end':24}
    assert text[g['start']:g['end']]=='GENE'
    r=witness(event,c,g);assert r['native_type']=='Negative_Correlation' and r['change']=='decrease'
    assert witness({**event,'negated':True},c,g) is None
    assert witness({**event,'observation':'planned'},c,g) is None

def test_semantic_positive_is_independent_of_wrong_id(monkeypatch):
    from nutri_literature.repair_v5 import representation as rep
    source='Drug reduces GENE.'
    c={'key':'c','kind':'chemical','start':0,'end':4,'text':'Drug','old_ids':['Cwrong'],'candidates':[{'native_id':'Cwrong','features':{}}],'reviews':{}}
    g={'key':'g','kind':'gene','start':13,'end':17,'text':'GENE','old_ids':['1'],'candidates':[{'native_id':'1','features':{}}],'reviews':{}}
    endpoints=[{k:m[k] for k in ('kind','start','end','text')} for m in (c,g)]
    anchor={'entity1':'Cwrong','entity2':'1','predicate':'Negative_Correlation','endpoints':endpoints,'evidence':[{'start':0,'end':len(source),'text':source}]}
    monkeypatch.setattr(rep,'anchor_rows',lambda p,policy:[anchor])
    monkeypatch.setattr(rep.oldlearn,'choose',lambda *a,**kw:{'selected_id':None,'proposed_id':None,'selection_reason':'nil'})
    d={'pmid':'t','text':source,'entities':[{'kind':'component','start':0,'end':4,'native_ids':['Ctrue']},{'kind':'gene','start':13,'end':17,'native_ids':['1']}],
        'relations':[{'entity1':'Ctrue','entity2':'1','predicate':'Negative_Correlation'}]}
    rows=rep.candidates({'pmid':'t','document':d,'mentions':[c,g],'events':[]},{},{})
    assert rows[0]['semantic_target']==1 and rows[0]['strict_target']==0 and rows[0]['id_target']==0
