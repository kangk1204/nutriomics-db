def test_optimized_oof_pass_cannot_override_nested_id_failure(monkeypatch):
    from types import SimpleNamespace
    from nutri_literature import identity_v7
    from nutri_literature.identity_artifact_v7 import apply_identity
    monkeypatch.setattr(identity_v7,'candidate_rows',lambda *a:([
        {'id':'NCBIGene:8','native_id':'8','taxon_id':9606}], [{},{}],
        {'eligible':True,'taxon_id':9606}))
    ranker=SimpleNamespace(predict=lambda rows:[.98,.02])
    manifest={'selection':{'threshold':.8,'margin':0,'target_precision_met':True},
              'outer_metrics':[{'tp':8,'fp':2,'fn':1,'changed':1,'correct_changes':1,'damaged_old_correct':0}],
              'strict_boundary_id_preservation_verified':True}
    mentions=[{'kind':'gene','text':'X','start':0,'end':1,'normalized_id':'NCBIGene:7'}]
    result,hypotheses=apply_identity(mentions,'X',[],{'_id_search':object()},(ranker,manifest))
    assert result[0]['normalized_id']=='NCBIGene:7'
    assert hypotheses[0]['proposed_id']=='NCBIGene:8'
    assert hypotheses[0]['automatic_applied'] is False
