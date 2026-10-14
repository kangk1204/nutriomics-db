from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import requests
import streamlit as st
from streamlit.testing.v1 import AppTest


class Response:
    def __init__(self, body):
        self.body = body
        self.content = b"{}"

    def raise_for_status(self):
        pass

    def json(self):
        return deepcopy(self.body)


@pytest.fixture
def app(monkeypatch):
    posted = []
    food = {"id": 1, "name": "Test food", "source_id": "TEST", "source_code": "food"}

    def get(url, **kwargs):
        if url.endswith("/foods"):
            return Response({"items": [food]})
        if url.endswith("/studies"):
            return Response({"items": []})
        if url.endswith("/meals/totals"):
            return Response({"meal_count": 0, "days": [], "note": "test"})
        if "/export/" in url:
            return Response({})
        if url.endswith("/provenance/ui-assertion"):
            return Response({"source_url": "https://example.org/source", "version": "v1"})
        raise AssertionError(f"Unexpected UI GET {url}")

    def post(url, json, **kwargs):
        posted.append((url, deepcopy(json)))
        if url.endswith("/meals/analyze"):
            return Response({
                "interpretation": "Related experiments only", "components": [],
                "unquantified": [], "evidence": [], "observed_evidence": [{
                    "id": "ui-assertion", "layer": "molecular_measurement", "study_id": "TEST",
                    "study_title": "Human paired test", "assertion": "Paired experimental result",
                    "exposure": {"label": "Test product", "amount": 800, "unit": "g"},
                    "applicability": {"dimensions": {"dose": "mismatch", "duration": "unknown"}},
                    "result_status": "reanalyzed", "results": [{"effect_estimate": -0.2, "q_value": 0.8}],
                    "series_accession": "GSETEST", "contrast": "post_pre",
                    "effect_selector": {"accession": "GSETEST", "contrast": "post_pre", "analysis_version": "v1"},
                    "clinical_endpoints": [], "limitations": ["No concurrent control"],
                    "provenance": {"source_url": "https://example.org/source"}, "export_allowed": True,
                }],
                "mechanistic_hypotheses": [], "evidence_gaps": ["No clinical endpoint"],
                "applicability": [], "provenance": [],
            })
        if url.endswith("/meals"):
            return Response({"id": 7})
        raise AssertionError(f"Unexpected UI POST {url}")

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    st.cache_data.clear()
    path = Path(__file__).resolve().parents[1] / "ui" / "app.py"
    instance = AppTest.from_file(str(path), default_timeout=30).run()
    assert not instance.exception
    yield instance, posted
    st.cache_data.clear()


def button(app, label):
    return next(element for element in app.button if element.label == label)


@pytest.mark.parametrize("auxiliary_files", ["sample_qc.tsv", ["sample_qc.tsv"]])
def test_study_auxiliary_scalar_and_list_render_exact_filename(app, monkeypatch, auxiliary_files):
    instance, _ = app
    original_get = requests.get
    downloaded = []
    run = {
        "analysis_version": "scalar-v1",
        "metadata": {"auxiliary_files": auxiliary_files},
        "outputs": [{"path": "/outputs/sample_qc.tsv"}, {"path": "/outputs/qc.tsv"}],
    }
    frozen_run = deepcopy(run)
    detail = {
        "accession": "GSETEST", "metadata_only_notice": None,
        "study_id": "TEST", "study_title": "Test study", "title": "Test series",
        "assay_type": "expression", "organism": "Homo sapiens", "platform_ids": [],
        "pubmed_id": None, "assay_data_downloaded": True,
        "counts": {}, "analysis_runs": [run], "samples": [], "effects": [],
    }

    def get(url, **kwargs):
        if url.endswith("/studies"):
            return Response({"items": [{"accession": "GSETEST"}]})
        if url.endswith("/studies/GSETEST"):
            return Response(detail)
        if "/analysis-runs/" in url and "/files/" in url:
            downloaded.append(url.rsplit("/", 1)[-1])
            response = Response({})
            response.content = b"sample\tapproved\n"
            return response
        return original_get(url, **kwargs)

    monkeypatch.setattr(requests, "get", get)
    st.cache_data.clear()
    instance.run()
    button(instance, "시료·분석결과 보기").click().run()
    assert not instance.exception
    assert downloaded == ["sample_qc.tsv"]
    assert instance.session_state["study_detail"]["analysis_runs"][0] == frozen_run
    assert detail["analysis_runs"][0] == frozen_run


def test_saved_meal_uses_analyzed_snapshot_after_streamlit_rerun(app):
    instance, posted = app
    button(instance, "식사에 추가").click().run()
    button(instance, "성분·근거 계산").click().run()
    assert not instance.exception
    analyzed = deepcopy(posted[-1][1])
    instance.text_input(key="meal_note").set_value("After analysis rerun").run()
    button(instance, "식사 기록 저장").click().run()
    assert not instance.exception
    assert posted[-1][0].endswith("/meals")
    assert posted[-1][1] == {**analyzed, "note": "After analysis rerun"}


def test_changed_draft_and_context_invalidate_analysis(app):
    instance, posted = app
    button(instance, "식사에 추가").click().run()
    button(instance, "성분·근거 계산").click().run()
    assert instance.session_state["meal_analysis_payload"]["items"] == [
        {"food_id": 1, "amount_g": 150.0}
    ]
    button(instance, "식사에 추가").click().run()
    assert not instance.exception
    assert "식사 기록 저장" not in [element.label for element in instance.button]
    button(instance, "성분·근거 계산").click().run()
    assert len(posted[-1][1]["items"]) == 2
    instance.checkbox(key="meal_context_response_known").check().run()
    assert "식사 기록 저장" not in [element.label for element in instance.button]
    button(instance, "성분·근거 계산").click().run()
    assert posted[-1][1]["reference_context"] == {
        "tissue": None, "response_time_h": 6.0, "regimen_duration_days": None,
    }
    assert not instance.exception


def test_observed_result_and_provenance_survive_ui_rerun(app):
    instance, _ = app
    button(instance, "식사에 추가").click().run()
    button(instance, "성분·근거 계산").click().run()
    assert any("Human paired test" in element.label and "Test product" in element.label for element in instance.expander)
    assert any("임상 결과가 없습니다" in element.value for element in instance.info)
    button(instance, "원문·분석 출처 확인").click().run()
    assert not instance.exception
    assert any("Human paired test" in element.label for element in instance.expander)


def test_contrast_and_fitted_model_scope_are_distinct_with_audit_provenance(app):
    instance, _ = app
    button(instance, "식사에 추가").click().run()
    button(instance, "성분·근거 계산").click().run()
    result = deepcopy(instance.session_state["meal_analysis"])
    result["observed_evidence"][0].update({
        "n_people": 14, "n_samples": 168,
        "contrast_metadata": {"sample_scope": {
            "contrast_people": 7, "directly_contributing_assays": 28,
            "fitted_model_people": 14, "fitted_model_assays": 168, "group_assays": 84,
        }},
        "analysis_provenance": {
            "version": "audited-v2",
            "inputs": [{"path": "normalized.parquet", "sha256": "a" * 64}],
            "outputs": [{"path": "effects.tsv", "sha256": "b" * 64}],
            "sample_scope": {"fitted_model_people": 14, "fitted_model_assays": 168},
            "audit_attachments": {"sample_scope": [{
                "path": "sample_scope_audit.json", "sha256": "c" * 64,
                "manual_expert_review_status": "pending",
            }]},
            "producer_output_audit": {
                "manifest": {"path": "producer_manifest.json", "sha256": "d" * 64, "bytes": 1234},
                "audit": {
                    "verification_status": "passed_with_declared_limitations",
                    "argv_status": "reconstructed_not_recorded",
                    "verification_scope": "Frozen source and output hashes; narrower historical audit",
                    "historical_limitations": [
                        "Output attribution is narrower than a recorded invocation",
                        "Wrapper snapshot was frozen after execution",
                    ],
                    "expert_manual_review": "pending",
                },
            },
            "expert_manual_review_status": "pending",
        },
    })
    instance.session_state["meal_analysis"] = result
    instance.run()
    assert not instance.exception
    frame = next(element.value for element in instance.dataframe if "독립 사람 수" in element.value.columns)
    rows = frame.set_index("범위")
    assert rows.loc["해당 대비에 직접 기여", "독립 사람 수"] == 7
    assert rows.loc["해당 대비에 직접 기여", "assay 수"] == 28
    assert rows.loc["전체 적합 모델", "독립 사람 수"] == 14
    assert rows.loc["전체 적합 모델", "assay 수"] == 168
    assert rows.loc["해당 군 전체", "assay 수"] == 84
    assert any("수동 전문가 검수: 대기 중 (pending)" in element.value for element in instance.caption)
    assert "분석 버전·입출력·시료 감사 출처 전체 보기" in [element.label for element in instance.expander]
    provenance_json = next(element.value for element in instance.json if "audit_attachments" in element.value)
    assert '"version": "audited-v2"' in provenance_json
    assert "sample_scope_audit.json" in provenance_json
    assert "producer_manifest.json" in provenance_json
    assert "reconstructed_not_recorded" in provenance_json
    assert any("원 실행 때 기록된 명령 인자가 아닙니다" in element.value for element in instance.warning)
    assert any("narrower historical audit" in element.value for element in instance.caption)
    assert any("Output attribution is narrower than a recorded invocation" in element.value for element in instance.markdown)
    assert any("추가 감사의 수동 전문가 검수: 대기 중 (pending)" in element.value for element in instance.caption)
    assert not any("독립 대상자: 14명" in element.value for element in instance.caption)


def test_shared_acute_chronic_assays_are_not_summed_as_unique_samples(app):
    instance, _ = app
    button(instance, "식사에 추가").click().run()
    button(instance, "성분·근거 계산").click().run()
    result = deepcopy(instance.session_state["meal_analysis"])
    acute = deepcopy(result["observed_evidence"][0])
    chronic = deepcopy(acute)
    records = []
    for record, label, assays in [(acute, "acute", 56), (chronic, "chronic", 28)]:
        record["id"] += label
        record["contrast"] = label
        record["analysis_provenance"] = {
            "version": label + "-v2", "expert_manual_review_status": "pending",
            "sample_scope": {
                "RNA_people": 7, "clinical_people": 13, "fitted_model_assays": assays,
                "unique_modeled_assays_across_acute_chronic": 70,
            },
        }
        records.append(record)
    result["observed_evidence"] = records
    instance.session_state["meal_analysis"] = result
    instance.run()
    assert not instance.exception
    scopes = [element.value for element in instance.dataframe if "독립 사람 수" in element.value.columns]
    assert [int(frame.iloc[0]["assay 수"]) for frame in scopes] == [56, 28]
    assert sum("중복 제거 assay: 70개" in element.value for element in instance.caption) == 2
    assert not any("중복 제거 assay: 84개" in element.value for element in instance.caption)
    assert any("RNA 대상자: 7명 · 별도 임상 대상자: 13명" in element.value for element in instance.caption)


def test_metadata_only_intervention_is_separate_from_observed_results(app):
    instance, _ = app
    button(instance, "식사에 추가").click().run()
    button(instance, "성분·근거 계산").click().run()
    result = deepcopy(instance.session_state["meal_analysis"])
    result["observed_evidence"][0].update({"result_status": "metadata_only", "results": []})
    result["evidence_gaps"] = [
        {"code": "no_results", "message": "중재 정보만 확보", "evidence_id": "one"},
        {"code": "no_results", "message": "중재 정보만 확보", "evidence_id": "two"},
    ]
    instance.session_state["meal_analysis"] = result
    instance.run()
    assert not instance.exception
    assert any("관련 중재 정보 · 분자 재분석 미확보" in element.value for element in instance.markdown)
    assert any("분자 재분석·임상 관찰 결과가 없습니다" in element.value for element in instance.info)
    assert any("Test product" in element.label and "중재 정보" in element.label for element in instance.expander)
    assert any("자료 부족 기록 전체 보기" == element.label for element in instance.expander)
    summary = next(element.value for element in instance.dataframe if "해당 기록 수" in element.value.columns)
    assert summary.iloc[0]["해당 기록 수"] == 2
