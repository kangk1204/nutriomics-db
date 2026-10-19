"""Release-bound reporting and review preparation; never infer human approval.

This module reads immutable release records. It deliberately does not read the
sealed retrieval evaluation or turn computational checks into scientific review.
"""
from __future__ import annotations

import csv
import hashlib
import html
import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "nutriomics-submission-report-v1"
REVIEW_KINDS = {"study", "exposure", "arm", "contrast", "assertion", "analysis_run",
                "clinical_endpoint_result", "clinical_study_audit"}
DECISIONS = {"pending", "accept", "correct", "exclude", "unconfirmed"}
HUMAN_DECISIONS = DECISIONS - {"pending"}
AI_NAMES = re.compile(r"\b(ai|chatgpt|gpt[- ]?\d*|codex|claude|gemini|assistant|automated)\b", re.IGNORECASE)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _decode(value: Any) -> dict:
    result = json.loads(value) if isinstance(value, str) else value
    if not isinstance(result, dict):
        raise TypeError("Record metadata must be an object")
    return result


def read_science_records(source: Any) -> list[dict]:
    """Accept a read-only SQLite connection/path, a snapshot object, or rows."""
    if isinstance(source, sqlite3.Connection):
        rows = [dict(zip(("id", "kind", "metadata_json"), row)) for row in source.execute(
            "SELECT id,kind,metadata_json FROM science_record ORDER BY kind,id")]
    elif isinstance(source, (str, Path)):
        path = Path(source)
        if path.suffix == ".json":
            return read_science_records(json.loads(path.read_text(encoding="utf-8")))
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            return read_science_records(db)
    elif isinstance(source, dict):
        return read_science_records(source["science_records"])
    else:
        rows = list(source)
    result, seen = [], set()
    for row in rows:
        key = (str(row["kind"]), str(row["id"]))
        if key in seen:
            raise ValueError("Duplicate science record identity")
        seen.add(key)
        value = _decode(row.get("metadata_json", row.get("record", row.get("value"))))
        result.append({"record_id": key[1], "record_kind": key[0],
                       "record_sha256": digest(value), "record": value})
    return sorted(result, key=lambda r: (r["record_kind"], r["record_id"]))


def _primary_sources(study: dict) -> list[dict]:
    meta = study.get("metadata", {})
    sources = meta.get("primary_sources", study.get("primary_sources", []))
    if isinstance(sources, dict):
        sources = [sources]
    return [s for s in sources if isinstance(s, dict) and s.get("url")]


def prepare_review_records(source: Any) -> list[dict]:
    records = read_science_records(source)
    studies = {str(r["record"].get("id", r["record_id"])): r["record"]
               for r in records if r["record_kind"] == "study"}
    series = {}
    for study in studies.values():
        for accession in study.get("metadata", {}).get("series", []):
            series[accession] = study
    result = []
    for row in records:
        if row["record_kind"] not in REVIEW_KINDS:
            continue
        value = row["record"]
        study = studies.get(str(value.get("study_id", value.get("id"))), {})
        study = study or series.get(value.get("series_accession"), {})
        sources = _primary_sources(study)
        if isinstance(value.get("source_references"), list):
            sources = [s for s in value["source_references"] if isinstance(s, dict) and s.get("url")] + sources
        if isinstance(value.get("source_urls"), list):
            sources = [{"label": "Declared audit source", "url": url} for url in value["source_urls"]] + sources
        clinical_source = value.get("source", {})
        if isinstance(clinical_source, dict) and clinical_source.get("url"):
            sources = [clinical_source, *sources]
        result.append({**row, "source_references": sources,
            "study_registry_id": value.get("study_id", study.get("id")),
            "source_url": next((s["url"] for s in sources if s.get("label") == "Primary publication"),
                               sources[0]["url"] if sources else None),
            "primary_paper_location": None, "decision": "pending", "reviewer": None,
            "reviewer_type": None, "reviewer_attestation": False,
            "reviewed_at": None, "reason": None, "proposed_correction": None,
            "review_role": "human_scientific_review_required"})
        result[-1]["source_location_candidate"] = value.get("source_location")
        result[-1]["source_reference_type"] = (
            "repository_only_primary_publication_unconfirmed"
            if result[-1]["source_url"] and any(t in result[-1]["source_url"] for t in ("ncbi.nlm.nih.gov/geo/", "ebi.ac.uk/biostudies/"))
            else "publication_reference_candidate" if result[-1]["source_url"] else "source_reference_missing")
    return result


def validate_review_decisions(source: Any, decisions: list[dict]) -> dict:
    """Validate identity and explicit human attestation, not reviewer identity proof.

    A human declaration is auditable but cannot authenticate who used an offline
    file. The final author remains responsible for checking this declaration.
    """
    expected = {(r["record_kind"], r["record_id"]): r for r in prepare_review_records(source)}
    seen, counts = set(), Counter()
    for row in decisions:
        key = (row.get("record_kind"), str(row.get("record_id")))
        if key in seen or key not in expected:
            raise ValueError("Unknown or duplicate review identity")
        seen.add(key)
        if row.get("record_sha256") != expected[key]["record_sha256"]:
            raise ValueError("Review refers to stale or changed record")
        if "record" in row and digest(row["record"]) != row["record_sha256"]:
            raise ValueError("Review embedded record content has changed")
        decision = row.get("decision")
        if decision not in DECISIONS:
            raise ValueError("Unknown review decision")
        if decision in HUMAN_DECISIONS:
            reviewer = row.get("reviewer")
            if row.get("reviewer_type") != "human" or row.get("reviewer_attestation") is not True:
                raise ValueError("Only explicitly attested human decisions can be recorded")
            if not isinstance(reviewer, str) or not reviewer.strip() or AI_NAMES.search(reviewer):
                raise ValueError("A named human reviewer is required")
            if any(not isinstance(row.get(k), str) or not row[k].strip()
                   for k in ("reviewed_at", "primary_paper_location", "reason")):
                raise ValueError("Review date, source location and reason are mandatory")
            try:
                timestamp = datetime.fromisoformat(row["reviewed_at"])
            except ValueError as exc:
                raise ValueError("Review timestamp must be ISO-8601") from exc
            if timestamp.tzinfo is None or timestamp > datetime.now(UTC):
                raise ValueError("Review timestamp must have a timezone and not be in the future")
            if decision == "correct" and not row.get("proposed_correction"):
                raise ValueError("Correction decision requires a proposed correction")
        elif row.get("reviewer_attestation") is True:
            raise ValueError("Pending records cannot carry an approval attestation")
        counts[decision] += 1
    counts["pending"] += len(expected) - len(seen)
    return {"expected_records": len(expected), "submitted_records": len(seen),
            "decisions": dict(counts), "expert_approved": counts["accept"],
            "review_complete": counts["pending"] == 0,
            "identity_authentication": "offline_human_self_declaration_not_independent_verification"}


def build_review_package(source: Any, output: str | Path, sources: Any = None,
                         decisions: list[dict] | None = None) -> dict:
    """Prepare every final-release record afresh; sources is optional audit evidence."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    rows = prepare_review_records(source)
    review_summary = validate_review_decisions(source, decisions or [])
    if decisions:
        saved = {(r["record_kind"], str(r["record_id"])): r for r in decisions}
        for row in rows:
            previous = saved.get((row["record_kind"], row["record_id"]))
            if previous:
                for key in ("decision", "reviewer", "reviewer_type", "reviewer_attestation", "reviewed_at",
                            "primary_paper_location", "reason", "proposed_correction"):
                    row[key] = previous.get(key)
    _json(output / "scientific-review.json", rows)
    facts = _fact_candidates(rows)
    _json(output / "primary-paper-fact-candidates.json", facts)
    _json(output / "source-check-audit.json", {"audit_actor": "automated_registry_source_audit",
        "scope": "Declared links only; full-paper factual agreement not established",
        "by_reference_type": dict(Counter(r["source_reference_type"] for r in rows)),
        "unconfirmed_primary_papers": [{k: r[k] for k in ("record_kind", "record_id", "source_url", "source_reference_type")}
            for r in rows if r["record_kind"] == "study" and r["source_reference_type"] != "publication_reference_candidate"],
        "expert_approved": 0, "sealed_query_answers_accessed": False})
    manifest = {"schema_version": "nutriomics-human-review-v2", "records": len(rows),
        "by_kind": dict(Counter(r["record_kind"] for r in rows)),
        "record_set_sha256": digest([[r["record_kind"], r["record_id"], r["record_sha256"]] for r in rows]),
        "expert_verified": review_summary["expert_approved"],
        "expert_review_status": "decisions_recorded" if review_summary["review_complete"] else "pending",
        "primary_paper_fact_candidates": len(facts),
        "sealed_query_answers_accessed": False,
        "review_identity_limit": "Offline human self-declaration; not authenticated independent review"}
    if sources is not None:
        manifest["source_audit_references"] = sources
    _json(output / "manifest.json", manifest)
    (output / "scientific-review.html").write_text(_review_html(rows), encoding="utf-8")
    return manifest


def _fact_candidates(rows: list[dict], limit: int = 200) -> list[dict]:
    """Stratified registry facts, expressly not source-verified gold answers."""
    fields = {"study": ["title", "metadata.study_design", "metadata.clinical_cohort_people"],
        "exposure": ["amount", "unit", "dose_basis", "duration_days", "formulation"],
        "arm": ["label", "exposure_ids", "allocation", "n_people"],
        "assertion": ["assertion", "layer", "organism", "inference", "limitations"],
        "contrast": ["design", "tissue", "n_people", "n_samples", "response_time_h", "comparison"],
        "analysis_run": ["analysis_version", "metadata.normalization", "metadata.design_formula",
            "metadata.multiple_testing", "metadata.effect_uncertainty", "metadata.independent_people", "metadata.sample_count"],
        "clinical_endpoint_result": ["endpoint", "comparison", "n_people", "n_people_scope", "observations",
            "statistical_tests", "source_location", "source_references", "clinical_effect_estimate"],
        "clinical_study_audit": ["study_id", "status"]}
    groups = {}
    for row in rows:
        for field in fields.get(row["record_kind"], []):
            value = row["record"]
            for part in field.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            fact = {"fact_id": row["record_kind"] + ":" + row["record_id"] + ":" + field,
                "record_kind": row["record_kind"], "record_id": row["record_id"],
                "record_sha256": row["record_sha256"], "field": field, "candidate_value": value,
                "study_registry_id": row["study_registry_id"],
                "candidate_value_status": "not_recorded" if value is None else "registry_value_candidate",
                "source_url": row["source_url"], "primary_paper_location": None,
                "candidate_basis": "actual_release_record_not_paper_verified_gold",
                "expert_verified": False, "review_status": "pending"}
            groups.setdefault(row["study_registry_id"] or "unresolved_study", {}).setdefault(field, []).append(fact)
    # Balance studies first, then fields within each study. Large contrast tables
    # cannot crowd all later studies out of the finite review-candidate set.
    selected, positions = [], Counter()
    while len(selected) < limit:
        progressed = False
        for study_id, strata in groups.items():
            keys = list(strata)
            for offset in range(len(keys)):
                index = (positions[study_id] + offset) % len(keys)
                group = strata[keys[index]]
                if group and len(selected) < limit:
                    selected.append(group.pop(0))
                    positions[study_id] = index + 1
                    progressed = True
                    break
        if not progressed:
            break
    return selected


def _review_html(rows: list[dict]) -> str:
    # All source content is assigned through textContent; embedded JSON cannot end a script.
    data = canonical(rows).replace("<", "\\u003c")
    record_set = digest([[r["record_kind"], r["record_id"], r["record_sha256"]] for r in rows])
    return '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>NutriOmics 과학적 검수</title>
<style>body{margin:32px auto;padding:0 24px;max-width:1280px;font:15px/1.6 system-ui;color:#172b3a;background:#f5f7f8}h1{font-size:28px}main{display:grid;grid-template-columns:340px 1fr;gap:24px}article{background:white;padding:24px;border:1px solid #ccd5da}select,textarea,input,button{font:inherit;padding:9px;border:1px solid #acbcc7;border-radius:3px}#records{width:100%;height:650px}pre{font-size:12px;white-space:pre-wrap;max-height:360px;overflow:auto}textarea{display:block;width:95%;height:65px;margin:8px 0}button{cursor:pointer;background:#174761;color:white;margin:6px 6px 6px 0}label{display:block}a{color:#075778}@media(max-width:760px){main{display:block}#records{height:200px}}</style>
<h1>원문 기반 과학적 검수</h1><p>각 기록의 원문 위치·비교 조건·단위를 확인해 판정합니다. 자동 점검은 과학적 승인이 아닙니다. 수정된 내용은 새 release에서 새 해시로 다시 검수합니다. 이 파일은 검수자의 자기 선언을 저장하며 신원을 인증하지 않습니다.</p><p id="progress"></p>
<label>검수자 <input id="reviewer" autocomplete="name"></label><label><input type="checkbox" id="attest"> 본인이 원문을 확인한 사람 검수자임을 확인합니다.</label><button id="download">판정 JSON 저장</button><label>저장한 판정 JSON 불러오기 <input type="file" id="resume" accept="application/json"></label><p id="saveStatus">같은 기록 집합의 판정은 이 브라우저에 자동 저장됩니다. JSON 파일도 별도로 보관하세요.</p><main><select id="records" aria-label="검수 기록"></select><article><h2 id="heading"></h2><p id="sourceType"></p><p id="sources"></p><pre id="payload"></pre><label>원문 위치<input id="location" style="width:95%" placeholder="논문 표·절·페이지 또는 보충 시료표"></label><label>판정 이유<textarea id="reason"></textarea></label><label>수정 제안 (수정 판정 시 필수)<textarea id="correction"></textarea></label><div id="buttons"></div></article></main>
<script>const rows=__DATA__, $=id=>document.getElementById(id), storageKey='nutriomics-review-__SET__';let current=0;const labels={pending:'대기',accept:'확인·수록',correct:'수정 필요',exclude:'제외',unconfirmed:'미확인'};
const fields=['decision','reviewer','reviewer_type','reviewer_attestation','reviewed_at','primary_paper_location','reason','proposed_correction'];
function savedRows(){return rows.map(r=>Object.fromEntries(['record_kind','record_id','record_sha256',...fields].map(k=>[k,r[k]])))}
function save(){try{localStorage.setItem(storageKey,JSON.stringify(savedRows()));$('saveStatus').textContent='이 기록 집합의 판정을 브라우저에 자동 저장했습니다. JSON 파일도 별도로 보관하세요.'}catch(e){$('saveStatus').textContent='브라우저 자동 저장을 사용할 수 없습니다. 판정 JSON을 저장하세요.'}}
function applySaved(saved){if(!Array.isArray(saved))throw Error('판정 배열이 아닙니다.');const seen=new Set(),known=new Map(rows.map(r=>[r.record_kind+'::'+r.record_id,r]));for(const s of saved){const key=s.record_kind+'::'+s.record_id,original=known.get(key);if(!original||seen.has(key)||original.record_sha256!==s.record_sha256)throw Error('알 수 없는 기록, 중복 또는 변경된 SHA256입니다.');seen.add(key);if(!Object.hasOwn(labels,s.decision))throw Error('판정 종류를 확인하세요.');if(s.decision!=='pending'){if(s.reviewer_type!=='human'||s.reviewer_attestation!==true||!s.reviewer||/\\b(ai|chatgpt|codex|claude|gemini|assistant|automated)\\b/i.test(s.reviewer)||!s.primary_paper_location||!s.reason||!s.reviewed_at||!Number.isFinite(Date.parse(s.reviewed_at)))throw Error('사람 검수자·원문 위치·이유·날짜 확인이 필요합니다.');if(s.decision==='correct'&&!s.proposed_correction)throw Error('수정 제안이 없습니다.');}else if(s.reviewer_attestation===true)throw Error('대기 기록에 승인 선언이 있습니다.');}for(const s of saved){const original=known.get(s.record_kind+'::'+s.record_id);for(const k of fields)original[k]=s[k]??null}const reviewer=saved.find(s=>s.reviewer);if(reviewer)$('reviewer').value=reviewer.reviewer;}
function render(){const selected=current;$('records').replaceChildren(...rows.map((r,i)=>{const o=document.createElement('option');o.value=i;o.textContent=labels[r.decision]+' · '+r.record_kind+' · '+r.record_id;return o}));$('records').value=selected;const r=rows[current];$('heading').textContent=r.record_kind+' / '+r.record_id;$('sourceType').textContent=r.source_reference_type==='repository_only_primary_publication_unconfirmed'?'저장소 링크만 확인됨: 원 논문 연결·원문 검수 필요':r.source_reference_type==='source_reference_missing'?'원문 출처 미확인':'논문 링크 후보: 원문 내용 검수 필요';$('payload').textContent=JSON.stringify(r.record,null,2);$('location').value=r.primary_paper_location||'';$('reason').value=r.reason||'';$('correction').value=r.proposed_correction||'';$('sources').replaceChildren(...r.source_references.filter(s=>/^https?:\\/\\//.test(s.url)).map(s=>{const a=document.createElement('a');a.href=s.url;a.target='_blank';a.rel='noopener';a.textContent=(s.label||'원문')+' ';return a}));$('progress').textContent=rows.filter(r=>r.decision!=='pending').length+' / '+rows.length+' 판정 기록';}
function decide(d){const who=$('reviewer').value.trim(),loc=$('location').value.trim(),reason=$('reason').value.trim();if(d!=='pending'&&(!who||!loc||!reason||!$('attest').checked)){alert('사람 검수자 확인, 원문 위치, 이유가 필요합니다.');return}if(d==='correct'&&!$('correction').value.trim()){alert('수정 제안을 입력하세요.');return}Object.assign(rows[current],{decision:d,reviewer:d==='pending'?null:who,reviewer_type:d==='pending'?null:'human',reviewer_attestation:d!=='pending',reviewed_at:d==='pending'?null:new Date().toISOString(),primary_paper_location:loc||null,reason:reason||null,proposed_correction:$('correction').value.trim()||null});save();render()}
for(const [d,label]of Object.entries(labels)){const b=document.createElement('button');b.textContent=label;b.onclick=()=>decide(d);$('buttons').append(b)}$('records').onchange=e=>{current=Number(e.target.value);render()};$('download').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(rows,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='scientific-review-decisions.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};$('resume').onchange=async e=>{try{applySaved(JSON.parse(await e.target.files[0].text()));save();render()}catch(error){alert('불러오기 실패: '+error.message)}finally{e.target.value=''}};try{const saved=localStorage.getItem(storageKey);if(saved)applySaved(JSON.parse(saved))}catch(error){$('saveStatus').textContent='자동 저장을 불러오지 못했습니다: '+error.message}render()</script></html>'''.replace("__DATA__", data).replace("__SET__", record_set)


def analysis_audit(source: Any) -> dict:
    """Inspect declared executed metadata. No method validity or approvals inferred."""
    rows, findings = [], []
    for row in read_science_records(source):
        if row["record_kind"] != "analysis_run":
            continue
        run = row["record"]
        meta = run.get("metadata", {})
        text = canonical(meta).lower()
        method = {k: meta.get(k) for k in (
            "normalization", "design", "design_formula", "person_block", "block_variable",
            "multiple_testing", "effect_uncertainty", "dmr_analysis", "analysis_role",
            "channel_direction", "cell_composition_adjusted", "sample_scope", "design_limit",
            "interpretation_limit", "qc_limit")}
        people, assays = meta.get("independent_people"), meta.get("sample_count")
        check = {"record_id": row["record_id"], "record_sha256": row["record_sha256"],
            "series_accession": run.get("series_accession", meta.get("accession")),
            "analysis_version": run.get("analysis_version"),
            "n_people_model": people, "n_assays_model": assays, "declared_methods": method,
            "source_script_sha256": run.get("source_script_sha256", meta.get("script_sha256")),
            "environment_lock_sha256": run.get("environment_lock_sha256", meta.get("environment_lock_sha256")),
            "input_manifest": run.get("input_manifest", meta.get("input_manifest")),
            "output_manifest": run.get("output_manifest"), "expert_review_status": "pending",
            "audit_actor": "automated_metadata_audit", "findings": []}
        def note(code: str, severity: str, message: str, target=check) -> None:
            item = {"code": code, "severity": severity, "message": message}
            target["findings"].append(item)
            findings.append({"analysis_version": target["analysis_version"], **item})
        if people is None or assays is None:
            note("sample_scope_missing", "review", "Model-level people/assay count not fully declared; do not infer contrast-level counts.")
        if any(token in text for token in ("subject", "person", "paired", "repeated", "participant")):
            check["dependency_structure_declared"] = True
        else:
            check["dependency_structure_declared"] = False
            note("dependency_structure_unconfirmed", "review", "Repeated-person structure requires source and design-matrix review.")
        if not meta.get("multiple_testing"):
            note("fdr_family_missing", "review", "No explicit multiplicity family in execution metadata.")
        elif "within" in str(meta["multiple_testing"]).lower() and not any(
                t in str(meta["multiple_testing"]).lower() for t in ("primary", "across all", "study")):
            note("within_contrast_fdr_only", "review", "Within-contrast FDR is declared; study-wide primary-contrast FDR is not established.")
        if not meta.get("analysis_role"):
            note("analysis_role_not_explicit", "review", "Primary, sensitivity or historical role needs explicit curation; do not infer it from version numbering.")
        methyl = any(t in text for t in ("m-value", "m_values", "methylation", "delta-beta", "fixed1000"))
        if methyl:
            check["methylation_statistical_scale_declared"] = any(t in text for t in ("m-value", "m=log2", "m_value"))
            check["beta_scale_uncertainty_declared"] = bool(meta.get("effect_uncertainty") and "beta" in str(meta["effect_uncertainty"]).lower())
            if not check["beta_scale_uncertainty_declared"]:
                note("beta_uncertainty_scale_unconfirmed", "review", "Do not interpret M-scale SE/CI as beta-scale uncertainty without explicit provenance.")
        region = any(t in text + str(check["analysis_version"]).lower() for t in ("fixed1000", "fixed 1kb", "fixed 1000", "1000bp"))
        check["result_label"] = "fixed_1kb_region_tests_not_validated_dmrs" if region else "feature_level_results"
        if region:
            note("fixed_region_not_discovered_dmr", "interpretation", "Fixed 1 kb region tests are not counts of discovered or validated DMRs.")
        if meta.get("channel_direction"):
            note("two_colour_direction_requires_paper_check", "review", "Preserve declared Cy3/Cy5 direction; verify against source before scientific approval.")
        rows.append(check)
    return {"schema_version": "nutriomics-analysis-metadata-audit-v1", "analyses": rows,
        "analysis_count": len(rows), "findings": findings, "expert_review_status": "pending",
        "sample_count_scope": "model-level counts only; never summed across versions or studies",
        "scientific_approval_generated": False, "sealed_query_answers_accessed": False}


def audit_frozen_producer_sources(source: Any, workspace: str | Path) -> dict:
    """Link metadata to exact producer bytes; source checking is not human approval."""
    workspace = Path(workspace)
    by_hash = {}
    for folder in (workspace / "scripts", workspace / "source_snapshots"):
        if folder.is_dir():
            paths = folder.glob("*.R") if folder.name == "scripts" else folder.rglob("*.R")
            for path in paths:
                by_hash.setdefault(hashlib.sha256(path.read_bytes()).hexdigest(), []).append(path)
    checks = []
    for row in read_science_records(source):
        if row["record_kind"] != "analysis_run":
            continue
        run, facts = row["record"], []
        expected = run.get("source_script_sha256", run.get("metadata", {}).get("script_sha256"))
        matches = sorted(by_hash.get(expected, []), key=lambda p: (len(p.parts), str(p)))
        selected = matches[0] if matches else None
        lines = selected.read_text(encoding="utf-8").splitlines() if selected else []
        method_locations = {}
        for category, pattern in {
            "fdr_and_hypothesis_family": r"p\.adjust|q_value|multiple_testing|study.family|primary.*contrast|padj",
            "repeated_person_and_model": r"duplicateCorrelation|block\s*=|subject_id|person_block|design\s*<-|model\.matrix|paired|lmFit|eBayes|voom\(",
            "effect_and_uncertainty_scale": r"effect_estimate|standard_error|delta_beta|delta_m|beta_fit|beta_summary|confidence|conf\.int|CI_lower|ci_lower",
            "normalization_and_filtering": r"filterByExpr|calcNormFactors|preprocessNoob|rma\(|normalizeWithinArrays|normalization|probe_filter",
        }.items():
            matches_at = [{"line": i + 1, "fragment": line.strip()[:700]}
                          for i, line in enumerate(lines) if re.search(pattern, line, re.IGNORECASE)]
            method_locations[category] = {"locations": matches_at[:30], "matching_lines": len(matches_at),
                "truncated": len(matches_at) > 30,
                "interpretation": "Exact code/embedded metadata locations, not proof that assumptions or contrasts are valid"}
        accession = run.get("series_accession")
        if accession in {"GSE54325", "GSE54643"}:
            for i, line in enumerate(lines):
                if 'q_value = p.adjust(fit$p.value[, 1L], method = "BH")' in line:
                    facts.append({"fact": "BH adjustment across the vector of tested features in this declared contrast",
                        "source_line": i + 1, "source_fragment": line.strip(), "expert_review_status": "pending"})
            if accession == "GSE54643" and any("standard_error = beta_summary$standard_error" in line for line in lines):
                helper_sha = run.get("metadata", {}).get("helper_sha256")
                helpers = sorted(by_hash.get(helper_sha, []), key=lambda p: (len(p.parts), str(p)))
                if helpers:
                    helper = helpers[0]
                    hlines = helper.read_text(encoding="utf-8").splitlines()
                    se_lines = [(i + 1, line.strip()) for i, line in enumerate(hlines)
                                if "standard_error <- sqrt(pmax(0, centered_sum_squares" in line]
                    if se_lines:
                        facts.append({"fact": "Exported SE is the paired-delta-beta summary SD/sqrt(n); M-scale SE has a separate column. No beta-scale limma CI is inferred.",
                            "helper_file": str(helper.relative_to(workspace)), "helper_sha256": helper_sha,
                            "source_line": se_lines[0][0], "source_fragment": se_lines[0][1],
                            "expert_review_status": "pending"})
        checks.append({"record_id": row["record_id"], "record_sha256": row["record_sha256"],
            "series_accession": accession, "analysis_version": run.get("analysis_version"),
            "expected_source_sha256": expected, "exact_source_bytes_present": selected is not None,
            "actual_source_sha256": hashlib.sha256(selected.read_bytes()).hexdigest() if selected else None,
            "source_sha256_match": selected is not None,
            "source_file": str(selected.relative_to(workspace)) if selected else None,
            "source_line_count": len(lines) if selected else None, "source_checked_facts": facts,
            "method_source_locations": method_locations,
            "derived_analysis_metadata": run.get("metadata", {}).get("derived_analysis"),
            "scope_note": "Postprocessing producers may inherit design from a separate parent run; source locations alone do not re-execute that parent"})
    return {"audit_actor": "AI-assisted deterministic source identity and code-location checks",
        "analysis_count": len(checks), "exact_source_matches": sum(r["exact_source_bytes_present"] for r in checks),
        "analyses": checks, "scope": "Metadata/producer identity and explicit code facts, not raw-matrix rerun or scientific approval",
        "expert_review_status": "pending", "sealed_query_answers_accessed": False}


def legacy_claim_ledger(workspace: str | Path) -> list[dict]:
    """Keep historical report text as historical evidence, not original DB proof."""
    workspace = Path(workspace)
    path = workspace / "final_2026/deliverables/전체검증_준비본_20261003_v2.json"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    sha = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    claims = [("legacy_food_count", "55,379", "2024-08", "historical_reported_count_not_current_verified_total"),
              ("legacy_interaction_count", "510,620", "2024-08", "historical_reported_count_not_current_verified_total"),
              ("legacy_markers", "200", None, "original_evidence_not_established"),
              ("legacy_participants", "2,700", None, "original_evidence_not_established"),
              ("legacy_auc", "0.88", None, "historical_claim_not_revalidated"),
              ("legacy_cpg", "cg04983977", None, "annotation_does_not_validate_marker_performance")]
    result = []
    for key, token, date, state in claims:
        needles = [token]
        if key == "legacy_markers": needles = ["200마커", "마커 200", "마커200"]
        if key == "legacy_participants": needles = ["2,700", "2700", "2,700명"]
        matches = [{"line": i + 1, "context": line.strip()[:900]}
                   for i, line in enumerate(lines) if any(t in line for t in needles)][:4]
        result.append({"claim_id": key, "reported_value": token, "reference_date": date,
            "status": state, "source_file": str(path.relative_to(workspace)), "source_sha256": sha,
            "source_type": "prepared_report_secondary_record_not_original_data_proof",
            "source_matches": matches, "current_release_achievement": False,
            "required_evidence": "Official report/original DB extract or model predictions, evaluation split and original analysis; not inferable from graph/effect counts"})
    return result


def _contract_counts(contract: dict) -> dict:
    if contract.get("schema_version") != "nutriomics-release-contract-v1":
        raise ValueError("Canonical release contract is required")
    if not contract.get("release_id") or not isinstance(contract.get("counts"), dict):
        raise ValueError("Contract release_id/counts missing")
    return contract["counts"]


def _database_release_id(source: Any) -> str | None:
    if isinstance(source, sqlite3.Connection):
        tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "meta" not in tables:
            return None
        row = source.execute("SELECT value_json FROM meta WHERE key='release_id'").fetchone()
        return json.loads(row[0]) if row else None
    if isinstance(source, (str, Path)) and Path(source).suffix != ".json":
        with closing(sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)) as db:
            return _database_release_id(db)
    return None


def clinical_record_summary(source: Any) -> dict:
    """Describe the actual transcribed layer without calculating a health effect."""
    rows = read_science_records(source)
    clinical = [r["record"] for r in rows if r["record_kind"] == "clinical_endpoint_result"]
    audits = [r["record"] for r in rows if r["record_kind"] == "clinical_study_audit"]
    endpoint_ids = {r.get("endpoint", {}).get("id") for r in clinical if isinstance(r.get("endpoint"), dict)} - {None}
    result = {"candidate_records": len(clinical), "study_audits": len(audits), "health_endpoint_types": len(endpoint_ids),
        "studies_with_candidate_results": sorted({r["study_id"] for r in clinical}),
        "by_domain": dict(Counter(r.get("endpoint", {}).get("domain", "unknown") for r in clinical)),
        "by_result_type": dict(Counter(r.get("result_type", "unknown") for r in clinical)),
        "by_audit_status": dict(Counter(r.get("status", "unknown") for r in audits)),
        "expert_review_status": "pending", "human_approved_records": 0,
        "coverage_is_exhaustive": False,
        "interpretation_limits": ["Machine-assisted source transcription remains subject to human review",
            "Same-study clinical and omics results do not establish individual linkage, mediation or causality",
            "No effect or CI reconstructed from group summaries or P values"],
        "unit_caveats": [], "statistical_scope_examples": []}
    for row in clinical:
        endpoint = row.get("endpoint", {})
        if endpoint.get("unit_caveat"):
            result["unit_caveats"].append({"record_id": row.get("id"), "study_id": row["study_id"],
                "source_unit": endpoint.get("unit"), "canonical_unit": endpoint.get("canonical_unit"),
                "note": endpoint["unit_caveat"], "source_location": row.get("source_location")})
        if row["study_id"] == "S05" and endpoint.get("id") == "ClinicalEndpoint:body_weight":
            result["statistical_scope_examples"].append({"record_id": row.get("id"), "study_id": "S05",
                "endpoint": endpoint, "n_people_scope": row.get("n_people_scope"),
                "statistical_tests": row.get("statistical_tests", []), "source_location": row.get("source_location"),
                "note": "Omnibus P and adjusted/unadjusted pairwise P are distinct; no randomized dietary control"})
    return result


def comparator_scope_matrix() -> list[dict]:
    """Primary-paper scope review, not a head-to-head performance benchmark."""
    common = {"source_checked_at": "2026-10-19", "source_check_actor": "AI-assisted live primary-paper reading; human confirmation pending",
              "comparison_type": "primary_paper_scope_only",
              "comparative_performance": "not_evaluated", "update_cadence": "not_established_by_this_review",
              "live_service_availability": "not_audited"}
    return [{**common, "resource": "FoodAtlas", "source_url": "https://www.nature.com/articles/s41538-025-00680-9",
        "source_location": "Abstract; Results; Discussion limitations",
        "verified_scope": "음식–화합물 조성, 화합물–질환, 생물활성·풍미를 출처와 연결한 그래프",
        "nutriomics_comparison": "사람 식이 대비·DNAm의 조건 보존을 평가. 서로 다른 계층의 관계 총수를 우열 또는 개인 위험으로 해석하지 않음"},
        {**common, "resource": "Eat4Genes", "source_url": "https://www.frontiersin.org/journals/nutrition/articles/10.3389/fnut.2023.1196520/full",
        "source_location": "Abstract; Methods 2.3–2.6; Results 3.1",
        "verified_scope": "사람 섭취 연구 51개·선택된 위험 유전자 96개. 질환·유전자별 원 연구 발현 결과 탐색",
        "nutriomics_comparison": "음식–유전자 탐색은 선행 기능. 노출량·맥락·전체 검정 결과·DNAm 보존을 비교하며 정확도 우위는 미평가"},
        {**common, "resource": "NutriGenomeDB", "source_url": "https://academic.oup.com/database/article/doi/10.1093/database/baz097/5607505",
        "source_location": "Abstract; Methods Data collection/Data processing; Discussion",
        "verified_scope": "주로 사람 세포 모델의 GEO 영양 관련 발현 시그니처. 유전자 표·시그니처 일치 탐색",
        "nutriomics_comparison": "사람 섭취와 세포 처리를 구분. 분석 키·결측·통계 범위를 보존하며 생물학적 우위를 주장하지 않음"}]


def food_inventory_grain_audit(workspace: str | Path, internal_contract: dict, public_contract: dict) -> dict:
    """Record historical label changes without forcing old totals onto new grains."""
    workspace = Path(workspace)
    prior_path = workspace / "outputs/publication/2026-10-17/extensions/CANDIDATE_INTEGRATION_COMPLETION.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8")) if prior_path.is_file() else {}
    prior_counts = prior.get("validated_food_records", {})
    result = {"metric": "food_inventory_grain_correction", "scope": {},
        "interpretation": "Food identifier nodes, the prior validated_food_records label and DISTINCT measurement.food are different counting grains; a count difference alone does not establish deletion or new data.",
        "prior_source_file": str(prior_path.relative_to(workspace)),
        "prior_source_sha256": hashlib.sha256(prior_path.read_bytes()).hexdigest() if prior_path.is_file() else None,
        "prior_reference_date": str(prior.get("verified_at", ""))[:10] or None}
    for scope, contract in (("internal", internal_contract), ("public", public_contract)):
        counts = contract["counts"]
        foods = counts.get("by_kind", counts.get("node_kinds", {})).get("food")
        backed = counts.get("composition_backed_foods")
        result["scope"][scope] = {"current_release_id": contract["release_id"],
            "food_identifier_nodes": foods, "composition_record_linked_foods": backed,
            "food_nodes_without_composition_records": foods - backed if isinstance(foods, int) and isinstance(backed, int) else None,
            "prior_validated_food_records": prior_counts.get(scope),
            "prior_field_name": "validated_food_records",
            "current_metric_definition": "COUNT(DISTINCT measurement.food), including any recorded measurement state; not necessarily a quantified usable food"}
    return result


def clinical_data_dictionary(source: Any) -> dict:
    """Field definitions for the new JSON subtypes, beyond legacy SQL columns."""
    definitions = {
        "clinical_endpoint_result": {
            "id": "Stable clinical-summary record identifier; not a participant identifier.",
            "study_id": "Same-study registry pointer; does not establish participant-level omics linkage or mediation.",
            "series_accessions": "Related deposited series, not a guarantee that clinical participants match omics participants.",
            "endpoint.id": "Endpoint identity; integrated iAUC and fasting concentration remain different endpoints.",
            "endpoint.domain": "Blood pressure, lipids or obesity domain.",
            "endpoint.unit": "Unit exactly preserved from the source summary.",
            "endpoint.canonical_unit": "Confirmed unit only; null when the source time-integration unit is unresolved.",
            "endpoint.unit_caveat": "Explicit unit ambiguity; no silent concentration-time or mass-volume conversion.",
            "result_type": "Baseline, repeated-group or crossover-iAUC summary; not a causal effect classification.",
            "n_people": "Source-reported cohort/subset count; interpret only with n_people_scope.",
            "n_people_scope": "Clinical cohort/subset, assay distinction, source imputation and observed-n limitations.",
            "comparison": "Original group, time and crossover/order context; no newly reconstructed clinical contrast.",
            "observations[].value": "Published group summary, not an individual measurement or treatment-effect estimate.",
            "observations[].summary_statistic": "Original mean/median/iAUC summary definition.",
            "observations[].dispersion": "Source SD, range or IQR; not automatically an SE or confidence interval.",
            "observations[].observed_n": "Actual non-imputed count per endpoint/time cell when reported; null does not equal cohort n.",
            "observations[].time_days": "Reported elapsed days; null for source summaries that do not define that field.",
            "statistical_tests[].comparison_scope": "Omnibus, pairwise, intervention or carry-over hypothesis tested by the source.",
            "statistical_tests[].p_value": "Source-reported P for this exact comparison and adjustment state.",
            "statistical_tests[].adjusted": "Whether the source reports this P as multiplicity-adjusted; not a new pipeline correction.",
            "statistical_tests[].multiplicity": "Original correction description; omnibus and pairwise families remain distinct.",
            "clinical_effect_estimate": "Null when only source group summaries are available; not reconstructed by subtracting medians.",
            "standard_error": "Reported effect SE only; not generated from SD/range/IQR or M-scale omics uncertainty.",
            "confidence_interval": "Reported effect interval only; not reconstructed from a P value.",
            "missing_reasons": "Field-specific absence/uncertainty explanations, distinct from numeric zero.",
            "source_location": "Original table, row, section or supplement location for review.",
            "source_references": "Versioned source URL, SHA256, attribution and file-specific redistribution terms.",
            "source_check_method": "Source transcription method; machine-assisted checking is not expert approval.",
            "expert_review_status": "Human scientific review state; source_checked does not mean approved.",
            "genomic_mediation_demonstrated": "Whether genomic mediation is established; false is preserved in this candidate layer.",
            "individual_omics_clinical_linkage": "Participant-level clinical/omics linkage status, not inferred from a shared study.",
            "individual_prediction_available": "Whether a validated individual prediction is available; false in this layer.",
            "interpretation_limit": "Record-specific limits of design, units, statistics and biological inference.",
        },
        "clinical_study_audit": {
            "study_id": "Audited study registry identifier, not an independent-cohort count proof.",
            "status": "Quantitative-source availability or unassessed scope; not clinical success/failure.",
            "source_urls": "Declared original-publication or repository access points.",
            "reason": "Why results are candidates, missing, inaccessible or not fully audited.",
            "not_measured": "Affirmative source-supported non-measurement only; null for unassessed outcomes.",
            "candidate_result_count": "Selected transcribed candidate records; zero does not mean no endpoint was measured.",
            "coverage_is_exhaustive": "Whether the selected transcription covers every eligible endpoint; false in this release.",
            "assessed_scope": "Health domains addressed by this availability audit.",
            "audit_date": "Date of this source availability audit.",
            "expert_review_status": "Human review state, distinct from technical reproduction.",
        }}
    rows = read_science_records(source)
    entries = []
    for kind, fields in definitions.items():
        available = [r["record"] for r in rows if r["record_kind"] == kind]
        for field, definition in fields.items():
            def present(value, parts):
                if not parts:
                    return True
                key = parts[0].replace("[]", "")
                if not isinstance(value, dict) or key not in value:
                    return False
                child = value[key]
                if "[]" in parts[0]:
                    return isinstance(child, list) and any(present(item, parts[1:]) for item in child)
                return present(child, parts[1:])
            entries.append({"record_kind": kind, "field": field, "definition": definition,
                "present_in_records": sum(present(value, field.split(".")) for value in available),
                "record_kind_count": len(available)})
    return {"schema_version": "nutriomics-clinical-subtype-dictionary-v1", "definitions": entries,
            "scope": "Selected clinical source-summary and availability-audit fields; complements the SQL table dictionary",
            "expert_review_status": "pending"}


def build_report(*, internal_contract: dict, public_contract: dict, science_source: Any,
                 output: str | Path, workspace: str | Path, validation_receipts: list[dict] | None = None,
                 review_decisions: list[dict] | None = None, clinical_summary: dict | None = None,
                 generated_at: str | None = None) -> dict:
    """Generate a printable Korean evidence report and machine-readable ledgers."""
    ic, pc = _contract_counts(internal_contract), _contract_counts(public_contract)
    snapshot = science_source
    if isinstance(science_source, (str, Path)) and Path(science_source).suffix == ".json":
        snapshot = json.loads(Path(science_source).read_text(encoding="utf-8"))
    if isinstance(snapshot, dict) and snapshot.get("release_id") and snapshot["release_id"] != public_contract["release_id"]:
        raise ValueError("Science snapshot is not from the current public contract release")
    native_id = _database_release_id(science_source)
    if native_id and native_id != public_contract["release_id"]:
        raise ValueError("Science database is not from the current public contract release")
    records = read_science_records(snapshot)
    actual = Counter(r["record_kind"] for r in records)
    expected = pc.get("science_kinds", {})
    if dict(actual) != {k: v for k, v in expected.items() if v}:
        raise ValueError("Public contract science counts differ from actual source records")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    review_package = build_review_package(records_to_snapshot(records), output / "review", decisions=review_decisions)
    review = validate_review_decisions(records_to_snapshot(records), review_decisions or [])
    audits = analysis_audit(records_to_snapshot(records))
    _json(output / "analysis-audit.json", audits)
    producer_audit = audit_frozen_producer_sources(records_to_snapshot(records), workspace)
    _json(output / "producer-source-audit.json", producer_audit)
    legacy = legacy_claim_ledger(workspace)
    _json(output / "legacy-claims.json", legacy)
    generated_at = generated_at or datetime.now(UTC).isoformat()
    ledger = []
    definitions = public_contract.get("count_definitions", {})
    for scope, counts, contract in (("internal", ic, internal_contract), ("public", pc, public_contract)):
        for name in ("composition_backed_foods", "node", "edge", "measurement", "effect", "study_registry_records"):
            value = counts.get(name)
            ledger.append({"claim_id": scope + ":" + name, "scope": scope, "metric": name,
                "value": value, "release_id": contract["release_id"], "status": "measured" if value is not None else "unconfirmed",
                "basis": "actual_immutable_release_canonical_contract", "definition": definitions.get(name),
                "reference_date": generated_at[:10], "not_people_or_efficacy": name in {"edge", "effect"}})
    for name, n in sorted(actual.items()):
        ledger.append({"claim_id": "public:science:" + name, "scope": "public", "metric": name,
            "value": n, "release_id": public_contract["release_id"], "status": "measured",
            "basis": "actual_science_record", "reference_date": generated_at[:10],
            "evidence_review_status": "human_review_pending_or_requires_separate_decision_ledger"})
    receipts = [dict(r) for r in validation_receipts or []]
    for receipt in receipts:
        if not receipt.get("source_file") or not receipt.get("source_sha256") or not receipt.get("reference_date"):
            raise ValueError("Validation receipts require exact source file, hash and reference date")
        if not re.fullmatch(r"[0-9a-f]{64}", receipt["source_sha256"]):
            raise ValueError("Validation receipt SHA256 must be lowercase hexadecimal")
        path = Path(receipt["source_file"])
        if not path.is_absolute():
            path = Path(workspace) / path
        if path.is_file():
            if hashlib.sha256(path.read_bytes()).hexdigest() != receipt["source_sha256"]:
                raise ValueError("Validation receipt content differs from its recorded hash")
            receipt["receipt_file_verification"] = "local_bytes_sha256_verified"
        else:
            receipt["receipt_file_verification"] = "source_file_not_available_here_hash_not_rechecked"
        release_ids = receipt.get("release_ids", [])
        if receipt.get("release_id"):
            release_ids = [*release_ids, receipt["release_id"]]
        receipt["applies_to_current_release"] = bool(set(release_ids) & {
            internal_contract["release_id"], public_contract["release_id"]})
        receipt["release_applicability"] = ("current_release" if receipt["applies_to_current_release"]
                                            else "historical_or_unbound_not_current_pass")
        # A data release can stay fixed while the serving code changes.  Its ID
        # alone must not make an older runtime benchmark a current pass.
        historical_runtime = bool(receipt.get("not_final_runtime_proof") or receipt.get("historical_runtime_source"))
        receipt["runtime_applicability"] = ("historical_runtime_not_current_pass" if historical_runtime
            else "runtime_version_declared" if receipt.get("runtime_source") else "runtime_not_declared")
        receipt["evidence_class"] = receipt.get("evidence_class", "unclassified_receipt_no_scientific_validation_inferred")
        receipt["scientific_validation_inferred"] = False
    actual_clinical = clinical_record_summary(records_to_snapshot(records))
    if clinical_summary:
        actual_clinical["provided_build_receipt"] = clinical_summary
    comparators = comparator_scope_matrix()
    _json(output / "comparison-scope-matrix.json", comparators)
    grains = food_inventory_grain_audit(workspace, internal_contract, public_contract)
    _json(output / "food-inventory-grain-audit.json", grains)
    clinical_dictionary = clinical_data_dictionary(records_to_snapshot(records))
    _json(output / "clinical-data-dictionary.json", clinical_dictionary)
    with (output / "clinical-data-dictionary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fields = ["record_kind", "field", "definition", "present_in_records", "record_kind_count"]
        writer = csv.DictWriter(handle, fields)
        writer.writeheader(); writer.writerows(clinical_dictionary["definitions"])
    report = {"schema_version": SCHEMA, "generated_at": generated_at,
        "internal_release": internal_contract, "public_release": public_contract,
        "counts_ledger": ledger, "review": review, "review_package": review_package,
        "analysis_audit": {"analysis_count": audits["analysis_count"], "findings": audits["findings"],
                           "expert_review_status": "pending"},
        "producer_source_audit": producer_audit,
        "clinical_summary": actual_clinical, "comparison_scope_matrix": comparators,
        "food_inventory_grain_audit": grains,
        "validation_receipts": receipts, "legacy_claims": legacy,
        "journal_submission_ready": False,
        "readiness_limits": ["Human scientific review is not automatically completed",
            "Sealed retrieval gold answers and precision remain unevaluated by this pipeline",
            "Version DOI, public HTTPS operation and author metadata require final human completion"],
        "sealed_query_answers_accessed": False,
        "report_wording": _report_wording(ic, pc, actual),
        "next_actions": ["Complete record-bound human source review and adjudicate corrections",
            "Freeze expert gold answers before sealed retrieval evaluation",
            "Run final-release accuracy, uncached/cold/warm performance, export, rebuild and rollback checks",
            "Register version data/code identifiers and complete public hosting and author metadata"]}
    _json(output / "submission-report.json", report)
    _json(output / "claim-ledger.json", ledger)
    with (output / "claim-ledger.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fields = sorted({k for row in ledger for k in row})
        writer = csv.DictWriter(handle, fields)
        writer.writeheader(); writer.writerows(ledger)
    (output / "NutriOmics_성과및최종보완보고서.html").write_text(_report_html(report), encoding="utf-8")
    return report


def records_to_snapshot(records: list[dict]) -> dict:
    return {"science_records": [{"id": r["record_id"], "kind": r["record_kind"],
                                "metadata_json": canonical(r["record"])} for r in records]}


def _report_wording(ic: dict, pc: dict, science: Counter) -> dict:
    def n(value): return f"{value:,}" if isinstance(value, int) else "미확인"
    para = (f"NutriOmics 연구용 통합 DB 및 인터랙티브 탐색 플랫폼을 구축하였다. 내부에서 조성 기록이 연결된 식품 "
        f"{n(ic.get('composition_backed_foods'))}개와 조성 {n(ic.get('measurement'))}건을 통합하였으며, "
        f"현재 공개 후보에는 {n(pc.get('composition_backed_foods'))}개 식품과 {n(pc.get('measurement'))}건의 조성이 포함된다. "
        f"공개 자료의 {science['analysis_run']}개 분석 실행에서 생성된 정량 결과 {n(pc.get('effect'))}행을 "
        "출처와 분석 버전별로 조회할 수 있다. 결과 행은 대상자 수가 아니며, 계산 예측·자동 문헌 후보와 "
        "사람 중재 결과는 별도 계층으로 제공한다. 과학적 검수 및 최종 공개 등록의 완료 여부는 별도 검수·등록 기록을 따른다.")
    return {"paragraph": para, "one_line": "식품 조성–사람 식이중재 오믹스 통합 DB·탐색 플랫폼 구축; 과학적 검수·최종 공개 등록 상태 별도 관리.",
        "disallowed_claims": ["개인 건강영향 예측 모델 완성", "관계 전체를 검증된 건강 상호작용으로 표현", "효과 행을 대상자 수로 표현",
                              "SOTA 달성", "자동 점검을 전문가 승인으로 표현"]}


def _report_html(report: dict) -> str:
    esc = lambda value: html.escape(str(value))
    def number(value): return f"{value:,}" if isinstance(value, int) else "미확인"
    ic, pc = report["internal_release"]["counts"], report["public_release"]["counts"]
    metrics = [("조성 기록이 연결된 식품", "composition_backed_foods"), ("조성 기록", "measurement"),
               ("전체 관계", "edge"), ("정량 결과 행", "effect"), ("연구 등록", "study_registry_records"),
               ("재분석 자료를 가진 연구 등록 그룹", "reanalyzed_study_groups")]
    counts = "".join(f"<tr><th>{label}</th><td>{number(ic.get(key))}</td><td>{number(pc.get(key))}</td></tr>" for label, key in metrics)
    counts += (f"<tr><th>음식 유형 식별자 노드</th><td>{number(ic.get('by_kind',ic.get('node_kinds',{})).get('food'))}</td>"
               f"<td>{number(pc.get('by_kind',pc.get('node_kinds',{})).get('food'))}</td></tr>")
    counts += "".join(f"<tr><th>{label}</th><td>{number(ic.get('science_kinds',{}).get(key))}</td><td>{number(pc.get('science_kinds',{}).get(key))}</td></tr>"
                      for label, key in (("노출 기록", "exposure"), ("대비 기록", "contrast"), ("분석 실행", "analysis_run")))
    tiers = "".join(f"<tr><th>{esc(tier)}</th><td>{number(ic.get('by_tier',{}).get(tier,0))}</td><td>{number(pc.get('by_tier',{}).get(tier,0))}</td></tr>"
                    for tier in sorted(set(ic.get("by_tier", {})) | set(pc.get("by_tier", {}))))
    audit_rows = "".join(f"<tr><td>{esc(f['analysis_version'])}</td><td>{esc(f['code'])}</td><td>{esc(f['message'])}</td></tr>"
                         for f in report["analysis_audit"]["findings"])
    def receipt_scope(r):
        if r.get("runtime_applicability") == "historical_runtime_not_current_pass":
            return "이전 실행 코드의 결과: 현재 서비스 통과로 승계하지 않음"
        return "현재 자료 release 참조; 실행 코드·검증 범위는 해당 증빙을 따름" if r["applies_to_current_release"] else "과거 또는 release 미결속: 현재 통과로 승계하지 않음"
    receipts = "".join(f"<tr><td>{esc(r['reference_date'])}</td><td>{esc(r.get('label',r['source_file']))}<br><span class=muted>{esc(r['evidence_class'])}</span></td><td>{esc(r.get('status','recorded'))}<br>{esc(receipt_scope(r))}</td><td class=hash>{esc(r['source_sha256'])}</td></tr>"
                       for r in report["validation_receipts"])
    if not receipts: receipts = "<tr><td colspan=4>이번 release에 연결된 검증 영수증 미입력. 과거 결과를 현재 통과로 대체하지 않음.</td></tr>"
    legacy = "".join(f"<tr><th>{esc(r['claim_id'])}</th><td>{esc(r['reported_value'])}</td><td>{esc(r['reference_date'] or '미확인')}</td><td>{esc(r['status'])}</td></tr>" for r in report["legacy_claims"])
    cs = report["clinical_summary"]
    clinical = (f"<p>원문 표 전사 후보 <strong>{cs['candidate_records']}건</strong> · "
        f"시료·결측 범위 감사 {cs['study_audits']}개 연구 · 건강지표 유형 {cs['health_endpoint_types']}개 · "
        f"정량 후보가 연결된 연구 {len(cs['studies_with_candidate_results'])}개.</p>"
        "<p>이는 기계 보조 원문 대조 후보이며 전문가 승인이 아니다. 전사하지 않은 지표는 미측정으로 표시하지 않는다.</p>")
    if cs["unit_caveats"]:
        clinical += ("<p class=status><strong>요구르트 iAUC 단위 주의:</strong> 원문은 농도 단위로 표기하지만 적분 시간 단위를 명시하지 않는다. "
            "원문 표기를 유지하며 공복 농도·시간 통합 단위로 변환하지 않는다. 임상 대상자 13명과 RNA-seq 7명은 별도 범위다.</p>")
    for example in cs["statistical_scope_examples"]:
        stats = "".join(f"<tr><td>{esc(t['comparison_scope'])}</td><td>{esc(t['reported_label'])}</td><td>{esc(t['multiplicity'])}</td></tr>"
                        for t in example["statistical_tests"])
        clinical += ("<h3>무대조 반복 관찰의 P값 범위: GSE219217 체중</h3>"
            "<p>전체 시점 검정과 개별 시점 비교, 보정 전후 P값을 구분한다. 전체 시점 유의성이 모든 쌍의 유의성을 뜻하지 않는다. "
            "원문은 임상 결측을 평균 대치했으며, 지표·시점별 실제 관측 n은 미확인이다.</p>"
            f"<table><thead><tr><th>검정 범위</th><th>원문 P값</th><th>다중검정</th></tr></thead><tbody>{stats}</tbody></table>")
    comparator_rows = "".join(f"<tr><th><a href=\"{esc(r['source_url'])}\">{esc(r['resource'])}</a></th><td>{esc(r['verified_scope'])}</td><td>{esc(r['nutriomics_comparison'])}</td></tr>"
                              for r in report["comparison_scope_matrix"])
    producer = report["producer_source_audit"]
    producer_facts = "".join(f"<tr><td>{esc(row['series_accession'])}</td><td>{esc(fact['fact'])}</td><td>{esc(row['source_file'])}:{fact['source_line']}</td></tr>"
        for row in producer["analyses"] for fact in row["source_checked_facts"])
    producer_section = (f"<p>현재 기록된 생산 코드 해시와 로컬 보존 코드의 정확 일치: {producer['exact_source_matches']} / {producer['analysis_count']}개. "
        "아래 보충 설명은 실행 코드의 위치를 확인한 것이며 원 metadata를 변경하거나 사람의 과학적 승인을 생성하지 않는다.</p>")
    if producer_facts:
        producer_section += f"<table><thead><tr><th>자료</th><th>해시가 일치하는 코드에서 확인한 내용</th><th>코드 위치</th></tr></thead><tbody>{producer_facts}</tbody></table>"
    grain_rows = "".join(f"<tr><th>{'내부' if scope=='internal' else '공개'}</th><td>{number(r['prior_validated_food_records'])}</td><td>{number(r['food_identifier_nodes'])}</td><td>{number(r['composition_record_linked_foods'])}</td><td>{number(r['food_nodes_without_composition_records'])}</td></tr>"
                         for scope, r in report["food_inventory_grain_audit"]["scope"].items())
    return f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>NutriOmics 구축 성과 및 최종 보완 보고서</title>
<style>@page{{size:A4;margin:18mm}}*{{box-sizing:border-box}}body{{margin:0;background:#edf1f3;color:#152b3b;font:14px/1.7 "Malgun Gothic",system-ui,sans-serif}}main{{max-width:1120px;margin:40px auto;background:white;padding:52px 60px}}.eyebrow{{font-size:12px;letter-spacing:.12em;color:#537080}}h1{{font-size:32px;line-height:1.35;margin:12px 0 18px}}h2{{font-size:20px;border-top:2px solid #173e54;padding-top:18px;margin-top:38px}}h3{{font-size:16px}}.lead{{font-size:17px;line-height:1.8}}.status{{border-left:4px solid #b87d24;background:#faf6ee;padding:15px 20px}}table{{width:100%;border-collapse:collapse;margin:18px 0;font-size:12px}}th,td{{border-bottom:1px solid #d7e0e5;text-align:left;padding:10px 8px;vertical-align:top}}thead th{{background:#eaf0f3}}th{{font-weight:600}}.mono,.hash{{font:11px/1.6 Consolas,monospace;overflow-wrap:anywhere}}.muted{{color:#536976}}blockquote{{margin:18px 0;padding:20px 24px;background:#f3f6f8;border-left:3px solid #34627d}}pre{{white-space:pre-wrap;font-size:11px}}button{{padding:10px 18px;background:#173e54;color:white;border:0;cursor:pointer}}ul{{padding-left:20px}}@media print{{body{{background:white}}main{{max-width:none;margin:0;padding:0}}button{{display:none}}h2,h3{{break-after:avoid}}tr,blockquote{{break-inside:avoid}}}}@media(max-width:760px){{main{{margin:0;padding:24px}}h1{{font-size:25px}}}}</style>
<main><div class=eyebrow>NUTRIOMICS · RELEASE-BOUND EVIDENCE REPORT</div><h1>구축 성과 및 최종 보완 보고서</h1><p class=muted>생성 기준: {esc(report['generated_at'])} · 과제 성과보고 및 Data Descriptor 준비</p><button onclick="window.print()">인쇄 / PDF 저장</button>
<p class=lead>식품 조성과 사람 식이중재 오믹스 결과를 출처·분석 버전별로 탐색할 수 있는 연구 플랫폼을 구축하였다. 수록 관계 전체가 검증된 건강 효과를 뜻하지 않는다.</p><p class=status><strong>논문 제출 준비 완료: 아니오.</strong> 전문가 검수, 봉인 질의 평가, DOI와 공개 운영 및 저자 확인이 남아 있다. 이 보고서 생성은 사람의 과학적 승인을 대신하지 않는다.</p>
<h2>1. 현재 release와 구축 실적</h2><p class=mono>내부: {esc(report['internal_release']['release_id'])}<br>공개 후보: {esc(report['public_release']['release_id'])}</p><table><thead><tr><th>집계 단위</th><th>내부</th><th>공개 후보</th></tr></thead><tbody>{counts}</tbody></table><p class=muted>식품은 원본·조리상태별 기록, 효과는 특징×대비×분석 버전별 결과다. 독립 대상자·독립 임상시험의 총합은 감사 전까지 미확인으로 유지한다.</p><h3>과거 식품 수치와 집계 단위 교정</h3><table><thead><tr><th>범위</th><th>과거 validated_food_records</th><th>현재 음식 노드</th><th>조성 기록 연결 식품</th><th>조성 기록 없는 음식 노드</th></tr></thead><tbody>{grain_rows}</tbody></table><p>현재 조성 연결 식품은 <span class=mono>COUNT(DISTINCT measurement.food)</span>로 산출한다. 음식 노드 총수와 다르며, 조성 기록이 있어도 모든 값이 정량 가능한 것은 아니다. 과거 수치에 맞춰 값을 강제로 유지하지 않는다. 집계 정의 차이만으로 데이터 삭제·추가를 판단하지 않으며, 원본 보존은 별도 대조 검증을 따른다.</p>
<p>대비 기록에는 분석 버전과 비교 종류가 함께 포함된다. 공개 후보의 약물 비교는 {number(pc.get('pharmacological_contrasts'))}건이다. GSE27385의 fenofibrate 비교를 식이 효과로 해석하지 않으며, 공개·내부 대비 수 차이는 원본 비교 기록의 포함 범위로 추적한다.</p>
<h3>근거 계층별 관계</h3><table><thead><tr><th>계층</th><th>내부</th><th>공개 후보</th></tr></thead><tbody>{tiers}</tbody></table><p>GraphRegLR은 세포주 기반 조절영역–유전자 계산 예측이며 식품 섭취 효과나 임상 확률이 아니다. 자동 문헌 후보, 조성, 사람 중재 관찰, 외부 기전 관계는 별도로 해석한다.</p>
<h2>2. 과학적 검수와 건강지표 연결</h2><p>최종 검수 대상 {report['review']['expected_records']}건 · 사람 승인 {report['review']['expert_approved']}건 · 대기 {report['review']['decisions'].get('pending',0)}건. 원문 위치·검수자·시각·판정·이유를 원본 기록 SHA256에 연결한다. 단독 검수는 독립 검증이나 검수자 간 일치도와 다르다.</p>{clinical}<p>같은 연구에서 보고된 임상 결과와 오믹스 결과의 동시 조회는 매개 또는 인과 관계를 입증하지 않는다. 건강 지표가 없거나 공개가 제한된 상태도 보존한다.</p>
<h2>3. 분석 metadata 감사</h2><p>현재 실행 기록 {report['analysis_audit']['analysis_count']}개를 대상으로 선언된 모델·시료 범위·통계 단위·다중검정 정보를 점검하였다. 아래 항목은 원 metadata의 누락·사람 검수 또는 해석 주의 사항이며, 자동으로 통계 오류·승인을 판정한 결과가 아니다.</p><table><thead><tr><th>분석 버전</th><th>점검 항목</th><th>확인할 내용</th></tr></thead><tbody>{audit_rows}</tbody></table>{producer_section}
<h2>4. 기술 검증의 날짜와 범위</h2><p>과거 성능 결과와 현재 release의 검증을 분리한다. warm 캐시·비캐시·첫 요청·프로세스 시작을 구분한다. 원문 파일의 재확보·전사 재현, 생산 코드 해시 일치, 분석 입력 파일 해시 확인은 각각 별개 범위이며 모델 타당성·임상 해석·사람 검수의 완료를 입증하지 않는다. 정량값 보존 검증도 생물학적 의미의 전문가 검수가 아니다.</p><table><thead><tr><th>기준일</th><th>증빙·범위</th><th>상태</th><th>SHA256</th></tr></thead><tbody>{receipts}</tbody></table>
<h2>5. 보고서에 사용할 문구</h2><blockquote>{esc(report['report_wording']['paragraph'])}</blockquote><p><strong>성과표:</strong> {esc(report['report_wording']['one_line'])}</p><p>개인 건강영향 예측 완성, 전체 관계의 효능 검증, 효과 행 수를 대상자 수로 표현하는 문구와 SOTA 달성 주장은 사용하지 않는다.</p>
<h2>6. 과거 과제 지표의 별도 관리</h2><table><thead><tr><th>과거 주장</th><th>기재값</th><th>기준일</th><th>판정</th></tr></thead><tbody>{legacy}</tbody></table><p>과거 보고서 문구는 원 DB 집계·원 모델 검증 증거와 다르다. 이번 release의 관계 또는 효과 행 수로 해당 지표의 달성을 대체하지 않는다. 정확한 문맥과 파일 해시는 legacy-claims.json에 보존한다.</p>
<h2>7. 선행 자원과의 범위 비교</h2><p>2026년 10월 19일 원 논문의 기술 범위를 확인하였다. 동일 자료로 수행한 성능 비교가 아니며, 최신 서비스 가용성·갱신 주기는 이 검토에서 확정하지 않았다.</p><table><thead><tr><th>자원·원 논문</th><th>확인한 범위</th><th>NutriOmics 비교 기준</th></tr></thead><tbody>{comparator_rows}</tbody></table><p>차별화 평가 대상은 정량 노출·사람 중재 대비·DNAm·근거 계층·재현 가능한 공개 release다. SOTA·최초·정확도 우위는 주장하지 않는다.</p>
<h2>8. 최종 보완 및 제출 조건</h2><ol><li>최종 핵심 기록의 원문 검수와 수정·제외 판정을 완료한다.</li><li>전문가 정답을 먼저 고정한 뒤 봉인 질의를 평가하고 분모·불확실성·실패 사례를 공개한다.</li><li>같은 release에서 수치·API·화면·원고 일치, 성능·내보내기·재구축·복원을 검증한다.</li><li>공개 자료만 사용하는 HTTPS 운영, 버전 DOI, 저자·연구비·윤리 문구를 확정한다.</li></ol><p class=muted>이 HTML과 JSON/CSV 장부는 동일한 canonical contract 및 실제 science_record에서 생성된다. 봉인 평가 정답은 읽지 않는다. 데이터·코드의 최종 등록 및 기관 확인은 저자가 수행한다.</p></main></html>'''
