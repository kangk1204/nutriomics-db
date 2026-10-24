def test_missing_pmcids_do_not_collapse_different_training_papers(tmp_path):
    import json
    from nutri_literature.data_audit_v7 import canonical_training_documents
    from nutri_literature.corpora_v6 import audit_training
    documents=[{'pmid':'1','pmcid':None,'text':'Distinct first source'},
               {'pmid':'2','pmcid':None,'text':'Completely different source'},
               {'pmid':'3','pmcid':'PMC7','text':'Actual shared publication'},
               {'pmid':'4','pmcid':'pmc7','text':'Another version'}]
    original=json.loads(json.dumps(documents))
    path=tmp_path/'train.json'
    path.write_text(json.dumps({'split':'train','documents':canonical_training_documents(documents)}))
    result=audit_training(path,[],tmp_path/'audited')
    assert set(result['training_pmids'])=={'1','2','3'}
    assert documents==original
