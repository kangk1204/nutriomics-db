from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

from nutrigenomics.schemas import exportable_payload

API_BASE = os.environ.get("NUTRIGENOMICS_API_BASE", "http://127.0.0.1:8000").rstrip("/")
API_PREFIX = f"{API_BASE}/api/v1"
BROWSER_API_BASE = os.environ.get("NUTRIGENOMICS_BROWSER_API_BASE", API_BASE).rstrip("/")
BROWSER_API_PREFIX = f"{BROWSER_API_BASE}/api/v1"
LOCAL_TIMEZONE = ZoneInfo("Asia/Seoul")
st.set_page_config(page_title="Nutrigenomics 연구 DB", page_icon="🧬", layout="wide")
st.title("Nutrigenomics 연구 DB")
st.caption("음식 성분량과 사람 연구 근거를 탐색하는 연구용 도구입니다. 개인별 유전자 반응을 예측하지 않습니다.")


def api_get(path: str, **params):
    response = requests.get(f"{API_PREFIX}{path}", params=params, timeout=15)
    response.raise_for_status()
    return response.json()


def api_post(path: str, payload: dict):
    response = requests.post(f"{API_PREFIX}{path}", json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def api_download(path: str, **params) -> bytes:
    response = requests.get(f"{API_PREFIX}{path}", params=params, timeout=60)
    response.raise_for_status()
    return response.content


@st.cache_data(ttl=300, show_spinner=False)
def load_foods(query: str):
    return api_get("/foods", query=query, limit=50).get("items", [])


def format_food(food: dict) -> str:
    details = [food.get("preparation_state"), food.get("edible_state"), food.get("source_id")]
    details = [value for value in details if value]
    return f"{food['name']} · {' / '.join(details)} · {food['source_code']}"


def meal_payload(draft: list[dict], reference_context: dict | None = None) -> dict:
    payload = {
        "items": [{"food_id": row["food_id"], "amount_g": row["amount_g"]} for row in draft]
    }
    if reference_context and any(value is not None for value in reference_context.values()):
        payload["reference_context"] = deepcopy(reference_context)
    return payload


def invalidate_changed_analysis(state, current_payload: dict) -> None:
    if state.get("meal_analysis_payload") != current_payload:
        state.pop("meal_analysis", None)
        state.pop("meal_analysis_payload", None)


def render_analysis_scope(record: dict) -> None:
    analysis = record.get("analysis_provenance") or {}
    contrast_scope = (record.get("contrast_metadata") or {}).get("sample_scope") or {}
    model_scope = analysis.get("sample_scope") or {}
    if contrast_scope or model_scope:
        rows = []
        if contrast_scope:
            rows.append({
                "범위": "해당 대비에 직접 기여",
                "독립 사람 수": contrast_scope.get("contrast_people", contrast_scope.get("independent_people")),
                "assay 수": contrast_scope.get("directly_contributing_assays", contrast_scope.get("direct_contrast_assays")),
            })
        fitted_people = contrast_scope.get("fitted_model_people")
        if fitted_people is None:
            fitted_people = model_scope.get("fitted_model_people", model_scope.get("independent_people"))
        fitted_assays = contrast_scope.get("fitted_model_assays")
        if fitted_assays is None:
            fitted_assays = model_scope.get("fitted_model_assays")
        if fitted_people is not None or fitted_assays is not None:
            rows.append({"범위": "전체 적합 모델", "독립 사람 수": fitted_people, "assay 수": fitted_assays})
        group_assays = contrast_scope.get("group_assays", contrast_scope.get("weight_group_assays"))
        if isinstance(group_assays, dict):
            for group, assays in group_assays.items():
                if isinstance(assays, (int, float)):
                    rows.append({"범위": f"군 전체 · {group}", "독립 사람 수": None, "assay 수": assays})
        elif isinstance(group_assays, (int, float)):
            rows.append({"범위": "해당 군 전체", "독립 사람 수": None, "assay 수": group_assays})
        if rows:
            st.markdown("**대비와 전체 모델의 시료 범위**")
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
            st.caption("각 행은 서로 다른 시료 범위입니다. 사람 수와 assay 수를 구분하며, 여러 대비·분석의 assay 수를 합쳐 고유 시료 수로 계산하지 않습니다.")
        unique_assays = model_scope.get("unique_modeled_assays_across_acute_chronic")
        if unique_assays is not None:
            st.caption(f"급성·반복 섭취 모델 전체의 중복 제거 assay: {unique_assays}개")
        if model_scope.get("RNA_people") is not None or model_scope.get("clinical_people") is not None:
            rna_people = model_scope.get("RNA_people")
            clinical_people = model_scope.get("clinical_people")
            st.caption(
                f"RNA 대상자: {rna_people if rna_people is not None else '미확인'}명 · "
                f"별도 임상 대상자: {clinical_people if clinical_people is not None else '미확인'}명"
            )
    if analysis:
        manual_status = analysis.get("expert_manual_review_status") or "미확인"
        label = "대기 중 (pending)" if manual_status == "pending" else str(manual_status)
        st.caption(f"분석의 수동 전문가 검수: {label}")
        producer_audit = analysis.get("producer_output_audit") or {}
        audit = producer_audit.get("audit") or {}
        if audit:
            st.markdown("**실행 출력의 추가 감사**")
            st.caption(f"출력 추적 감사 상태: {audit.get('verification_status') or '미확인'}")
            if audit.get("argv_status") == "reconstructed_not_recorded":
                st.warning("실행 인자는 소스·래퍼에서 사후 재구성했습니다. 원 실행 때 기록된 명령 인자가 아닙니다 (reconstructed_not_recorded).")
            if audit.get("verification_scope"):
                st.caption(f"추가 감사의 범위: {audit['verification_scope']}")
            if audit.get("historical_limitations"):
                st.markdown("**과거 실행 기록과 감사의 제한**")
                for limitation in audit["historical_limitations"]:
                    st.write(f"• {limitation}")
            producer_manual = audit.get("expert_manual_review") or manual_status
            producer_label = "대기 중 (pending)" if producer_manual == "pending" else str(producer_manual)
            st.caption(f"추가 감사의 수동 전문가 검수: {producer_label}")
        with st.expander("분석 버전·입출력·시료 감사 출처 전체 보기"):
            st.json(analysis)


def render_assertions(records: list[dict], namespace: str) -> None:
    for index, record in enumerate(records[:300]):
        exposure = record.get("exposure") or {}
        study = record.get("series_accession") or record.get("study_id") or record.get("study_title")
        title = record.get("study_title") or ""
        detail = exposure.get("label") or exposure.get("name")
        if not detail:
            detail = " → ".join(str(record[key]) for key in ("subject_id", "object_id") if record.get(key))
        status = {"reanalyzed": "재분석 결과", "metadata_only": "중재 정보"}.get(record.get("result_status"), "주석 근거")
        label = " · ".join(str(value) for value in (study, title if title != study else None, detail, record.get("contrast"), status) if value)
        label = label or record.get("id", "근거")
        with st.expander(str(label)):
            if record.get("assertion"):
                st.write(record["assertion"])
            if exposure:
                st.markdown(f"**{exposure.get('label') or exposure.get('name') or '중재'}**")
                amount = exposure.get("amount")
                duration = exposure.get("duration_days")
                contrast_scope = (record.get("contrast_metadata") or {}).get("sample_scope") or {}
                model_scope = (record.get("analysis_provenance") or {}).get("sample_scope") or {}
                st.caption(
                    f"연구 용량: {amount if amount is not None else '미확인'} {exposure.get('unit') or ''} · "
                    f"중재 기간: {duration if duration is not None else '미확인'} day · "
                    f"조직: {record.get('tissue') or '미확인'}"
                    + ("" if contrast_scope or model_scope else
                       f" · 독립 대상자: {record.get('n_people') if record.get('n_people') is not None else '미확인'}명")
                )
                with st.expander("중재 기록 전체 보기"):
                    st.json(exposure)
            render_analysis_scope(record)
            if record.get("applicability"):
                st.markdown("**현재 입력에 대한 조건별 적용 범위**")
                applicability = record["applicability"]
                dimension_names = {
                    "food_formulation": "식품·제형", "component": "성분", "dose": "용량",
                    "dose_frequency": "섭취 빈도", "duration": "중재 기간", "tissue": "조직",
                    "response_time": "반응 시간", "population": "대상군",
                }
                states = {"match": "일치", "mismatch": "불일치", "unknown": "미확인"}
                st.dataframe(pd.DataFrame([
                    {"조건": dimension_names.get(key, key), "입력과 연구": states.get(value, value)}
                    for key, value in applicability.get("dimensions", {}).items()
                ]), width="stretch", hide_index=True)
                input_amount = applicability.get("input_comparable_amount")
                if input_amount is not None:
                    unit = applicability.get("study_unit") or exposure.get("unit") or ""
                    st.caption(f"비교 가능한 입력량 {input_amount:g} {unit} · 연구 용량 {applicability.get('study_amount')} {unit}")
                if applicability.get("dose_comparison_scope") == "nominal_related_food_mass":
                    st.caption("관련 식품의 명목 질량만 비교했습니다. 제품·제형의 동일성은 확인되지 않았습니다.")
                if applicability.get("dose_frequency") == "per_day":
                    st.caption("연구 용량은 하루 섭취량입니다. 이번 한 끼와의 용량 일치 여부는 미확인입니다.")
                with st.expander("조건 비교 기록 전체 보기"):
                    st.json(applicability)
            st.caption(f"결과 상태: {record.get('result_status', '미확인')}")
            if record.get("results"):
                st.dataframe(pd.DataFrame(record["results"]), width="stretch", hide_index=True)
            elif record.get("subject_id") or record.get("object_id"):
                st.json({key: record.get(key) for key in (
                    "subject_id", "object_id", "layer", "organism", "tissue", "inference",
                    "review_status", "effect", "effect_unit", "p_value", "q_value", "value_status",
                )})
            selector = record.get("effect_selector") or {}
            params = {
                "accession": selector.get("accession") or selector.get("series_accession") or record.get("series_accession"),
                "contrast": selector.get("contrast") or record.get("contrast"),
                "feature_type": selector.get("feature_type"),
                "analysis_version": selector.get("analysis_version"),
                "limit": 100,
            }
            if params["accession"] and params["contrast"]:
                st.link_button(
                    "이 비교 조건의 전체 결과 조회",
                    f"{BROWSER_API_PREFIX}/evidence?{urlencode({k: v for k, v in params.items() if v is not None})}",
                )
            clinical = record.get("clinical_endpoints") or []
            if clinical:
                with st.expander("별도로 측정된 임상 결과", expanded=True):
                    st.caption("집계 임상 대상자와 오믹스 하위표본을 구분합니다. 미보고 효과량은 0이 아니며 분자 변화와의 인과 관계를 의미하지 않습니다.")
                    clinical_frame = pd.DataFrame(clinical)
                    visible_columns = [key for key in (
                        "endpoint", "finding", "statistical_status", "effect_estimate", "p_value",
                        "n_people", "source_location", "source_url",
                    ) if key in clinical_frame.columns]
                    st.dataframe(clinical_frame[visible_columns], width="stretch", hide_index=True)
                    with st.expander("임상 결과의 대비·결측·출처 전체 보기"):
                        st.json(clinical)
            elif record.get("layer") in {"observed", "molecular_measurement"}:
                st.info("이 근거에 연결된 임상 결과가 없습니다. 분자 변화만으로 건강상 이득·손해를 판단할 수 없습니다.")
            if record.get("limitations"):
                limitations = record["limitations"]
                if isinstance(limitations, list):
                    st.markdown("**해석의 제한**")
                    for limitation in limitations:
                        st.write(f"• {limitation}")
                else:
                    st.warning(str(limitations))
            provenance = record.get("provenance") or {}
            if provenance:
                st.markdown("**출처와 검수 기록**")
                st.caption(
                    f"원문 위치: {provenance.get('source_location') or provenance.get('source_ref') or '미확인'} · "
                    f"버전: {provenance.get('version') or '미확인'} · "
                    f"출처 검수: {record.get('review_status') or provenance.get('review_status') or '미확인'} · "
                    f"전문가 검수: {(provenance.get('metadata') or {}).get('expert_review_status') or (record.get('analysis_provenance') or {}).get('expert_manual_review_status') or '미확인'}"
                )
                with st.expander("출처·해시·이용조건 전체 보기"):
                    st.json(provenance)
            source_url = record.get("source_url") or provenance.get("source_url") or provenance.get("url")
            if source_url and source_url.startswith(("https://", "http://")):
                st.link_button("원본 자료 열기", source_url)
            evidence_id = record.get("id") or record.get("evidence_id")
            if evidence_id and st.button("원문·분석 출처 확인", key=f"provenance-{namespace}-{index}-{evidence_id}"):
                try:
                    st.json(api_get(f"/provenance/{evidence_id}"))
                except requests.RequestException as error:
                    st.error(f"출처를 불러오지 못했습니다: {error}")
            if record.get("export_allowed") is False:
                st.caption("이 기록은 원자료 이용조건에 따라 내보내기에서 제외됩니다.")
    if len(records) > 300:
        st.caption("화면에는 처음 300개 근거를 표시합니다. 검색 페이지 크기를 줄여 추가 자료를 확인하세요.")


def render_evidence_gaps(gaps: list, components: list[dict]) -> None:
    grouped = {}
    component_names = {row.get("component_key"): row.get("name") for row in components}
    for gap in gaps:
        if not isinstance(gap, dict):
            key = str(gap)
            grouped.setdefault(key, {"판단할 수 없는 부분": key, "해당 기록 수": 0})["해당 기록 수"] += 1
            continue
        message = gap.get("message") or gap.get("code") or "미확인"
        row = grouped.setdefault(message, {"판단할 수 없는 부분": message, "해당 기록 수": 0, "성분": []})
        row["해당 기록 수"] += 1
        if gap.get("component_key"):
            row["성분"].append(component_names.get(gap["component_key"]) or gap["component_key"])
    rows = []
    for row in grouped.values():
        rows.append({**row, "성분": ", ".join(row.get("성분", []))})
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    with st.expander("자료 부족 기록 전체 보기"):
        st.json(gaps)


pages = st.tabs(["식사 분석", "근거 탐색", "연구 상세", "분석·내보내기"])

with pages[0]:
    st.subheader("음식과 섭취량")
    st.info(
        "USDA FoodData Central과 K-FIND 한식 50종을 검색합니다. "
        "K-FIND 함량은 원천자료의 100g 기준이며, 폐기율을 자동 환산하지 않습니다. "
        "이 화면은 성분량과 연구 근거를 조회하며 개인별 오믹스·질병 위험을 예측하지 않습니다."
    )
    search_text = st.text_input("음식 이름", placeholder="예: 흰쌀밥, 계란찜, apple")
    try:
        food_hits = load_foods(search_text.strip())
    except requests.RequestException as error:
        st.error("API 서버에 연결할 수 없습니다. 원격 서버의 API와 SSH 포워딩을 확인하세요.")
        st.caption(f"요청 주소: {API_BASE}. 기술 내용: {error}")
        food_hits = []

    if not food_hits:
        st.info("검색 결과가 없습니다. 다른 음식명이나 별칭으로 검색해 보세요.")
    else:
        food_by_label = {format_food(food): food for food in food_hits}
        chosen_label = st.selectbox("식품 기록 선택", list(food_by_label))
        portion = st.number_input("먹은 양 (g)", min_value=0.1, max_value=20_000.0, value=150.0, step=10.0)
        if st.button("식사에 추가", type="secondary"):
            chosen = food_by_label[chosen_label]
            st.session_state.setdefault("meal_draft", [])
            st.session_state["meal_draft"].append(
                {"food_id": chosen["id"], "name": chosen["name"], "amount_g": float(portion)}
            )

    st.markdown("#### 이번 끼니")
    draft = st.session_state.setdefault("meal_draft", [])
    if draft:
        st.dataframe(pd.DataFrame(draft), width="stretch", hide_index=True)
        with st.expander("조회할 연구 조건 (선택)"):
            tissue = st.selectbox(
                "조직", ["미지정", "whole blood", "PBMC"], key="meal_context_tissue"
            )
            response_known = st.checkbox("반응 시간을 지정", key="meal_context_response_known")
            response_time = st.number_input(
                "반응 시간 (h)", min_value=0.0, value=6.0,
                disabled=not response_known, key="meal_context_response_time",
            )
            duration_known = st.checkbox("반복 섭취 기간을 지정", key="meal_context_duration_known")
            regimen_days = st.number_input(
                "반복 섭취 기간 (day)", min_value=0.0, value=0.0,
                disabled=not duration_known, key="meal_context_duration",
            )
            st.caption("미지정 조건은 일치로 간주하지 않습니다. 한 끼의 섭취량으로 장기 중재 효과를 예측하지 않습니다.")
        current_payload = meal_payload(
            draft,
            {
                "tissue": None if tissue == "미지정" else tissue,
                "response_time_h": float(response_time) if response_known else None,
                "regimen_duration_days": float(regimen_days) if duration_known else None,
            },
        )
        invalidate_changed_analysis(st.session_state, current_payload)
        left, right = st.columns(2)
        with left:
            analyze_clicked = st.button("성분·근거 계산", type="primary")
        with right:
            if st.button("입력 지우기"):
                st.session_state["meal_draft"] = []
                st.session_state.pop("meal_analysis", None)
                st.session_state.pop("meal_analysis_payload", None)
                st.rerun()

        if analyze_clicked:
            try:
                result = api_post("/meals/analyze", current_payload)
                st.session_state["meal_analysis"] = result
                st.session_state["meal_analysis_payload"] = deepcopy(current_payload)
            except requests.RequestException as error:
                st.error(f"분석 요청에 실패했습니다: {error}")

        analysis = st.session_state.get("meal_analysis")
        if analysis:
            st.markdown("#### 계산 결과")
            st.caption(analysis["interpretation"])
            if analysis["components"]:
                st.dataframe(
                    pd.DataFrame(analysis["components"])[["name", "category", "amount", "unit"]],
                    width="stretch",
                    hide_index=True,
                )
                with st.expander("사용한 성분 기록의 원본·단위·범위"):
                    st.json(
                        [
                            {
                                "component": row["name"],
                                "calculated_amount": row["amount"],
                                "calculated_unit": row["unit"],
                                "source_records": row.get("source_records", []),
                            }
                            for row in analysis["components"]
                        ]
                    )
            else:
                st.info("현재 선택한 음식에서 정량 가능한 성분 기록을 찾지 못했습니다.")
            if analysis["unquantified"]:
                with st.expander("정량하지 못한 기록과 이유"):
                    st.dataframe(pd.DataFrame(analysis["unquantified"]), width="stretch", hide_index=True)
            download_json, download_csv = st.columns(2)
            export_analysis = exportable_payload(analysis)
            with download_json:
                st.download_button(
                    "계산 결과 JSON",
                    data=json.dumps(export_analysis, ensure_ascii=False, indent=2),
                    file_name="meal-analysis.json",
                    mime="application/json",
                    key="meal-analysis-json",
                )
            with download_csv:
                component_frame = pd.DataFrame(export_analysis["components"])
                st.download_button(
                    "계산 결과 CSV",
                    data=component_frame.to_csv(index=False),
                    file_name="meal-components.csv",
                    mime="text/csv",
                    key="meal-analysis-csv",
                )
            if export_analysis["export_withheld_records"]:
                st.caption(f"이용조건에 따라 {export_analysis['export_withheld_records']}개 기록을 내보내기에서 제외합니다.")
            st.markdown("#### 관련 사람 실험에서 관찰된 결과")
            observed = analysis.get("observed_evidence", [])
            measured = [row for row in observed if row.get("result_status") == "reanalyzed" or row.get("clinical_endpoints")]
            metadata_only = [row for row in observed if row not in measured]
            if measured:
                render_assertions(measured, "meal-observed")
            else:
                st.info("현재 입력과 연결된 분자 재분석·임상 관찰 결과가 없습니다. 근거 부재나 무효과를 의미하지 않습니다.")
            if metadata_only:
                st.markdown("#### 관련 중재 정보 · 분자 재분석 미확보")
                st.caption("다음 기록은 실제 연구의 음식·성분·용량 정보입니다. 아직 관찰된 분자 효과를 조회할 수 없습니다.")
                render_assertions(metadata_only, "meal-registry")
            hypotheses = analysis.get("mechanistic_hypotheses", [])
            if hypotheses:
                st.markdown("#### 기전 가설")
                st.caption("세포·동물·표적·조절 영역의 관계는 해당 식사의 사람 건강 효과를 입증하지 않습니다.")
                render_assertions(hypotheses, "meal-hypotheses")
            if analysis.get("evidence_gaps"):
                with st.expander("현재 판단할 수 없는 부분", expanded=True):
                    render_evidence_gaps(analysis["evidence_gaps"], analysis["components"])
            if analysis.get("applicability"):
                with st.expander("관련성 및 조건별 일치·불일치·미확인"):
                    st.json(analysis["applicability"])
            if analysis["evidence"]:
                st.markdown("#### 기존 식별자 연결")
                st.dataframe(pd.DataFrame(analysis["evidence"]), width="stretch", hide_index=True)
            note = st.text_input("연구 메모 (선택)", max_chars=500, key="meal_note")
            if st.button("식사 기록 저장"):
                saved_payload = deepcopy(st.session_state["meal_analysis_payload"])
                saved_payload["note"] = note or None
                try:
                    saved = api_post("/meals", saved_payload)
                    st.success(f"저장했습니다. 기록 번호: {saved['id']}")
                except requests.RequestException as error:
                    st.error(f"기록을 저장하지 못했습니다: {error}")
    else:
        st.info("음식을 검색한 뒤 이번 끼니에 추가하세요.")

    st.markdown("#### 누적 섭취량")
    default_end = datetime.now(LOCAL_TIMEZONE).date()
    default_start = default_end - timedelta(days=6)
    start_date, end_date = st.columns(2)
    with start_date:
        date_start = st.date_input("시작일", value=default_start, key="intake_start")
    with end_date:
        date_end = st.date_input("종료일", value=default_end, key="intake_end")
    if date_end < date_start:
        st.error("종료일은 시작일 이후여야 합니다.")
    elif st.button("누적량 조회"):
        try:
            result = api_get("/meals/totals", start=date_start.isoformat(), end=date_end.isoformat())
            st.caption(f"기록한 식사 {result['meal_count']}건 · {result['note']}")
            for day in result["days"]:
                with st.expander(day["date"], expanded=True):
                    if day["components"]:
                        st.dataframe(pd.DataFrame(day["components"]), width="stretch", hide_index=True)
                    else:
                        st.write("정량 가능한 성분 기록이 없습니다.")
            meals_json = api_download(
                "/export/meals", start=date_start.isoformat(), end=date_end.isoformat(), format="json"
            )
            meals_csv = api_download(
                "/export/meals", start=date_start.isoformat(), end=date_end.isoformat(), format="csv"
            )
            export_json, export_csv = st.columns(2)
            with export_json:
                st.download_button("식사 기록 JSON", data=meals_json, file_name="meal-records.json", mime="application/json")
            with export_csv:
                st.download_button("식사 기록 CSV", data=meals_csv, file_name="meal-records.csv", mime="text/csv")
        except requests.RequestException as error:
            st.error(f"누적량을 불러오지 못했습니다: {error}")

with pages[1]:
    st.subheader("성분·유전자·경로·연구 근거")
    evidence_query = st.text_input("검색어", placeholder="성분명, 유전자, 경로, GEO accession, contrast")
    effect_type = st.selectbox(
        "결과 유형", ["전체", "gene", "pathway", "cpg", "dmr", "clinical"]
    )
    layer_options = {
        "전체 계층": None,
        "사람 분자 측정 결과": "molecular_measurement",
        "중재·섭취 조건": "exposure",
        "분자 표적": "molecular_target",
        "화학적 동일성": "molecular_identity",
        "경로 연결": "pathway",
    }
    layer_label = st.selectbox("근거 계층", list(layer_options))
    organism_filter = st.text_input("종 (선택)", placeholder="예: Homo sapiens")
    tissue_filter = st.text_input("조직 (선택)", placeholder="예: whole blood, PBMC")
    design_filter = st.text_input("연구설계 (선택)", placeholder="예: randomized_crossover")
    contrast_filter = st.text_input("비교 조건 (선택)", placeholder="contrast 식별자")
    page_controls = st.columns(2)
    with page_controls[0]:
        evidence_offset = st.number_input("검색 시작 위치", min_value=0, value=0, step=300)
    with page_controls[1]:
        evidence_page_size = st.selectbox("화면 표시 행 수", [100, 300, 1000, 5000], index=1)
    evidence_params = {
        "query": evidence_query,
        "feature_type": None if effect_type == "전체" else effect_type,
        "layer": layer_options[layer_label],
        "organism": organism_filter.strip() or None,
        "tissue": tissue_filter.strip() or None,
        "study_design": design_filter.strip() or None,
        "contrast": contrast_filter.strip() or None,
        "limit": evidence_page_size,
        "offset": int(evidence_offset),
    }
    if st.session_state.get("evidence_params") != evidence_params:
        st.session_state.pop("evidence_results", None)
    if st.button("근거 검색"):
        try:
            results = api_get("/evidence", **evidence_params)
            st.session_state["evidence_results"] = results
            st.session_state["evidence_params"] = deepcopy(evidence_params)
        except requests.RequestException as error:
            st.error(f"검색 요청에 실패했습니다: {error}")
    saved_evidence = st.session_state.get("evidence_results")
    if saved_evidence is not None:
        st.caption("통계적 연관성과 생물학적 의미는 구분해서 검토하세요.")
        st.caption(
            f"{saved_evidence['offset']:,}행부터 기존 결과 {saved_evidence['count']:,}행 표시 · "
            f"전체 효과 {saved_evidence['total_effects']:,}건 · "
            f"중재 연결 근거 {saved_evidence.get('total_assertions', 0):,}건 · "
            f"분자 주석 {saved_evidence.get('total_mechanistic_annotations', 0):,}건"
        )
        if saved_evidence.get("assertions"):
            st.markdown("#### 중재와 연결된 근거")
            render_assertions(saved_evidence["assertions"], "search")
        if saved_evidence.get("mechanistic_annotations"):
            st.markdown("#### 검수된 분자 주석·기전 관계")
            st.caption(
                f"주석은 별도 페이지 기준으로 {saved_evidence.get('annotation_offset', 0):,}행부터 "
                f"최대 {saved_evidence.get('annotation_limit', 200):,}개를 표시합니다. "
                "관련성·eQTM·표적·경로 연결은 식사 효과나 인과적 건강 변화의 예측값이 아닙니다."
            )
            render_assertions(saved_evidence["mechanistic_annotations"], "search-annotation")
        if saved_evidence["effects"]:
            st.markdown("#### 재분석 결과")
            st.dataframe(pd.DataFrame(saved_evidence["effects"]), width="stretch", hide_index=True)
        if saved_evidence["links"]:
            st.markdown("#### 기존 식별자 연결")
            st.dataframe(pd.DataFrame(saved_evidence["links"]), width="stretch", hide_index=True)
        if not saved_evidence["count"] and not saved_evidence.get("assertion_count") and not saved_evidence.get("mechanistic_annotation_count"):
            st.info("현재 적재된 자료에서 찾지 못했습니다.")
        query_params = {**st.session_state["evidence_params"], "format": "json"}
        try:
            evidence_json = api_download("/export/evidence", **query_params)
            evidence_csv = api_download("/export/evidence", **{**query_params, "format": "csv"})
            export_json, export_csv = st.columns(2)
            with export_json:
                st.download_button("근거 JSON", data=evidence_json, file_name="evidence.json", mime="application/json")
            with export_csv:
                st.download_button("근거 CSV", data=evidence_csv, file_name="evidence.csv", mime="text/csv")
        except requests.RequestException as error:
            st.error(f"근거 내보내기를 만들지 못했습니다: {error}")
    st.markdown("#### 실제 중재·용량·기간 탐색")
    intervention_query = st.text_input("중재 검색", placeholder="예: flavanol, yoghurt, fish oil")
    if st.button("중재 조회"):
        try:
            st.session_state["intervention_results"] = api_get(
                "/interventions", query=intervention_query, limit=100
            )
            st.session_state["intervention_query"] = intervention_query
        except requests.RequestException as error:
            st.error(f"중재 조회에 실패했습니다: {error}")
    intervention_results = st.session_state.get("intervention_results")
    if intervention_results and st.session_state.get("intervention_query") == intervention_query:
        st.caption(f"중재 기록 {intervention_results.get('total', 0):,}건")
        st.json(intervention_results.get("items", []))

with pages[2]:
    st.subheader("공개 연구와 분석 상태")
    try:
        studies = api_get("/studies").get("items", [])
        if studies:
            st.dataframe(pd.DataFrame(studies), width="stretch", hide_index=True)
            accession = st.selectbox(
                "GEO Series 상세",
                [row["accession"] for row in studies],
                key="study_detail_accession",
            )
            if st.button("시료·분석결과 보기", key="study_detail_load"):
                st.session_state["study_detail"] = api_get(f"/studies/{accession}")
            detail = st.session_state.get("study_detail")
            if detail and detail.get("accession") == accession:
                if detail["metadata_only_notice"]:
                    st.warning(detail["metadata_only_notice"])
                st.markdown("#### 연구 설계")
                st.write(detail.get("study_design") or "공개 연구 설계 요약이 등록되지 않았습니다.")
                counts = detail.get("counts", {})
                count_columns = st.columns(3)
                count_columns[0].metric(
                    "연구에서 보고한 사람 수",
                    counts.get("reported_people") if counts.get("reported_people") is not None else "미확인",
                )
                count_columns[1].metric("GEO 시료 기록", counts.get("geo_sample_records", 0))
                latest_run_people = next(
                    (
                        run.get("people")
                        for run in counts.get("analysis_runs", [])
                        if run.get("people") is not None
                    ),
                    None,
                )
                count_columns[2].metric(
                    "재분석에 사용한 사람 수",
                    latest_run_people if latest_run_people is not None else "실행별 기록 없음",
                )
                st.caption(counts.get("sample_count_note", ""))
                if detail.get("series_design"):
                    st.markdown("**이 Series의 설계와 assay 기준**")
                    st.json(detail["series_design"])
                bridge_detail = detail.get("intervention_evidence") or {}
                if bridge_detail:
                    st.markdown("#### 실제 중재와 분석 비교 조건")
                    for field in ("exposures", "arms", "contrasts", "interventions"):
                        if bridge_detail.get(field):
                            st.json(bridge_detail[field])
                    assertions = bridge_detail.get("assertions") or bridge_detail.get("items")
                    if assertions:
                        render_assertions(assertions, "study")
                if detail.get("study_limitations"):
                    st.markdown("**연구 해석의 제한**")
                    if isinstance(detail["study_limitations"], list):
                        for limitation in detail["study_limitations"]:
                            st.write(f"• {limitation}")
                    else:
                        st.info(str(detail["study_limitations"]))
                if detail.get("primary_sources"):
                    source_links = " · ".join(
                        f"[{source['label']}]({source['url']})"
                        for source in detail["primary_sources"]
                    )
                    st.markdown(f"출처: {source_links}")
                st.json(
                    {
                        key: detail[key]
                        for key in [
                            "study_id",
                            "study_title",
                            "title",
                            "assay_type",
                            "organism",
                            "platform_ids",
                            "pubmed_id",
                            "assay_data_downloaded",
                        ]
                    }
                )
                if detail["analysis_runs"]:
                    st.markdown("#### 재현 가능한 분석 실행")
                    st.json(detail["analysis_runs"])
                    for run in detail["analysis_runs"]:
                        allowed_outputs = {
                            str(output.get("path", "")).rsplit("/", 1)[-1]
                            for output in run.get("outputs", [])
                            if isinstance(output, dict)
                        }
                        auxiliary_candidates = run.get("metadata", {}).get("auxiliary_files", [])
                        if isinstance(auxiliary_candidates, str):
                            auxiliary_candidates = [auxiliary_candidates]
                        if not isinstance(auxiliary_candidates, list):
                            auxiliary_candidates = []
                        auxiliary_files = [
                            filename
                            for filename in auxiliary_candidates
                            if isinstance(filename, str) and filename in allowed_outputs
                        ]
                        if auxiliary_files:
                            st.markdown(f"**{run['analysis_version']} QC·시료 파일**")
                            columns = st.columns(min(3, len(auxiliary_files)))
                            for index, filename in enumerate(auxiliary_files):
                                try:
                                    contents = api_download(
                                        f"/studies/{accession}/analysis-runs/{run['analysis_version']}/files/{filename}"
                                    )
                                    columns[index % len(columns)].download_button(
                                        filename,
                                        data=contents,
                                        file_name=filename,
                                        mime="application/pdf" if filename.endswith(".pdf") else "text/tab-separated-values",
                                        key=f"{accession}-{run['analysis_version']}-{filename}",
                                    )
                                except requests.RequestException as error:
                                    st.caption(f"{filename} 다운로드 실패: {error}")
                st.markdown("#### GEO 시료")
                st.dataframe(pd.DataFrame(detail["samples"]), width="stretch", hide_index=True)
                if detail["effects"]:
                    st.markdown("#### 효과 결과")
                    if detail["effects_truncated"]:
                        st.caption(f"FDR 순 상위 1,000개 표시 · 전체 {detail['effect_count_total']:,}개 결과는 근거 검색·내보내기에서 조회")
                    st.dataframe(pd.DataFrame(detail["effects"]), width="stretch", hide_index=True)
                else:
                    st.info("이 Series에 연결된 분석 결과가 아직 없습니다.")
        else:
            st.info("조사 manifest와 GEO metadata를 적재하면 연구 목록이 표시됩니다.")
    except requests.RequestException as error:
        st.error(f"연구 목록을 불러오지 못했습니다: {error}")

with pages[3]:
    st.subheader("분석 결과와 연구 데이터 내보내기")
    st.caption(
        "식사 기록 또는 공개 연구의 재분석 결과를 조건별로 내려받습니다. "
        "오믹스 결과는 연구·조직·시점·대비 맥락을 보존하며 개인별 예측값이 아닙니다."
    )
    export_kind = st.radio(
        "내보낼 자료",
        ["식사 기록", "연구 효과 결과"],
        horizontal=True,
        key="export_kind",
    )

    if export_kind == "식사 기록":
        default_end = datetime.now(LOCAL_TIMEZONE).date()
        default_start = default_end - timedelta(days=6)
        start_col, end_col = st.columns(2)
        with start_col:
            export_meal_start = st.date_input(
                "시작일", value=default_start, key="export_meal_start"
            )
        with end_col:
            export_meal_end = st.date_input("종료일", value=default_end, key="export_meal_end")
        if export_meal_end < export_meal_start:
            st.error("종료일은 시작일 이후여야 합니다.")
        else:
            try:
                meal_summary = api_get(
                    "/meals/totals",
                    start=export_meal_start.isoformat(),
                    end=export_meal_end.isoformat(),
                )
                st.caption(
                    f"선택 기간의 식사 기록 {meal_summary['meal_count']:,}건을 내보냅니다. "
                    "식사 기록 내보내기는 최대 10,000건입니다."
                )
                json_col, csv_col = st.columns(2)
                with json_col:
                    st.download_button(
                        "식사 기록 JSON",
                        data=lambda: api_download(
                            "/export/meals",
                            start=export_meal_start.isoformat(),
                            end=export_meal_end.isoformat(),
                            format="json",
                        ),
                        file_name="nutrigenomics-meals.json",
                        mime="application/json",
                        key="bulk-meals-json",
                    )
                with csv_col:
                    st.download_button(
                        "식사 기록 CSV",
                        data=lambda: api_download(
                            "/export/meals",
                            start=export_meal_start.isoformat(),
                            end=export_meal_end.isoformat(),
                            format="csv",
                        ),
                        file_name="nutrigenomics-meals.csv",
                        mime="text/csv",
                        key="bulk-meals-csv",
                    )
            except requests.RequestException as error:
                st.error(f"식사 기록 내보내기를 준비하지 못했습니다: {error}")

    else:
        try:
            studies = api_get("/studies").get("items", [])
            accession_labels = {"전체 연구": None}
            accession_labels.update(
                {
                    f"{study['accession']} · {study['title']}": study["accession"]
                    for study in studies
                }
            )
            chosen_study_label = st.selectbox(
                "GEO 연구", list(accession_labels), key="export_study"
            )
            feature_options = {
                "전체 유형": None,
                "유전자 발현": "gene",
                "경로": "pathway",
                "DNA methylation CpG": "cpg",
                "DNA methylation DMR": "dmr",
                "임상지표": "clinical",
            }
            feature_label = st.selectbox(
                "결과 유형", list(feature_options), key="export_feature_type"
            )
            export_offset = st.number_input(
                "시작 행(offset)", min_value=0, value=0, step=100_000, key="export_offset"
            )
            export_limit = st.select_slider(
                "내보낼 최대 행 수",
                options=[1_000, 10_000, 50_000, 100_000],
                value=10_000,
                key="export_limit",
            )
            export_filters = {
                "accession": accession_labels[chosen_study_label],
                "feature_type": feature_options[feature_label],
                "limit": int(export_limit),
                "offset": int(export_offset),
            }
            preview = api_get(
                "/evidence",
                accession=export_filters["accession"],
                feature_type=export_filters["feature_type"],
                limit=1,
                offset=0,
            )
            available = max(preview["total_effects"] - int(export_offset), 0)
            export_rows = min(available, int(export_limit))
            st.caption(
                f"조건에 맞는 효과 {preview['total_effects']:,}건 · "
                f"이번 파일에 최대 {export_rows:,}행 포함"
            )
            if preview["total_effects"] == 0:
                st.info("선택한 조건에 해당하는 재분석 결과가 없습니다.")
            json_col, csv_col = st.columns(2)
            with json_col:
                st.download_button(
                    "연구 효과 JSON",
                    data=lambda: api_download(
                        "/export/evidence", **export_filters, format="json"
                    ),
                    file_name="nutrigenomics-evidence.json",
                    mime="application/json",
                    key="bulk-evidence-json",
                    disabled=export_rows == 0,
                )
            with csv_col:
                st.download_button(
                    "연구 효과 CSV",
                    data=lambda: api_download(
                        "/export/evidence", **export_filters, format="csv"
                    ),
                    file_name="nutrigenomics-evidence.csv",
                    mime="text/csv",
                    key="bulk-evidence-csv",
                    disabled=export_rows == 0,
                )
            st.caption(
                "결과가 100,000행을 넘으면 offset을 늘려 여러 파일로 받으세요. "
                "분석 manifest와 QC 파일은 ‘연구 상세’ 화면에서 받을 수 있습니다."
            )
        except requests.RequestException as error:
            st.error(f"연구 결과 내보내기를 준비하지 못했습니다: {error}")

