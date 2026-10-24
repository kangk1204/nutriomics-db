def test_diagnostics_resume_keeps_metrics_despite_error_order(tmp_path):
    import json
    from nutri_literature import evaluation_v6
    from nutri_literature.contract_v7 import canonical,sha
    from nutri_literature.mapping_v7 import native_mapping
    from nutri_literature.diagnostics_v7 import diagnose
    def entity(kind,start,native):
        return {'kind':kind,'start':start,'end':start+1,'text':'X Y Z'[start],
                'native_ids':[native],'normalized_id':('NCBIGene:' if kind=='gene' else 'MESH:')+native,
                'payload':{'native_id':native}}
    a,b,c=entity('component',0,'D1'),entity('gene',2,'1'),entity('component',4,'D2')
    gold={'pmid':'p','text':'X Y Z','entities':[a,b,c],'relations':[]}
    raw=canonical({'documents':[gold]}).encode();(tmp_path/'gold.json').write_bytes(raw)
    prediction={'identity':{'test':True},'entities':[a,b,c],
        'relations':[{'entity1':'D1','entity2':'1','predicate':'Association'},
                     {'entity1':'D2','entity2':'1','predicate':'Association'}],
        'verified_relations':[],'candidate_pairs':[[a,b],[c,b]]}
    (tmp_path/'paper-p.json').write_text(canonical(prediction))
    metric=evaluation_v6._metrics_for(gold,prediction,native_mapping(prediction['entities']),False,'bioredirect')
    report={'mode':'end_to_end','identity':prediction['identity'],'gold_sha256':sha(raw),
            'prepared_gold_path':str(tmp_path/'gold.json'),'per_paper':[{'pmid':'p',**metric}]}
    (tmp_path/'report.json').write_text(canonical(report))
    output=tmp_path/'diagnostics.json';first=diagnose(tmp_path/'report.json',output)
    assert len(first['error_examples'])==2 and first['candidate_coverage']['total']==0
    legacy={**first,'error_examples':list(reversed(first['error_examples']))}
    output.write_text(canonical(legacy))
    assert canonical(diagnose(tmp_path/'report.json',output))==canonical(legacy)
