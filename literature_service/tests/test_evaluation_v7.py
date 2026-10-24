import json
from pathlib import Path
from nutri_literature.contract_v7 import sha


def test_source_predictions_are_persisted_before_gold(tmp_path,monkeypatch):
    import nutri_literature.evaluation_v7 as evaluation
    import nutri_literature.operations_v6 as operations
    import nutri_literature.automatic_features_v6 as features
    import nutri_literature.extraction_v7 as extraction
    import nutri_literature.extraction_v6 as legacy
    import nutri_literature.scope_v5 as scopes
    from nutri_literature import benchmarks
    text='A binds B.'
    entities=[{'kind':'component','start':0,'end':1,'text':'A','normalized_id':'MESH:D1','native_ids':['D1'],'payload':{'native_id':'D1'}},
              {'kind':'gene','start':8,'end':9,'text':'B','normalized_id':'NCBIGene:2','native_ids':['2'],'payload':{'native_id':'2'}}]
    document={'pmid':'1','text':text,'sections':[],'source_hash':sha(text)}
    sentence={'text':text,'start':0,'end':len(text),'label':'Results'}
    pair={'chemical':entities[0],'gene':entities[1],'sentence':sentence}
    head=tmp_path/'head';head.mkdir();(head/'manifest.json').write_text('{}')
    baseline=tmp_path/'baseline';baseline.mkdir()
    (baseline/'paper-1.json').write_text(json.dumps({'source_hash':sha(text),'entities':entities}))
    index=tmp_path/'public_benchmarks/v2-measurement-index.json';index.parent.mkdir()
    index.write_text(json.dumps({'steps':{'prepare:development:bioredirect:abstract':{'inputs_path':str(tmp_path/'input.json'),'gold_path':str(tmp_path/'gold.json')}}}))
    class Head:
        def predict(self,rows):
            return [{'Association':0.,'Bind':1.,'Negative_Correlation':0.,'Positive_Correlation':0.,'valid':1.,'exists':1.,'direction':{'undirected':1.}}]
    class Clinical:
        def predict(self,rows):return [sentence]
    monkeypatch.setattr(extraction,'load_head',lambda p:(Head(),{'training_pmids':[],'selection':{'threshold':.5}}))
    monkeypatch.setattr(extraction,'head_metadata',lambda p:{'training_pmids':[],'selection':{'threshold':.5}})
    monkeypatch.setattr(operations,'components',lambda *a:{'_encoder':object(),'_clinical_models':Clinical(),'_learned_models':object()})
    monkeypatch.setattr(features,'bags_for',lambda *a:[{'pairs':[pair],'features':{}}])
    monkeypatch.setattr(scopes,'predict',lambda *a:{})
    monkeypatch.setattr(legacy,'native_event',lambda *a:(None,['direct_evidence_unresolved']))
    monkeypatch.setattr(benchmarks,'_load_inputs',lambda *a:({'documents':[document]}, {},'source-hash'))
    output=tmp_path/'out'
    def gold(*args):
        prediction=json.loads((output/'paper-1.json').read_bytes())
        assert prediction['relations'][0]['entity1']=='D1'
        return {'documents':[{'pmid':'1','entities':entities,'relations':[{'entity1':'D1','entity2':'2','predicate':'Bind'}]}]}, {},'gold-hash'
    monkeypatch.setattr(benchmarks,'_load_gold',gold)
    report=evaluation.development(tmp_path,head,baseline,tmp_path/'models',output)
    assert report['metrics']['relation']['tp']==1 and report['metrics']['relation']['fn']==0
    assert report['metrics']['verified_relation']['tp']==0
    def forbidden(*args):raise AssertionError('Saved predictions must not reload NLP')
    monkeypatch.setattr(extraction,'load_head',forbidden)
    monkeypatch.setattr(operations,'components',forbidden)
    resumed=evaluation.development(tmp_path,head,baseline,tmp_path/'models',output)
    assert resumed==report
