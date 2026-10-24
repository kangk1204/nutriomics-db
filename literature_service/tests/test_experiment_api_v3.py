"""Stored experiment authenticity and read-only API isolation."""
import json
import pytest
import test_api as api_fixtures
from nutri_literature.benchmarks import _receipt
from nutri_literature.db import uid, dumps, now

workspace=api_fixtures.workspace


def test_experiments_not_started_has_no_engine_effect(workspace):
    client,_=workspace
    response=client.get('/api/v1/experiments')
    assert response.status_code==200
    assert response.json()['status']=='not_started'
    assert response.json()['promotion_eligible'] is False
    assert client.get('/healthz').json()['engine']=='heuristic-v1'


@pytest.fixture
def summary_workspace(workspace):
    client,settings=workspace
    record={'schema_version':'literature-v3-experiment-summary-v1','id':uid(),'at':now(),
            'engine':'heuristic-v3','reports':[],'promotion_eligible':False,'sealed_test_reused':False,
            'status':'synthetic_software_fixture'}
    _receipt(settings,'public_v3_experiment_summary',record['id'],record)
    path=settings.home/'experiments/v3/summary.json'
    path.parent.mkdir(parents=True)
    path.write_text(dumps(record),encoding='utf-8')
    return client,path,record


def test_audited_summary_read(summary_workspace):
    client,_,record=summary_workspace
    assert client.get('/api/v1/experiments').json()==record


def test_summary_tampering_rejected(summary_workspace):
    client,path,record=summary_workspace
    record['reports']=[{'split':'test','promotion_eligible':True}]
    path.write_text(json.dumps(record))
    assert client.get('/api/v1/experiments').status_code==409


def test_fixture_never_changes_default(summary_workspace):
    client,_,_=summary_workspace
    assert client.get('/api/v1/engines').json()['default']=='heuristic-v1'


def test_nutrition_preview_empty_is_not_validated(workspace):
    client,_=workspace
    value=client.get('/api/v1/experiments/nutrition').json()
    assert value['items']==[]
    assert value['nutrition_accuracy']=='unmeasured'
    assert client.get('/api/v1/experiments/nutrition/'+'a'*64).status_code==404


@pytest.fixture
def nutrition_fixture(workspace):
    client,settings=workspace
    case_id='a'*64
    case={'id':case_id,'split':'development','rules_version':'fixture','source_hash':'fixture-source'}
    case_path=settings.home/'experiments/v3/nutrition/fixture'/f'{case_id}.json'
    case_path.parent.mkdir(parents=True)
    case_path.write_text(dumps(case),encoding='utf-8')
    _receipt(settings,'nutrition_v3_preview_case',case_id,case)
    index={'id':uid(),'split':'development','identity':{'rules_version':'fixture'},
           'promotion_eligible':False,'sealed_test_reused':False,
           'items':[{'id':case_id,'source_hash':'fixture-source','path':str(case_path.relative_to(settings.home))}]}
    _receipt(settings,'nutrition_v3_preview_index',index['id'],index)
    (settings.home/'experiments/v3/nutrition-preview.json').write_text(dumps(index),encoding='utf-8')
    return client,case_path,case_id


def test_preview_paths_are_not_exposed(nutrition_fixture):
    client,_,_=nutrition_fixture
    assert 'path' not in client.get('/api/v1/experiments/nutrition').json()['items'][0]


def test_preview_source_receipt_required(nutrition_fixture):
    client,path,case_id=nutrition_fixture
    assert client.get('/api/v1/experiments/nutrition/'+case_id).status_code==200
    value=json.loads(path.read_bytes())
    value['source_hash']='tampered'
    path.write_text(dumps(value))
    assert client.get('/api/v1/experiments/nutrition/'+case_id).status_code==409


def test_unknown_preview_id_is_missing(nutrition_fixture):
    client,_,_=nutrition_fixture
    assert client.get('/api/v1/experiments/nutrition/'+'b'*64).status_code==404
