"""Isolated UI-only fixture; no research DB, real accounts, reviews, models or remote calls."""
from pathlib import Path
from fastapi import FastAPI,Request,Response,HTTPException
from fastapi.staticfiles import StaticFiles
app=FastAPI(title='QA-only literature UI')


@app.get('/api/v1/explorer/literature/stats')
def stats():return {'status':'available','release_id':'QA-only-no-research-data','scope':'internal','counts':{},'engines':{'heuristic-v1':{}},'benchmarks':[],'capabilities':{'management':True},'collection':{'topics':[]}}


def checked(r):
    if r.cookies.get('nutri_lit_session')!='QA-only':raise HTTPException(401,'QA session absent')


@app.get('/api/v1/explorer/literature/papers')
def papers(r:Request):checked(r);return {'items':[],'total':0}


@app.post('/api/v1/literature-admin/session')
def login(response:Response):
    response.set_cookie('nutri_lit_session','QA-only',httponly=True,samesite='strict')
    return {'username':'qa-operator','role':'operator','csrf':'QA-not-a-real-secret'}


@app.get('/api/v1/literature-admin/session')
def me(r:Request):checked(r);return {'username':'qa-operator','role':'operator','csrf':'QA-not-a-real-secret'}


@app.delete('/api/v1/literature-admin/session')
def logout(r:Request,response:Response):checked(r);response.delete_cookie('nutri_lit_session');return {'status':'logged_out'}


@app.get('/api/v1/literature-admin/jobs')
def jobs(r:Request):checked(r);return {'items':[]}


app.mount('/explorer',StaticFiles(directory=Path(__file__).resolve().parents[2]/'frontend/explorer/dist',html=True),name='ui')
