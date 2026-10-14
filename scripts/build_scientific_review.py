"""Prepare hash-bound human review and primary-paper fact candidates, never approvals."""
import argparse
import json
from pathlib import Path

from nutrigenomics.explorer_build import decode
from nutrigenomics.publication import portable, record_hash
from nutrigenomics.source_updates import atomic_json


def build(audit, output):
    data = json.loads(Path(audit).read_text(encoding="utf-8"))
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    records = [dict(r, value=decode(r["metadata_json"])) for r in data["science_records"]]
    studies = {r["value"]["id"]: r["value"] for r in records if r["kind"] == "study"}
    series_studies = {s: study for study in studies.values() for s in study.get("metadata", {}).get("series", [])}
    decisions, facts = [], []
    fields = {"study": ["title", "independent_study"],
              "exposure": ["amount", "unit", "dose_basis", "duration_days", "formulation"],
              "contrast": ["design", "tissue", "n_people", "n_samples", "response_time_h", "comparison"]}
    for row in records:
        value = row["value"]
        if row["kind"] not in {"study", "exposure", "arm", "contrast", "assertion", "analysis_run"}:
            continue
        study = studies.get(value.get("study_id", value.get("id")), {})
        if not study:
            study = series_studies.get(value.get("series_accession"), {})
        sources = study.get("metadata", {}).get("primary_sources", [])
        primary = next((s["url"] for s in sources if s.get("label") == "Primary publication"), None)
        decisions.append({"record_id": row["id"], "record_kind": row["kind"],
                          "record_sha256": record_hash(value), "source_url": primary,
                          "primary_paper_location": None, "decision": "pending", "reviewer": None,
                          "reviewed_at": None, "reason": None, "record": portable(value)})
        for field in fields.get(row["kind"], []):
            facts.append({"fact_id": row["kind"] + ":" + row["id"] + ":" + field,
                          "record_id": row["id"], "record_sha256": record_hash(value),
                          "field": field, "candidate_value": value.get(field), "source_url": primary,
                          "primary_paper_location": None, "expert_verified": False,
                          "review_status": "pending", "source_kind": "paper_and_repository_candidate"})
    # Round-robin field strata avoids 200 metadata facts from one large series.
    strata = {}
    for fact in facts:
        strata.setdefault(fact["field"], []).append(fact)
    selected = []
    while len(selected) < 200 and any(strata.values()):
        for group in strata.values():
            if group and len(selected) < 200:
                selected.append(group.pop(0))
    if len(selected) != 200:
        raise ValueError("insufficient distinct scientific fact candidates")
    atomic_json(output / "scientific-review.json", decisions)
    atomic_json(output / "primary-paper-facts-200.json", selected)
    atomic_json(output / "manifest.json", {"records": len(decisions), "fact_candidates": len(selected),
                "expert_verified": 0, "human_reviewers_available": 1,
                "inter_rater_agreement_applicable": False, "sealed_query_answers_accessed": False})
    encoded = json.dumps(decisions, ensure_ascii=False).replace("<", "\\u003c")
    template = '''<!doctype html><html lang="ko"><meta charset="utf-8"><title>NutriOmics 과학적 검수</title>
<style>body{font:15px system-ui;color:#17242d;background:#f5f7f9;margin:32px}header,main{max-width:1200px;margin:auto}main{display:grid;grid-template-columns:300px 1fr;gap:24px}h1{font-size:26px}select,input,textarea,button{font:inherit;padding:10px;border:1px solid #bbc9d0;border-radius:4px}select{width:100%;height:600px;background:white}article{padding:24px;background:white;border:1px solid #dbe2e6}pre{white-space:pre-wrap;font-size:12px;max-height:380px;overflow:auto}textarea{width:95%;height:65px}.controls{display:grid;gap:12px}button{background:#153c52;color:white;cursor:pointer}p{line-height:1.6}</style>
<header><h1>NutriOmics 원문 검수</h1><p>기록별 원 논문·시료 대응·비교 조건을 확인한 뒤 판정하세요. 자동 검사와 별도의 과학적 검수입니다. 내용이 바뀌면 SHA256가 달라져 이 판정은 무효화됩니다. 일괄 승인 기능은 없습니다.</p><p id="progress"></p><p>검수자 <input id="reviewer" placeholder="실제 검수자 이름"> <button id="download">판정 JSON 저장</button></p></header>
<main><select id="list" size="25" aria-label="검수할 기록"></select><article><h2 id="title"></h2><a id="source" target="_blank" rel="noopener">원문 후보</a><pre id="record"></pre><div class="controls"><input id="location" placeholder="원 논문 페이지·Methods 문단·표 또는 시료표 위치"><textarea id="reason" placeholder="판정 근거 또는 제외 이유"></textarea><button id="accept">확인·수록</button><button id="exclude">제외 결정</button><button id="pending">미확인으로 유지</button></div></article></main>
<script>const rows=__DATA__;const $=id=>document.getElementById(id);let index=0;function render(){ $('list').replaceChildren(...rows.map((r,i)=>{const o=document.createElement('option');o.value=i;o.textContent=(r.decision==='pending'?'○':'✓')+' '+r.record_kind+' · '+r.record_id;return o}));$('list').value=index;const r=rows[index];$('title').textContent=r.record_kind+' / '+r.record_id;$('record').textContent=JSON.stringify(r.record,null,2);$('source').href=r.source_url||'#';$('source').textContent=r.source_url?'원문 후보 열기':'원문 링크 미확인';$('location').value=r.primary_paper_location||'';$('reason').value=r.reason||'';$('progress').textContent=rows.filter(r=>r.decision!=='pending').length+' / '+rows.length+' 판정 완료';}function decide(d){const who=$('reviewer').value.trim(),loc=$('location').value.trim(),reason=$('reason').value.trim();if(d!=='pending'&&(!who||!loc||!reason)){alert('검수자, 원문 위치, 이유를 모두 입력하세요.');return}Object.assign(rows[index],{decision:d,reviewer:d==='pending'?null:who,primary_paper_location:loc||null,reason:reason||null,reviewed_at:d==='pending'?null:new Date().toISOString()});render()}$('list').onchange=e=>{index=Number(e.target.value);render()};for(const d of ['accept','exclude','pending'])$(d).onclick=()=>decide(d);$('download').onclick=()=>{const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(rows,null,2)],{type:'application/json'}));a.download='scientific-review-decisions.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)};render()</script></html>'''
    (output / "scientific-review.html").write_text(template.replace("__DATA__", encoded), encoding="utf-8")
    return {"records": len(decisions), "facts": len(selected), "expert_verified": 0}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--audit", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(build(a.audit, a.output)))
