"""22 planned independent calls, at most three counted retries, original ledger."""
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timezone
import json
from .common import *
from ..chatgpt_repair_io import AttemptBudget,BudgetExceeded
from ..repair_v4.review import PROMPT as ORIGINAL,ROLES,validate

PROMPT=ORIGINAL+'''\nAdditional v5 contract: keep measurement and benchmark type independent. Do not pick a gene
subunit merely because a family name is mentioned; retain NIL when species/subunit is unresolved.
An ID menu is a retrieval hypothesis, not a statement that all candidates are synonyms.
Use compact roles from active_agent, analyte, therapeutic_protein, endogenous_protein, gene_product,
variant, residue, assay_reagent, family, generic_class, unknown. Preserve exact source evidence.
Explicit target inhibitors and altered binding require their individual scopes. No prior answer is supplied.
For chemical-gene types, a gene being a chemical receptor or a chemical binding a gene promoter supports
Bind, unless the source gives a signed functional effect that supports Positive/Negative_Correlation.
Use the exact measured variable and the named gene's role; don't confuse increased resistance with
increased drug sensitivity. A sequence residue or a chosen probe alone does not establish a relation.
'''

class Budget(AttemptBudget):
    def reserve(self,fingerprint,phase,model):
        caps={'repair-v5-train':16,'repair-v5-development':6,'repair-v5-retry':3}
        if phase not in caps:raise ValueError('Unknown v5 phase')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM attempts WHERE request_hash=?',(fingerprint,)).fetchone()[0]:phase='repair-v5-retry'
            if db.execute('SELECT count(*) FROM attempts').fetchone()[0]>=3000:raise BudgetExceeded('원래 3000회 예산 소진')
            if db.execute("SELECT count(*) FROM attempts WHERE phase LIKE 'repair-v5-%'").fetchone()[0]>=25:raise BudgetExceeded('v5 추가 25회 소진')
            if db.execute('SELECT count(*) FROM attempts WHERE phase=?',(phase,)).fetchone()[0]>=caps[phase]:raise BudgetExceeded('v5 단계 예산 소진: '+phase)
            return db.execute('INSERT INTO attempts(request_hash,phase,model,state,started) VALUES(?,?,?,?,?)',
                (fingerprint,phase,model,'started',datetime.now(timezone.utc).isoformat())).lastrowid

def paths(split,pmid,model,effort):
    root=HOME/'inference'/split/(model+'-'+effort)
    return root/'responses'/('paper-'+pmid+'.json'),root/'valid'/('paper-'+pmid+'.json')

def one(split,pmid,model,effort,auth,budget):
    from ..chatgpt_client import ChatGPTPlanClient
    payload=read(HOME/'payloads'/split/('paper-'+pmid+'.json'))
    response_path,valid_path=paths(split,pmid,model,effort)
    base={'model':model,'reasoning_effort':effort,'instructions':PROMPT+'\n'+ROLES[model],
        'input':canonical(payload),'source_sha256':payload['source_sha256'],'prior_answers_supplied':False}
    if valid_path.exists():
        v=read(valid_path);r=HOME/v['response_path']
        assert v['base_request_sha256']==sha(canonical(base)) and filehash(r)==v['response_sha256']
        validate(json.loads(read(r)['text']),payload);return 'cached'
    error=None;parent=None
    for retry in range(3):
        request=dict(base);target=response_path if retry==0 else response_path.with_stem(response_path.stem+'-format'+str(retry))
        if retry:request.update(parent_request_sha256=parent,input=canonical({'source_payload':payload,'format_error':error,'instruction':'Return complete valid compact JSON.'}))
        fingerprint=sha(canonical(request));immutable(target.parent.parent/'requests'/target.name,request)
        if target.exists():result=read(target)
        else:
            attempt=budget.reserve(fingerprint,'repair-v5-'+split if retry==0 else 'repair-v5-retry',model)
            try:
                result=ChatGPTPlanClient(auth,model,auth.status()['active_registration_id'],reasoning_effort=effort)(request['instructions'],request['input'])
                result.update(request_sha256=fingerprint,attempt_id=attempt,test_only=False)
                immutable(target,result);budget.finish(attempt,response=result)
            except Exception as exc:
                budget.finish(attempt,error=exc)
                safe=str(exc)[:180] if type(exc).__name__ in ('ChatGPTRequestError','ChatGPTAuthError','BudgetExceeded') else type(exc).__name__
                immutable(target.parent.parent/'errors'/(target.stem+'-attempt'+str(attempt)+'.json'),{'request_sha256':fingerprint,'error_class':type(exc).__name__,'safe_message':safe,'credentials_logged':False})
                raise
        if result.get('status')!='completed' or result.get('actual_model')!=model or result.get('account_profile_sha256')!=auth._repair_expected_profile_sha256 or result.get('request_sha256')!=fingerprint:raise ValueError('응답 모델·계정·요청 불일치')
        try:raw=validate(json.loads(result['text']),payload)
        except (ValueError,TypeError,KeyError) as exc:
            error=str(exc)[:100];parent=fingerprint;immutable(target.parent.parent/'rejections'/target.name,{'reason':error,'request_sha256':fingerprint});continue
        immutable(valid_path,{'base_request_sha256':sha(canonical(base)),'request_sha256':fingerprint,
            'response_path':str(target.relative_to(HOME)),'response_sha256':filehash(target),
            'model':model,'effort':effort,'events':len(raw['events']),'source_sha256':payload['source_sha256']})
        return 'completed'
    raise ValueError('형식 재시도 실패')

def request(split):
    from ..chatgpt_auth import ChatGPTAuth
    from ..chatgpt_batch import batch_lock
    from .. import chatgpt_tuning as old
    if split=='development':
        from .pipeline import verify_freeze
        verify_freeze()
    selection=read(HOME/'targeted-cohort.json');ids=selection[split]
    auth=ChatGPTAuth();reference=read(next((PRIOR/'correction/train').glob('*/responses/*.json')))
    if sha(auth.status().get('active_registration_id') or '')!=reference['account_profile_sha256']:raise ValueError('기존 승인 계정 불일치; 자동 전환 금지')
    auth._repair_expected_profile_sha256=reference['account_profile_sha256']
    budget=Budget(PRIOR/'attempts.sqlite3');errors=[]
    with batch_lock(old.PRIOR),ThreadPoolExecutor(max_workers=2) as pool:
        pending=iter([(p,m,e) for p in ids for m,e in MODELS]);futures={}
        def submit():
            task=next(pending,None)
            if task:
                p,m,e=task;futures[pool.submit(one,split,p,m,e,auth,budget)]=(p,m)
        for _ in range(2):submit()
        from concurrent.futures import wait,FIRST_COMPLETED
        while futures:
            done,_=wait(futures,return_when=FIRST_COMPLETED)
            for future in done:
                p,m=futures.pop(future)
                try:state=future.result();log('inference-progress',split=split,pmid=p,model=m,state=state,budget=budget_status())
                except Exception as exc:
                    errors.append({'pmid':p,'model':m,'error_class':type(exc).__name__,
                        'message':str(exc)[:180] if type(exc).__name__ in ('ChatGPTRequestError','ChatGPTAuthError','BudgetExceeded') else type(exc).__name__})
                    log('inference-error',**errors[-1])
            if not errors:
                for _ in done:submit()
    if errors:raise RuntimeError('완료 응답 보존; 중단 '+canonical(errors))
    write(split+'-inference-complete.json',{'pmids':ids,'valid_responses':len(ids)*2,'budget':budget_status(),'prompt_sha256':sha(PROMPT)})
