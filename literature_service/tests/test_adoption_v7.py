from nutri_literature.adoption_v7 import identity_adoption


def test_optimized_oof_precision_cannot_override_outer_fold_failures():
    value={'selection':{'precision':.95,'target_precision_met':True},'outer_metrics':[
        {'tp':80,'fp':20,'fn':50,'changed':6,'correct_changes':5,'damaged_old_correct':0}]}
    result=identity_adoption(value)
    assert not result['passed']
    assert result['precision']==.8
    assert 'nested_selected_id_precision_below_90_percent' in result['reasons']
    assert 'strict_boundary_id_preservation_not_verified' in result['reasons']
