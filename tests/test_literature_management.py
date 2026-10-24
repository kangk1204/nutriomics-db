import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'literature_service'))
from nutri_literature.settings import Settings
from nutri_literature.db import initialize,connect
from nutri_literature.auth import create_account
from nutri_literature import platform_control as control
from nutrigenomics.literature_management import create_app


@pytest.fixture
def setup(tmp_path):
    config=tmp_path/'topics.json';config.write_text(json.dumps({'topics':[{'id':'HTN','name':'HTN','query':'hypertension','start_date':'2010-01-01','enabled':True}]}))
    settings=Settings(tmp_path/'runtime',config);initialize(settings);control.initialize(settings)
    for name in ('reviewer','operator'):create_account(settings,name,'correct-horse-battery-phrase')
    control.grant(settings,'operator','operator')
    return settings,TestClient(create_app(settings))


def login(client,username='operator'):
    response=client.post('/api/v1/literature-admin/session',json={'username':username,'password':'correct-horse-battery-phrase'})
    assert response.status_code==200
    return {'X-CSRF-Token':response.json()['csrf'],'Idempotency-Key':'one'}


def test_internal_text_and_management_require_authentication(setup):
    settings,c=setup
    assert c.get('/api/v1/explorer/literature/papers').status_code==401
    assert c.get('/api/v1/literature-admin/jobs').status_code==401
    assert c.get('/api/v1/literature-admin/session').status_code==401
    assert c.get('/api/v1/explorer/literature/stats').status_code==200


def test_session_is_http_only_expires_and_does_not_return_bearer_secret(setup):
    settings,c=setup
    r=c.post('/api/v1/literature-admin/session',json={'username':'operator','password':'correct-horse-battery-phrase'})
    assert r.status_code==200 and 'access_token' not in r.json()
    cookie=r.headers.get_list('set-cookie')[0]
    assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie and 'Max-Age=28800' in cookie
    h={'X-CSRF-Token':r.json()['csrf']}
    assert c.get('/api/v1/literature-admin/session').json()['role']=='operator'
    assert c.delete('/api/v1/literature-admin/session',headers=h).status_code==200
    assert c.get('/api/v1/literature-admin/jobs').status_code==401


def test_reviewer_cannot_start_training_and_operator_requires_csrf(setup):
    settings,c=setup;h=login(c,'reviewer')
    assert c.post('/api/v1/literature-admin/jobs',json={'kind':'train-id'},headers=h).status_code==403
    login(c)
    assert c.post('/api/v1/literature-admin/jobs',json={'kind':'train-id'}).status_code==403


def test_idempotent_jobs_do_not_run_inside_request_or_accept_commands(setup):
    settings,c=setup;h=login(c)
    a=c.post('/api/v1/literature-admin/jobs',json={'kind':'collect'},headers=h)
    b=c.post('/api/v1/literature-admin/jobs',json={'kind':'collect'},headers=h)
    assert a.status_code==202 and b.json()['job_id']==a.json()['job_id'] and a.json()['status']=='queued'
    assert c.post('/api/v1/literature-admin/jobs',json={'kind':'shell','params':{'cmd':'id'}},headers={**h,'Idempotency-Key':'two'}).status_code==409
    assert c.post('/api/v1/literature-admin/jobs',json={'kind':'collect','params':{'topic':{'bad':'object'}}},headers={**h,'Idempotency-Key':'three'}).status_code==409
    assert c.post('/api/v1/literature-admin/jobs',json={'kind':'reprocess'},headers=h).status_code==409


def test_cancellation_resume_and_claim_are_exclusive(setup):
    settings,c=setup;h=login(c)
    a=c.post('/api/v1/literature-admin/jobs',json={'kind':'collect'},headers=h).json()
    assert c.post('/api/v1/literature-admin/jobs/'+a['job_id']+'/cancel',headers=h).json()['status']=='cancelled'
    assert c.post('/api/v1/literature-admin/jobs/'+a['job_id']+'/resume',headers=h).status_code==200
    first=control.claim(settings);assert first['job_id']==a['job_id'] and control.claim(settings) is None
    assert c.post('/api/v1/literature-admin/jobs/'+a['job_id']+'/cancel',headers=h).json()['status']=='cancel_requested'


def test_cross_origin_changes_and_automatic_human_review_are_rejected(setup):
    settings,c=setup;h=login(c)
    assert c.post('/api/v1/literature-admin/jobs',json={'kind':'collect'},headers={**h,'Origin':'https://other.example'}).status_code==403
    assert c.post('/api/v1/literature-admin/reviews',json={'event_id':'x','source_sha256':'a'*64,'decision':'approved','comment':'test','human_confirmation':False},headers=h).status_code==422


def test_worker_executes_only_claimed_job_and_saves_result(setup):
    settings,c=setup;h=login(c)
    job=c.post('/api/v1/literature-admin/jobs',json={'kind':'collect'},headers=h).json()
    executed=[]
    control.run_one(settings,Path.cwd(),executor=lambda j:executed.append(j['job_id']) or {'processed':12})
    assert executed==[job['job_id']]
    rows=c.get('/api/v1/literature-admin/jobs').json()['items'];assert rows[0]['status']=='completed' and rows[0]['progress']['processed']==12
    assert control.run_one(settings,Path.cwd(),executor=lambda j:None) is None
