"""Internal-only cookie-authenticated management app. Never mounted in the public factory."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .literature_api import attach_literature


def legacy_modules():
    source=Path(__file__).resolve().parents[1]/'literature_service'
    if source.is_dir() and str(source) not in sys.path:sys.path.insert(0,str(source))
    from nutri_literature import auth,platform_control
    from nutri_literature.db import connect,initialize,dumps,uid,now
    from nutri_literature.settings import Settings
    return auth,platform_control,connect,initialize,dumps,uid,now,Settings


class Login(BaseModel):
    model_config=ConfigDict(extra='forbid')
    username:str=Field(min_length=3,max_length=64)
    password:str=Field(min_length=1,max_length=1024,repr=False)


class Job(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind:str=Field(max_length=40)
    params:dict=Field(default_factory=dict)


class ReviewInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    event_id:str=Field(max_length=128)
    source_sha256:str=Field(min_length=64,max_length=64)
    decision:str=Field(max_length=30)
    comment:str=Field(min_length=1,max_length=10000)
    corrected_payload:dict|None=None
    human_confirmation:bool=False


def create_app(settings=None, *, snapshot=None, dist=None, secure_cookie=False):
    auth,control,connect,initialize,dumps,uid,now,Settings=legacy_modules()
    settings=settings or Settings.from_env()
    if not settings.database.exists():initialize(settings)
    control.initialize(settings)
    app=FastAPI(title='NutriOmics internal literature management',version='1.0')
    attach_literature(app,public=False,database_path=snapshot)
    app.state.literature_management=True;app.state.settings=settings
    prefix='/api/v1/literature-admin'

    @app.middleware('http')
    async def internal_boundary(request,call_next):
        path=request.url.path
        if path.startswith('/api/v1/explorer/literature') and path!='/api/v1/explorer/literature/stats':
            token=request.cookies.get('nutri_lit_session','')
            if not auth.verify_token(settings,token):return JSONResponse({'detail':'개인 검수 계정으로 로그인하세요.'},status_code=401)
        if request.method not in ('GET','HEAD','OPTIONS') and request.headers.get('origin'):
            from urllib.parse import urlsplit
            if urlsplit(request.headers['origin']).netloc!=request.headers.get('host'):
                return JSONResponse({'detail':'다른 origin의 상태 변경 요청은 허용하지 않습니다.'},status_code=403)
        response=await call_next(request);response.headers['Cache-Control']='no-store';return response

    def actor(request,operator=False,mutate=False):
        token=request.cookies.get('nutri_lit_session','');username=auth.verify_token(settings,token)
        if not username:raise HTTPException(401,'개인 검수 계정으로 로그인하세요.')
        if operator and control.role(settings,username)!='operator':raise HTTPException(403,'운영자 권한이 필요합니다.')
        if mutate and not control.verify_csrf(settings,token,request.headers.get('X-CSRF-Token','')):
            raise HTTPException(403,'CSRF 검증에 실패했습니다. 로그인 상태를 다시 확인하세요.')
        return username

    @app.get('/healthz')
    def health():return {'status':'ok','scope':'internal_management','public_management_routes':False}

    @app.post(prefix+'/session')
    def login(body:Login,response:Response):
        try:s=control.login(settings,body.username,body.password)
        except PermissionError as exc:raise HTTPException(429 if str(exc)=='too_many_attempts' else 401,'로그인 정보 또는 시도 제한을 확인하세요.') from None
        response.set_cookie('nutri_lit_session',s['access_token'],httponly=True,secure=secure_cookie,samesite='strict',max_age=8*3600,path='/')
        response.set_cookie('nutri_lit_csrf',s['csrf'],httponly=False,secure=secure_cookie,samesite='strict',max_age=8*3600,path='/')
        return {k:s[k] for k in ('username','role','expires_at','csrf')}

    @app.get(prefix+'/session')
    def me(request:Request):
        username=actor(request);return {'username':username,'role':control.role(settings,username),'csrf':request.cookies.get('nutri_lit_csrf','')}

    @app.delete(prefix+'/session')
    def logout(request:Request,response:Response):
        actor(request,mutate=True);auth.revoke_token(settings,request.cookies['nutri_lit_session'])
        for name in ('nutri_lit_session','nutri_lit_csrf'):response.delete_cookie(name,path='/')
        return {'status':'logged_out'}

    @app.get(prefix+'/jobs')
    def jobs(request:Request):
        actor(request)
        with connect(settings) as con:
            rows=[dict(r) for r in con.execute('SELECT * FROM platform_jobs ORDER BY created_at DESC LIMIT 100')]
        for r in rows:r['progress']=json.loads(r.pop('progress_json'));r['params']=json.loads(r.pop('params_json'))
        backfill=settings.home/'backfill-state.json'
        training=settings.home/'gpu-training-state.json'
        return {'items':rows,'total_shown':len(rows),'backfill':json.loads(backfill.read_text()) if backfill.exists() else None,
                'model_training':json.loads(training.read_text()) if training.exists() else None}

    @app.post(prefix+'/jobs',status_code=202)
    def add_job(body:Job,request:Request):
        username=actor(request,operator=True,mutate=True)
        snapshot_hash=getattr(app.state,'literature_database',None)
        version=json.loads((Path(snapshot_hash).parent/'manifest.json').read_text())['source_transaction_sha256'] if snapshot_hash else 'not_connected'
        try:return control.enqueue(settings,body.kind,body.params,request.headers.get('Idempotency-Key',''),username,version)
        except ValueError as exc:raise HTTPException(409,str(exc)) from None

    @app.post(prefix+'/jobs/{job_id}/cancel')
    def cancel(job_id:str,request:Request):
        actor(request,operator=True,mutate=True)
        with connect(settings,durable=True) as con:
            row=con.execute('SELECT status FROM platform_jobs WHERE job_id=?',(job_id,)).fetchone()
            if not row:raise HTTPException(404,'작업을 찾을 수 없습니다.')
            if row[0] not in ('queued','running','waiting','cancel_requested'):raise HTTPException(409,'이미 종료된 작업입니다.')
            status='cancelled' if row[0] in ('queued','waiting') else 'cancel_requested'
            con.execute('UPDATE platform_jobs SET cancel_requested=1,status=? WHERE job_id=?',(status,job_id))
        return {'job_id':job_id,'status':status,'checkpoint_preserved':True}

    @app.post(prefix+'/jobs/{job_id}/resume')
    def resume(job_id:str,request:Request):
        actor(request,operator=True,mutate=True)
        with connect(settings,durable=True) as con:
            con.execute('BEGIN IMMEDIATE');row=con.execute('SELECT * FROM platform_jobs WHERE job_id=?',(job_id,)).fetchone()
            if not row:raise HTTPException(404,'작업을 찾을 수 없습니다.')
            if row['status'] not in ('failed','waiting','cancelled'):raise HTTPException(409,'재개 가능한 작업이 아닙니다.')
            if row['status']=='failed' and row['attempts']>=3:raise HTTPException(409,'실패 원인 확인이 필요한 작업입니다.')
            con.execute("UPDATE platform_jobs SET status='queued',cancel_requested=0,error=NULL WHERE job_id=?",(job_id,))
        return {'job_id':job_id,'status':'queued'}

    @app.post(prefix+'/reviews')
    def review(body:ReviewInput,request:Request):
        username=actor(request,mutate=True)
        if not body.human_confirmation:raise HTTPException(422,'검수자가 원문을 직접 확인했다는 표시가 필요합니다.')
        if body.decision not in ('approved','corrected','rejected','unconfirmed'):raise HTTPException(422,'지원하지 않는 검수 판정입니다.')
        from .literature_api import database
        with database(request) as snap:
            row=snap.execute('SELECT * FROM events WHERE event_id=?',(body.event_id,)).fetchone()
            if not row:raise HTTPException(404,'이 snapshot에 해당 후보가 없습니다.')
            payload=json.loads(row['payload_json']);assertion_id=payload['original_assertion_id']
        if row['source_sha256']!=body.source_sha256:raise HTTPException(409,'검수한 원문 버전이 다릅니다.')
        with connect(settings,durable=True) as con:
            if body.decision=='unconfirmed':con.execute('BEGIN IMMEDIATE')
            current=con.execute('SELECT s.text_hash,e.active,p.current_source_id,e.source_id FROM assertions a JOIN extractions e USING(extraction_id) JOIN papers p ON p.paper_id=a.paper_id JOIN sources s ON s.source_id=e.source_id WHERE a.assertion_id=?',(assertion_id,)).fetchone()
            if not current or current[0]!=body.source_sha256 or not current[1] or current[2]!=current[3]:raise HTTPException(409,'원문 또는 추출 버전이 변경되어 재검토가 필요합니다.')
            latest=con.execute('SELECT review_id FROM reviews WHERE assertion_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',(assertion_id,)).fetchone()
            if (latest[0] if latest else None)!=payload.get('review_cursor'):raise HTTPException(409,'다른 검수 판정이 저장됐습니다. 새 snapshot을 확인하세요.')
            if body.decision=='unconfirmed':
                con.execute('INSERT INTO platform_review_notes VALUES(?,?,?,?,?,?,?)',(uid(),assertion_id,body.source_sha256,username,'unconfirmed',body.comment,now()))
                con.execute("UPDATE assertions SET status='needs_review' WHERE assertion_id=?",(assertion_id,))
                return {'assertion_id':assertion_id,'decision':'unconfirmed','expert_approved':False}
        from nutri_literature.api import create_app as legacy_app,Review
        legacy=legacy_app(settings);handler=next(r.endpoint for r in legacy.routes if getattr(r,'path',None)=='/api/v1/reviews' and 'POST' in getattr(r,'methods',set()))
        decision,correction=body.decision,body.corrected_payload
        if decision=='approved' and correction is None and payload.get('review_correction'):
            # Approving the displayed corrected version must not silently restore the original model labels.
            decision,correction='corrected',payload['review_correction']
        return handler(Review(assertion_id=assertion_id,decision=decision,comment=body.comment,corrected_payload=correction),username=username)

    assets=Path(dist or os.environ.get('NUTRIGENOMICS_EXPLORER_DIST',Path(__file__).resolve().parents[1]/'frontend/explorer/dist')).resolve()
    @app.get('/')
    def home():return RedirectResponse('/explorer/?view=literature')
    @app.get('/explorer/{asset:path}')
    def explorer(asset:str):
        p=(assets/(asset or 'index.html')).resolve()
        if not p.is_relative_to(assets) or not p.is_file():raise HTTPException(404,'화면 파일을 찾을 수 없습니다.')
        return FileResponse(p)
    return app


def app_factory():
    return create_app(snapshot=os.environ.get('NUTRIOMICS_INTERNAL_LITERATURE_SNAPSHOT'),secure_cookie=os.environ.get('NUTRIOMICS_SECURE_COOKIE')=='1')
