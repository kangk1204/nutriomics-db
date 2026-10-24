from nutri_literature.repair_v5_revision import bind_reference

def test_existing_standalone_normalization_is_not_new_or_deleted():
    m={'kind':'gene','start':0,'end':4,'text':'GENE','old_ids':[],'features':{}}
    p={'mentions':[m]}
    bound=bind_reference(p,{('gene',0,4):'123'})
    assert bound['mentions'][0]['old_ids']==['123']
    assert bound['mentions'][0]['reference_id']=='123'
    assert m['old_ids']==[]

def test_unresolved_reference_retains_original_relation_ids():
    m={'kind':'chemical','start':0,'end':4,'text':'Drug','old_ids':['C1']}
    result=bind_reference({'mentions':[m]},{('chemical',0,4):None})
    assert result['mentions'][0]['old_ids']==['C1']

def test_reference_id_never_enters_numeric_feature_dictionary():
    m={'kind':'gene','start':0,'end':4,'text':'GENE','old_ids':[],'candidates':[{'native_id':'1','features':{'exact_alias':1.}}]}
    result=bind_reference({'mentions':[m]},{('gene',0,4):'1'})
    assert result['mentions'][0]['candidates'][0]['features']=={'exact_alias':1.}
