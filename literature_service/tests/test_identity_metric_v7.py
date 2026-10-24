def test_strict_identity_counts_nil_damage_and_multiple_gold_ids():
    from nutri_literature.identity_training_v7 import metric
    examples=[{'origin':'automatic','gold_ids':['1','2'],'previous_id':'NCBIGene:1',
               'records':[{'id':'NCBIGene:1','native_id':'1','taxon_id':9606}],
               'eligibility':{'eligible':True,'taxon_id':9606}}]
    rejected=metric(examples,[[.1,.9]],.8,0,2)
    assert rejected['tp']==0 and rejected['fn']==2 and rejected['damaged_old_correct']==1
    assert rejected['cleared_old_correct']==1 and rejected['baseline_tp']==1
    accepted=metric(examples,[[.95,.05]],.8,0,2)
    assert accepted['tp']==1 and accepted['fn']==1 and accepted['damaged_old_correct']==0
