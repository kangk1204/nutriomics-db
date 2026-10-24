def test_relation_upgrade_retains_clinical_observations_and_summary(monkeypatch):
    from copy import deepcopy
    from types import SimpleNamespace
    from nutri_literature import extraction_v6,extraction_v7,automatic_features_v6,scope_v5
    observation={'id':'obs-1','dose':'200 mg','timepoint':'8 weeks','statistical_significance':'not_significant'}
    summary={'design':'trial','results':['No significant difference was observed.']}
    base={'mentions':[],'sentence_roles':[],'events':[],
          'assertions':[{'payload':{'observation':observation}}],
          'clinical_observations':[observation],'summary':summary,'normalization_candidates':[],
          'manifest':{'engine_version':'heuristic-v6'},'dictionary_version':'test',
          'module_validation':{'clinical':'verified_as_v6'}}
    monkeypatch.setattr(extraction_v6,'extract',lambda *args:deepcopy(base))
    monkeypatch.setattr(automatic_features_v6,'bags_for',lambda *args:[])
    monkeypatch.setattr(scope_v5,'predict',lambda *args:[])
    head=SimpleNamespace(predict=lambda bags:[])
    model={'model_sha256':'test','selection':{'threshold':.9}}
    result=extraction_v7.extract('',[],None,{'_v7_model_path':'test','_v7_loaded_head':(head,model),
                                            '_gold_entities':[],'_learned_models':None})
    assert result['clinical_observations']==base['clinical_observations']
    assert result['summary']==base['summary']
    assert result['assertions']==base['assertions']
