import json
import pytest
from nutri_literature.contract_v7 import POLICY,sources


def test_v7_registry_is_additive_and_promotion_is_disabled(tmp_path):
    pytest.importorskip('requests')
    from nutri_literature.registry_v7 import ENGINE_CHOICES,default_engine,switch_engine
    from nutri_literature.settings import Settings
    settings=Settings(tmp_path, tmp_path/'topics.json')
    assert 'heuristic-v7' in ENGINE_CHOICES and 'heuristic-v6' in ENGINE_CHOICES
    assert default_engine(settings)=='heuristic-v1'
    with pytest.raises(ValueError,match='relation-only'):
        switch_engine(settings,'heuristic-v7',{'passed':True})


def test_stored_experiment_api_does_not_load_nlp(tmp_path,monkeypatch):
    pytest.importorskip('requests')
    fastapi=pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from nutri_literature.settings import Settings
    from nutri_literature.api import create_app
    config=tmp_path/'topics.json'
    config.write_text(json.dumps({'topics':[]}))
    settings=Settings(tmp_path/'runtime',config)
    path=settings.home/'v7/dev/report.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'id':'dev','engine':'heuristic-v7','mode':'end_to_end','metrics':{},'papers':100}))
    def unavailable(*a,**k):
        raise AssertionError('API attempted model inference')
    import nutri_literature.operations_v6 as ops
    monkeypatch.setattr(ops,'components',unavailable)
    with TestClient(create_app(settings)) as client:
        response=client.get('/api/v1/experiments',params={'engine':'heuristic-v7'})
        assert response.status_code==200 and response.json()['items'][0]['papers']==100
        response=client.get('/api/v1/papers',params={'engine':'heuristic-v7'})
        assert response.status_code==200
