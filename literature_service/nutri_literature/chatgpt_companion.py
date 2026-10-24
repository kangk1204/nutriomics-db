"""Loopback-only personal ChatGPT connections and stored comparison results.

This separate local companion does not replace researcher login or production
8010/8510 services. Credentials stay in the OS-protected account store.
"""
from __future__ import annotations

import json
import asyncio
from pathlib import Path
import secrets
import threading

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .chatgpt_auth import ChatGPTAuth, ChatGPTAuthError
from .chatgpt_client import ChatGPTPlanClient, ChatGPTRequestError, available_models
from .chatgpt_batch import batch_lock, batch_busy


PAGE = """<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>개인 ChatGPT · 문헌 근거 비교</title><style nonce="__NONCE__">
body{font:16px/1.65 system-ui,sans-serif;background:#f1f5f8;color:#132838;margin:0}main{max-width:1080px;margin:42px auto;padding:0 24px}h1{font-size:30px}h2{font-size:20px}section{background:white;border:1px solid #d7e0e5;border-radius:14px;padding:24px;margin:20px 0}button,select{font:inherit;padding:10px 16px;border:1px solid #708594;border-radius:8px;margin:5px 8px 5px 0}button{cursor:pointer;background:#155e75;color:white}button:disabled{opacity:.5;cursor:default}small,.muted{color:#536573}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}td,th{text-align:right;padding:10px;border-bottom:1px solid #e0e6eb}td:first-child,th:first-child{text-align:left}#notice{white-space:pre-wrap;color:#9b3418}a{color:#155e75}progress{width:100%;height:20px}.pill{display:inline-block;background:#e4f2f3;padding:3px 10px;border-radius:8px}</style>
<main><div class="pill">개인용 로컬 연결</div><h1>ChatGPT 문헌 관계 추출 비교</h1>
<p>같은 문헌 100편 · 정답 관계 154건 · 기존 평가기. 원문만 모델에 전달하고 정답은 전달하지 않습니다.</p>
<section><h2>1. 개인 ChatGPT 연결</h2><p id="account">연결 상태 확인 중</p>
<button id="connect">Continue with ChatGPT · 다른 계정 연결</button>
<select id="accounts" aria-label="저장 계정 선택"></select><button id="select">선택 계정 사용</button><button id="logout">선택 계정 연결 해제</button>
<p><a href="https://chatgpt.com/#settings" target="_blank" rel="noopener noreferrer">ChatGPT 설정에서 사용량·앱 권한 관리</a></p>
<small>OpenAI 공식 화면에서 직접 로그인합니다. 이 화면은 비밀번호·인증 코드를 받지 않습니다. 기존 Codex 계정은 자동으로 가져오지 않습니다.</small></section>
<section><h2>2. 모델별 동일 기준 평가</h2><label>모델 <select id="models" aria-label="모델 선택"></select></label>
<label>추론 수준 <select id="effort"><option value="medium">medium</option><option value="max">max</option><option value="high">high</option><option value="low">low</option></select></label>
<button id="run">100편 평가 시작·재개</button><p id="progressText"></p><progress id="progress" value="0" max="100"></progress>
<p class="muted">단독·혼합은 동일한 모델 응답을 사용합니다. 혼합은 원문·ID·의미 검증을 적용합니다. 실패 논문을 제외해 완료로 표시하지 않습니다.</p>
<div id="results"></div></section><p id="notice" role="status"></p>
<small>반복 노출된 개발 자료의 비교입니다. 독립 최종 성능·영양학 정확도는 미측정이며 기본 엔진은 heuristic-v1입니다.</small></main>
<script nonce="__NONCE__">
const key=__KEY__; const el=id=>document.getElementById(id);let loadedAccount=null;
async function call(path,data){const r=await fetch(path,{method:data?'POST':'GET',headers:{'X-Companion-Key':key,...(data?{'Content-Type':'application/json'}:{})},body:data?JSON.stringify(data):undefined});const v=await r.json();if(!r.ok)throw Error(v.detail||'요청 실패');return v;}
async function action(path,data){try{el('notice').textContent='';const v=await call(path,data);if(v.message)el('notice').textContent=v.message;await refresh();}catch(e){el('notice').textContent=e.message;}}
function option(select,value,label){const o=document.createElement('option');o.value=value;o.textContent=label;select.append(o);}
async function refresh(){try{const s=await call('/status');const a=s.auth.accounts.find(a=>a.registration_id===s.auth.active_registration_id);el('account').textContent=a?`${a.label} · ${a.plan_usage_enabled?'요금제 사용 권한 확인':'요금제 사용 권한 필요'}`:'연결된 계정이 없습니다. 다른 계정으로 로그인할 수 있습니다.';const picked=el('accounts').value;el('accounts').replaceChildren();s.auth.accounts.forEach(a=>option(el('accounts'),a.registration_id,a.label));if(picked)el('accounts').value=picked;
el('connect').disabled=s.running||s.sign_in_pending;el('select').disabled=s.running;el('logout').disabled=s.running;el('run').disabled=s.running||!a?.plan_usage_enabled;el('progressText').textContent=s.running?'평가 진행 중 · 완료 응답을 논문별로 저장합니다.':s.message||'평가를 시작하거나 저장된 지점부터 재개할 수 있습니다.';
if(a?.plan_usage_enabled&&loadedAccount!==a.registration_id){const list=await call('/models');el('models').replaceChildren();list.models.forEach(m=>option(el('models'),m.slug,m.display_name));loadedAccount=a.registration_id;}
el('results').replaceChildren();let complete=0;for(const x of s.experiments){complete+=x.completed_papers||0;const p=document.createElement('p');const label={running:'진행 중',evaluated:'평가 완료',ready_to_score:'점수 산출 대기',awaiting_authenticated_inference:'실행 대기'}[x.state]||x.state;p.textContent=`${x.model} (${x.reasoning_effort}) · 응답 수신 ${x.completed_responses}/100편 · 추출 정리 ${x.completed_papers}/100편 · ${label}`;el('results').append(p);if(x.metrics){const t=document.createElement('table');const tr=document.createElement('tr');['구성','정탐','오탐','누락','정밀도','재현율','F1'].forEach(v=>{const c=document.createElement('th');c.textContent=v;tr.append(c)});t.append(tr);for(const [name,value]of Object.entries(x.metrics)){const m=value.relation;const r=document.createElement('tr');[{existing_algorithm:'기존 알고리즘',chatgpt_only:'ChatGPT 단독',hybrid:'혼합'}[name]||name,m.tp,m.fp,m.fn,...['precision','recall','f1'].map(k=>m[k]===null?'미정의':(100*m[k]).toFixed(2)+'%')].forEach(v=>{const c=document.createElement('td');c.textContent=v;r.append(c)});t.append(r)}el('results').append(t)}}el('progress').max=Math.max(100,s.experiments.length*100);el('progress').value=complete;
}catch(e){el('notice').textContent=e.message;}}
el('connect').onclick=()=>action('/connect',{});el('select').onclick=()=>{loadedAccount=null;action('/select',{registration_id:el('accounts').value})};el('logout').onclick=()=>action('/logout',{registration_id:el('accounts').value});el('run').onclick=()=>action('/compare',{model:el('models').value,reasoning_effort:el('effort').value});refresh();setInterval(refresh,5000);
</script></html>"""


def create_companion(home: Path, output_root: Path, *, auth=None, port=8520):
    from .chatgpt_evaluation import prepare_experiment, run_experiment, status_experiment
    from .chatgpt_rescore import score_corrected_experiment
    home, output_root = Path(home).resolve(), Path(output_root).resolve()
    auth = auth or ChatGPTAuth()
    secret, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    origin = f"http://127.0.0.1:{port}"
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    state = {"running": False, "sign_in_pending": False, "message": ""}
    lock = threading.Lock()

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if request.headers.get("host") != f"127.0.0.1:{port}":
            return JSONResponse({"detail": "로컬 접속 주소를 확인하세요."}, status_code=403)
        if request.headers.get("origin") not in (None, origin) or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "다른 사이트의 요청은 허용하지 않습니다."}, status_code=403)
        if request.url.path != "/" and not secrets.compare_digest(request.headers.get("X-Companion-Key", ""), secret):
            return JSONResponse({"detail": "로컬 연결 화면에서 요청하세요."}, status_code=403)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; frame-ancestors 'none'; base-uri 'none'"})
        return response

    @app.get("/", response_class=HTMLResponse)
    def page():
        return PAGE.replace("__NONCE__", nonce).replace("__KEY__", json.dumps(secret))

    @app.get("/status")
    def status():
        experiments = []
        busy = state["running"] or batch_busy(output_root)
        for path in sorted(output_root.glob("*/contract.json")):
            contract = json.loads(path.read_bytes())
            item = {"model": contract["model"], "reasoning_effort": contract.get("reasoning_effort"),
                    "completed_responses": len(list((path.parent / "responses").glob("*.json"))),
                    **status_experiment(path.parent)}
            if busy and item["completed_papers"] < 100:
                item["state"] = "running"
            experiments.append(item)
        return {"auth": auth.status(), "experiments": experiments, **state,
                "running": busy}

    @app.get("/models")
    def models():
        try:
            return {"models": available_models(auth)}
        except (ChatGPTAuthError, ChatGPTRequestError) as error:
            raise HTTPException(400, str(error)) from None

    @app.post("/connect")
    def connect():
        with lock:
            if state["running"] or state["sign_in_pending"] or batch_busy(output_root):
                raise HTTPException(409, "현재 작업이 끝난 뒤 계정을 연결하세요.")
            state["sign_in_pending"] = True
        try:
            attempt = auth.start_sign_in(timeout=1800)
        except ChatGPTAuthError as error:
            state["sign_in_pending"] = False
            raise HTTPException(400, str(error)) from None

        def finish_login():
            try:
                attempt.wait(1805)
                state["message"] = "계정이 연결됐습니다. 모델을 선택해 평가를 실행할 수 있습니다."
            except ChatGPTAuthError as error:
                state["message"] = str(error)
            finally:
                state["sign_in_pending"] = False
        threading.Thread(target=finish_login, daemon=True).start()
        return {"message": "공식 로그인 화면에서 다른 계정과 요금제 사용 권한을 선택하세요."}

    @app.post("/select")
    async def select(request: Request):
        body = await request.json()
        def change():
            with lock:
                if state["running"] or state["sign_in_pending"] or batch_busy(output_root):
                    raise HTTPException(409, "평가 중에는 계정을 바꿀 수 없습니다.")
                try:
                    auth.select_account(body.get("registration_id"))
                except ChatGPTAuthError as error:
                    raise HTTPException(400, str(error)) from None
                return {"selected": True}
        return await asyncio.to_thread(change)

    @app.post("/logout")
    async def logout(request: Request):
        body = await request.json()
        def disconnect():
            with lock:
                if state["running"] or state["sign_in_pending"] or batch_busy(output_root):
                    raise HTTPException(409, "평가 중에는 연결을 해제할 수 없습니다.")
                try:
                    result = auth.sign_out(body.get("registration_id"))
                except ChatGPTAuthError as error:
                    raise HTTPException(400, str(error)) from None
                result["message"] = "연결을 해제했습니다." if result["remote_revocation_confirmed"] else "로컬 연결을 해제했습니다. 원격 해제는 확인되지 않아 ChatGPT 설정에서 확인해야 합니다."
                return result
        return await asyncio.to_thread(disconnect)

    @app.post("/compare")
    async def compare(request: Request):
        body = await request.json()
        model, effort = body.get("model"), body.get("reasoning_effort")
        if effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise HTTPException(400, "지원하는 추론 수준을 선택하세요.")
        with lock:
            if state["running"] or state["sign_in_pending"] or batch_busy(output_root):
                raise HTTPException(409, "이미 작업이 실행 중입니다.")
            profile = auth.status()["active_registration_id"]
            state["running"], state["message"] = True, ""
        try:
            catalog = await asyncio.to_thread(available_models, auth, profile)
            if model not in {m["slug"] for m in catalog}:
                raise HTTPException(400, "연결 계정의 모델을 선택하세요.")
        except (ChatGPTAuthError, ChatGPTRequestError) as error:
            state["running"] = False
            raise HTTPException(400, str(error)) from None
        except Exception:
            state["running"] = False
            raise

        def worker():
            from .contract_v7 import sha
            target = output_root / (model + "-" + effort + "-" + sha(model + ":" + effort)[:8])
            try:
                with batch_lock(output_root):
                    prepare_experiment(home, home / "baseline", target, model,
                        dictionary_path=home / "official-id-dictionary.json.gz", reasoning_effort=effort)
                    result = run_experiment(target, ChatGPTPlanClient(auth, model, profile, reasoning_effort=effort))
                    if result["completed_papers"] != 100:
                        state["message"] = "요청이 중단됐습니다. 같은 모델·설정으로 다시 실행하면 저장된 지점부터 재개합니다."
                        return
                    score_corrected_experiment(target, home)
                    state["message"] = "100편의 동일 기준 비교가 완료됐습니다."
            except (ChatGPTAuthError, ChatGPTRequestError) as error:
                state["message"] = str(error)
            except Exception:
                state["message"] = "평가를 완료하지 못했습니다. 원본 결과를 보존했으며 실행 기록을 확인해야 합니다."
            finally:
                state["running"] = False
        threading.Thread(target=worker, name="chatgpt-comparison", daemon=True).start()
        return {"started": True}

    return app
