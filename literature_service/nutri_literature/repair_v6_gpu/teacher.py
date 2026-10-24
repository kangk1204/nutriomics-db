"""Seven attempts maximum in the original ledger, local authorized login only."""
from datetime import datetime,timezone
import re
from .common import *

def run():
 from ..chatgpt_repair_io import AttemptBudget,BudgetExceeded
 from ..chatgpt_auth import ChatGPTAuth
 from ..chatgpt_client import ChatGPTPlanClient
 from ..repair_v4.review import PROMPT,ROLES,validate
 prior=ROOT/'runtime/chatgpt-repair-v2';destination=HOME/'teacher';receipt=destination/'selection.json'
 if not receipt.exists():
  # Selection sees source and alias ambiguity only, never an error list or annotation.
  options=[];training={d['pmid'] for d in read(HOME/'data/relations-train.json')['documents']}
  for path in sorted((ROOT/'runtime/chatgpt-repair-v4/payloads/train').glob('paper-*.json')):
   pmid=path.stem[6:]
   if pmid not in training:continue
   p=read(path)
   if p.get('gold_annotations_provided') or p.get('prior_answers_supplied'):raise ValueError('교사 입력에 정답/이전 답변 포함')
   score=0
   for m in p['mentions']:
    exact_names=[r for r in m['candidates'] if any(re.sub(r'\W','',n).casefold()==re.sub(r'\W','',m['text']).casefold() for n in r['names'])]
    score+=int(len(exact_names)!=1)+int(len({r.get('taxon') for r in exact_names if r.get('taxon')})>1)
   options.append((-score,sha(p['source_sha256']),pmid,str(path.relative_to(ROOT))))
  selected=sorted(options)[:3]
  if len(selected)!=3:raise ValueError('교사 학습 원문 후보 3편 없음')
  atomic(receipt,{'papers':[{'pmid':v[2],'payload':v[3],'payload_sha256':filehash(ROOT/v[3]),'ambiguity':-v[0]} for v in selected],
   'selection_uses_answers':False,'selection_rule':'source-only exact-alias ambiguity; deterministic hash tie break','development_calls':0,'maximum_attempts':7},True)
 selection=read(receipt);budget=AttemptBudget(prior/'attempts.sqlite3');auth=ChatGPTAuth()
 reference=read(next((prior/'correction/train').glob('*/responses/*.json')))
 profile=auth.status().get('active_registration_id')
 if not profile or sha(profile)!=reference['account_profile_sha256']:raise ValueError('기존 승인 로그인과 다름; 자동 계정 전환 금지')
 errors=[];completed=[]
 for paper in selection['papers']:
  payload=read(ROOT/paper['payload'])
  if filehash(ROOT/paper['payload'])!=paper['payload_sha256']:raise ValueError('선택 원문 변경')
  for model,effort in [('gpt-6.1-sol','medium'),('gpt-6-luna','max')]:
   target=destination/'responses'/model/('paper-'+paper['pmid']+'.json')
   prompt=PROMPT+'\n'+ROLES[model]+'\nIndependent source review for relation-repair-v6-gpu. No old answers are supplied.'
   request={'model':model,'reasoning_effort':effort,'instructions':prompt,'input':canonical(payload),'source_sha256':payload['source_sha256']}
   fingerprint=sha(canonical(request));atomic(destination/'requests'/model/target.name,request,True)
   if target.exists():
    saved=read(target)
    if saved['request_sha256']!=fingerprint or saved['actual_model']!=model or saved['account_profile_sha256']!=sha(profile):raise ValueError('교사 응답 캐시 불일치')
    validate(json.loads(saved['text']),payload);completed.append(str(target.relative_to(HOME)));continue
   for retry in range(2):
    with budget.connect() as db:
     db.execute('BEGIN IMMEDIATE')
     total=db.execute('SELECT count(*) FROM attempts').fetchone()[0];used=db.execute("SELECT count(*) FROM attempts WHERE phase LIKE 'repair-v6-gpu-%'").fetchone()[0]
     retries=db.execute("SELECT count(*) FROM attempts WHERE phase='repair-v6-gpu-retry'").fetchone()[0]
     duplicate=db.execute('SELECT count(*) FROM attempts WHERE request_hash=?',(fingerprint,)).fetchone()[0]>0
     is_retry=bool(retry or duplicate)
     if total>=3000 or used>=7 or (is_retry and retries>=1):raise BudgetExceeded('원래 예산 또는 v6 7회/재시도 1회 한도')
     attempt=db.execute('INSERT INTO attempts(request_hash,phase,model,state,started) VALUES(?,?,?,?,?)',(fingerprint,'repair-v6-gpu-retry' if is_retry else 'repair-v6-gpu-train',model,'started',datetime.now(timezone.utc).isoformat())).lastrowid
    try:
     response=ChatGPTPlanClient(auth,model,profile,reasoning_effort=effort)(prompt,request['input'])
     response.update(request_sha256=fingerprint,attempt_id=attempt,test_only=False)
     atomic(destination/'raw'/model/(target.stem+'-attempt'+str(attempt)+'.json'),response,True);budget.finish(attempt,response=response)
     if response.get('status')!='completed' or response.get('actual_model')!=model or response.get('account_profile_sha256')!=sha(profile):raise ValueError('교사 완료/모델/계정 불일치')
     validate(json.loads(response['text']),payload);atomic(target,response,True);completed.append(str(target.relative_to(HOME)));log('teacher-complete',pmid=paper['pmid'],model=model,budget=budget.status());break
    except Exception as exc:
     budget.finish(attempt,error=exc);errors.append({'pmid':paper['pmid'],'model':model,'error_class':type(exc).__name__,'attempt':attempt})
     atomic(destination/'errors.json',errors)
     if retry:raise
 atomic(destination/'manifest.json',{'responses':[{'path':p,'sha256':filehash(HOME/p)} for p in completed],'selection_sha256':filehash(receipt),'budget':budget.status(),'credentials_exported':False,'used_as_gold':False,'diagnostic_only':True,
  'upstream_menu_note':'Archived automatic menus have historical training exposure; quarantined from clean neural fitting and feature selection.'},True)
