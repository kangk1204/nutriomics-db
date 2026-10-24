"""Durable, allowlisted management jobs; credentials remain in the existing account DB."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import time

from . import auth
from .db import connect, digest, dumps, now, uid

KINDS = {'collect', 'reprocess', 'train-id', 'train-relation', 'evaluate-development', 'build-report', 'refresh-snapshot'}
SCHEMA = """
CREATE TABLE IF NOT EXISTS platform_roles(username TEXT PRIMARY KEY REFERENCES accounts(username),role TEXT NOT NULL CHECK(role IN ('reviewer','operator')));
CREATE TABLE IF NOT EXISTS platform_csrf(session_hash TEXT PRIMARY KEY,csrf_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS platform_jobs(job_id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE NOT NULL,kind TEXT NOT NULL,
 params_json TEXT NOT NULL,input_sha256 TEXT NOT NULL,created_by TEXT NOT NULL,status TEXT NOT NULL,
 created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,heartbeat TEXT,cancel_requested INTEGER NOT NULL DEFAULT 0,
 progress_json TEXT NOT NULL DEFAULT '{}',error TEXT,attempts INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS platform_jobs_status ON platform_jobs(status,created_at);
CREATE TABLE IF NOT EXISTS platform_review_notes(note_id TEXT PRIMARY KEY,assertion_id TEXT NOT NULL,
 source_sha256 TEXT NOT NULL,reviewer TEXT NOT NULL,decision TEXT NOT NULL,comment TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS platform_login_failures(username_hash TEXT NOT NULL,created_at TEXT NOT NULL);
"""


def initialize(settings):
    with connect(settings, durable=True) as con:
        con.executescript(SCHEMA)


def role(settings, username):
    with connect(settings) as con:
        row = con.execute('SELECT role FROM platform_roles WHERE username=?', (username,)).fetchone()
    return row[0] if row else 'reviewer'


def grant(settings, username, value):
    if value not in ('reviewer', 'operator'):
        raise ValueError('Unsupported platform role')
    with connect(settings, durable=True) as con:
        if not con.execute('SELECT 1 FROM accounts WHERE username=? AND enabled=1', (username,)).fetchone():
            raise ValueError('An enabled existing local account is required')
        con.execute('INSERT INTO platform_roles VALUES(?,?) ON CONFLICT(username) DO UPDATE SET role=excluded.role', (username, value))


def login(settings, username, password):
    identity = digest(username)
    since = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    with connect(settings) as con:
        n = con.execute('SELECT count(*) FROM platform_login_failures WHERE username_hash=? AND created_at>=?', (identity, since)).fetchone()[0]
    if n >= 8:
        raise PermissionError('too_many_attempts')
    if not auth.authenticate(settings, username, password):
        with connect(settings) as con:
            con.execute('INSERT INTO platform_login_failures VALUES(?,?)', (identity, now()))
        raise PermissionError('invalid_credentials')
    session = auth.create_session(settings, username);csrf = secrets.token_urlsafe(32)
    with connect(settings, durable=True) as con:
        con.execute('INSERT INTO platform_csrf VALUES(?,?)', (auth.token_hash(session['access_token']), digest(csrf)))
    return {**session, 'csrf': csrf, 'role': role(settings, username)}


def verify_csrf(settings, token, csrf):
    if not token or not csrf:
        return False
    with connect(settings) as con:
        row = con.execute('SELECT csrf_hash FROM platform_csrf WHERE session_hash=?', (auth.token_hash(token),)).fetchone()
    return bool(row and secrets.compare_digest(row[0], digest(csrf)))


def enqueue(settings, kind, params, key, username, snapshot_hash):
    if kind not in KINDS or not key or len(key) > 100:
        raise ValueError('허용되지 않은 작업 또는 idempotency key입니다.')
    if set(params) - {'engine', 'topic'} or params.get('engine', 'heuristic-v1') not in {'heuristic-v1', 'relation-repair-v6-gpu-r2'}:
        raise ValueError('작업 매개변수는 허용된 엔진·주제만 사용할 수 있습니다.')
    if any(not isinstance(v,str) or len(v)>80 for v in params.values()):
        raise ValueError('작업 매개변수는 80자 이하 문자열이어야 합니다.')
    if params.get('topic'):
        with connect(settings) as con:
            if not con.execute('SELECT 1 FROM topics WHERE topic_id=? AND enabled=1', (params['topic'],)).fetchone():
                raise ValueError('등록된 주제만 수집할 수 있습니다.')
    payload = dumps({'kind': kind, 'params': params, 'snapshot': snapshot_hash, 'config_sha256': hashlib.sha256(settings.config.read_bytes()).hexdigest()})
    ih = digest(payload)
    with connect(settings, durable=True) as con:
        con.execute('BEGIN IMMEDIATE')
        old = con.execute('SELECT * FROM platform_jobs WHERE idempotency_key=?', (key,)).fetchone()
        if old:
            if old['input_sha256'] != ih:
                raise ValueError('같은 idempotency key의 작업 입력이 변경되었습니다.')
            return dict(old)
        old = con.execute("SELECT * FROM platform_jobs WHERE input_sha256=? AND status IN ('queued','running','waiting','cancel_requested')", (ih,)).fetchone()
        if old:
            return dict(old)
        jid = uid()
        con.execute('INSERT INTO platform_jobs(job_id,idempotency_key,kind,params_json,input_sha256,created_by,status,created_at) VALUES(?,?,?,?,?,?,?,?)',
                    (jid, key, kind, dumps(params), ih, username, 'queued', now()))
        return dict(con.execute('SELECT * FROM platform_jobs WHERE job_id=?', (jid,)).fetchone())


def update(settings, job_id, **values):
    allowed = {'status','started_at','finished_at','heartbeat','progress_json','error','cancel_requested'}
    if not values or set(values)-allowed:
        raise ValueError('Unsupported job state update')
    with connect(settings, durable=True) as con:
        con.execute('UPDATE platform_jobs SET '+','.join(k+'=?' for k in values)+' WHERE job_id=?', [*values.values(), job_id])


def claim(settings):
    with connect(settings, durable=True) as con:
        con.execute('BEGIN IMMEDIATE')
        # One control worker leaves the second project processing slot for the GPU owner.
        if con.execute("SELECT 1 FROM platform_jobs WHERE status IN ('running','cancel_requested')").fetchone():
            return None
        row = con.execute("SELECT * FROM platform_jobs WHERE status='queued' ORDER BY created_at,job_id LIMIT 1").fetchone()
        if not row:
            return None
        con.execute("UPDATE platform_jobs SET status='running',started_at=?,heartbeat=?,attempts=attempts+1 WHERE job_id=?", (now(),now(),row['job_id']))
        return dict(row)


def run_one(settings, root, *, executor=None):
    job = claim(settings)
    if job is None:
        return None
    job_id = job['job_id'];kind=job['kind'];params=json.loads(job['params_json']);root=Path(root)
    logpath=settings.logs/('platform-job-'+job_id+'.log')
    try:
        if executor:
            result=executor(job)
            update(settings,job_id,status='completed',finished_at=now(),progress_json=dumps(result or {}));return job_id
        backfill=settings.home/'backfill-state.json'
        if backfill.exists() and json.loads(backfill.read_text()).get('running') and kind in {'collect','reprocess'}:
            update(settings,job_id,status='waiting',error='초기 전수 수집·처리 소유 작업이 진행 중입니다.',heartbeat=now());return job_id
        env={**os.environ,'LITERATURE_HOME':str(settings.home),'LITERATURE_CONFIG':str(settings.config),
             'OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1','MKL_NUM_THREADS':'1','PYTHONUNBUFFERED':'1'}
        if kind in {'train-id','train-relation'}:
            probe="from pathlib import Path;import json; h=Path('/home/keunsoo/projects/nutriomics-relation-v6-gpu-r2/runtime/relation-repair-v6-gpu-r2');p=h/'job-state.json';m=h/'cancel-request.json'; old=json.loads(m.read_text()) if m.exists() else {};m.unlink() if old.get('management_job')=="+repr(job_id)+" else None; print(json.dumps({'running':json.loads(p.read_text()).get('running',False) if p.exists() else False}))"
            import shlex
            check=subprocess.run(['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=12','keunsoo@100.93.148.94','python3 -c '+shlex.quote(probe)],capture_output=True,timeout=20)
            if check.returncode or json.loads(check.stdout)['running']:
                update(settings,job_id,status='waiting',error='GPU 연결 또는 기존 r2 소유 작업의 완료를 기다립니다.',heartbeat=now());return job_id
            command=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=12','keunsoo@100.93.148.94',
                     'cd /home/keunsoo/projects/nutriomics-relation-v6-gpu-r2 && .venv/bin/python -B scripts/relation_repair_v6_gpu_r2.py '+kind]
        elif kind=='evaluate-development':
            receipt=settings.home/'model-evaluation/evaluation.json'
            if not receipt.exists():
                update(settings,job_id,status='waiting',error='100편 동결 예측 및 로컬 평가 작업의 결과를 기다립니다.',heartbeat=now());return job_id
            result=json.loads(receipt.read_text());update(settings,job_id,status='completed',finished_at=now(),progress_json=dumps({'performance_target_passed':result['performance_target_passed']}));return job_id
        elif kind=='refresh-snapshot':
            command=[sys.executable,'-B',str(root/'scripts/refresh_platform_snapshots.py')]
        elif kind in {'build-report','collect','reprocess'}:
            command=[sys.executable,'-B','-m','nutri_literature.platform_tasks','--job',job_id]
        else:
            if params.get('engine','heuristic-v1')!='heuristic-v1':
                raise ValueError('r2 전체 문헌 재처리는 동결 모델과 전수 처리 계약이 준비된 뒤 실행할 수 있습니다.')
            command=[sys.executable,'-B','-m','nutri_literature.cli','reprocess','--engine','heuristic-v1']
        with logpath.open('ab',buffering=0) as out:
            process=subprocess.Popen(command,cwd=root,env=env,stdout=out,stderr=subprocess.STDOUT)
            while process.poll() is None:
                update(settings,job_id,heartbeat=now(),progress_json=dumps({'phase':kind,'worker_pid':os.getpid(),'process_pid':process.pid}))
                # Cancellation is cooperative: never kill an epoch/collection transaction.
                with connect(settings) as con:cancel=con.execute('SELECT cancel_requested FROM platform_jobs WHERE job_id=?',(job_id,)).fetchone()[0]
                if cancel and kind in {'train-id','train-relation'}:
                    marker="from pathlib import Path;import json;p=Path('/home/keunsoo/projects/nutriomics-relation-v6-gpu-r2/runtime/relation-repair-v6-gpu-r2/cancel-request.json');p.write_text(json.dumps({'management_job':"+repr(job_id)+"}))"
                    subprocess.run(['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8','keunsoo@100.93.148.94','python3 -c '+shlex.quote(marker)],stdout=out,stderr=out,timeout=12)
                time.sleep(2)
            if process.returncode:
                with connect(settings) as con:cancel=con.execute('SELECT cancel_requested FROM platform_jobs WHERE job_id=?',(job_id,)).fetchone()[0]
                if not cancel:raise RuntimeError('작업 프로세스 종료 코드 '+str(process.returncode))
        with connect(settings) as con:
            cancel=con.execute('SELECT cancel_requested FROM platform_jobs WHERE job_id=?',(job_id,)).fetchone()[0]
        resultpath=settings.home/'job-results'/(job_id+'.json')
        result=json.loads(resultpath.read_text()) if resultpath.exists() else {}
        final='cancelled' if cancel else result.get('status','completed')
        update(settings,job_id,status=final,finished_at=now() if final!='waiting' else None,progress_json=dumps(result or {'phase':'checkpoint_preserved' if cancel else 'completed'}))
        return job_id
    except Exception as exc:
        update(settings,job_id,status='failed',finished_at=now(),error=str(exc)[:1000]);return job_id
