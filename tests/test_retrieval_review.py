"""Synthetic protocol fixtures; these never become scientific gold or live calls."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from nutrigenomics.retrieval_review import (
    METHODS,
    binding,
    blank_review,
    evaluate_review,
    file_sha,
    generate_questions,
    load_package,
    machine_policy,
    metric_comparison,
    object_sha,
    prepare_review,
    read_json,
    require_holdout_authorization,
    split_audit,
    validate_human_gold,
    wilson,
    write_json,
)


@pytest.fixture
def package(tmp_path):
    studies = []
    science = {"release_id": "synthetic-public-v2", "science_records": [], "sources": []}
    source_rows, curated = [], []
    for sid in ("S01", "S02", "S03", "S04", "S05", "N06", "N14", "N15", "N16"):
        source_id = "source:" + sid
        exposure = {"id": sid + ":exposure", "study_id": sid, "label": "synthetic fat meal",
                    "component_keys": ["FDC-NUTRIENT:1004"], "food_terms": []}
        studies.append({"id": sid, "tissue": "whole blood", "exposures": [exposure]})
        for kind, value in [("study", {"id": sid, "title": "Synthetic study " + sid}), ("exposure", exposure)]:
            science["science_records"].append({"id": value["id"], "kind": kind, "metadata_json": json.dumps(value)})
        source_rows.append({"id": source_id, "url": "https://example.test/" + sid,
                            "source_kind": "primary_publication", "source_locations": ["Synthetic Methods"]})
        curated.append({"study_id": sid, "source_ids": [source_id], "verified_fields": {"tissue": "whole blood"}})
    science["science_records"].append({"id": "curation", "kind": "curation", "metadata_json": json.dumps({"studies": curated, "sources": source_rows})})
    curator = {"studies": studies}
    benchmark = tmp_path / "old-benchmark"
    benchmark.mkdir()
    old_queries = generate_questions(curator)["development"]
    for row in old_queries:
        row["candidate_relevant_study_ids"] = ["THIS_IS_NOT_GOLD"]
    write_json(benchmark / "development_queries.candidate.json", old_queries)
    write_json(benchmark / "manifest.json", {"curation_manifest_sha256": "original-input-unavailable",
        "files": {"development_queries.candidate.json": file_sha(benchmark / "development_queries.candidate.json"),
                  "holdout_queries.sealed.json": "DO-NOT-OPEN-THIS-FILE"}})
    # Opening/parsing this file would immediately fail and disclose the regression.
    (benchmark / "holdout_queries.sealed.json").write_text("THIS MUST NOT BE OPENED", encoding="utf-8")
    curator_path, science_path = tmp_path / "curator.json", tmp_path / "science.json"
    write_json(curator_path, curator)
    write_json(science_path, science)
    output = tmp_path / "retrieval-review" / "benchmark-review-v2"
    prepare_review(benchmark, curator_path, science_path, output)
    return output


def fake_transport(release="synthetic-public-v2", *, response=None, calls=None):
    def run(base, route, payload=None):
        if calls is not None:
            calls.append((route, payload))
        if route.endswith("/meta"):
            value = {"release_id": release}
        else:
            value = response if response is not None else {"release_id": release, "observed_evidence": [{
                "study_id": "S01", "study_evidence": {"species": "Homo sapiens"}, "causal_inference": False,
                "applicability": [{"dimension": key, "status": "unknown"} for key in ("tissue", "response_time_h", "regimen_duration_days")]}]}
        return value, object_sha(value)
    return run


def human_fixture(package, partition="holdout"):
    manifest, questions, catalog = load_package(package, partition)
    review = blank_review(questions, catalog, binding(manifest, partition))
    review.update(status="frozen", actor_type="human", reviewer="Synthetic Test Reviewer", reviewed_at="2026-10-20T01:00:00+00:00")
    review["attestation"] = {key: True for key in review["attestation"]}
    for row in review["query_reviews"]:
        row.update(decision="adjudicated", reason="Synthetic fixture only", complete_relevance_set=True)
        for judgment in row["candidate_judgments"]:
            study = next(s for s in catalog if s["study_id"] == judgment["study_id"])
            source = study["sources"][0]
            judgment.update(relevance="relevant" if judgment["study_id"] == "S01" else "irrelevant",
                            context_relation="related_study", reason="Synthetic source check",
                            source_citations=[{"source_id": source["source_id"], "source_record_sha256": source["source_record_sha256"],
                                               "source_location": "Synthetic Methods, line1"}])
    authorization = {"binding": binding(manifest, partition), "actor_type": "human", "reviewer": review["reviewer"],
                     "approved_at": "2026-10-20T01:01:00+00:00", "status": "authorized_once",
                     "human_gold_sha256": object_sha(review), "no_system_results_seen": True,
                     "protocol_amendment_accepted": True}
    return manifest, questions, catalog, review, authorization


def test_source_only_package_never_reads_original_sealed_file(package):
    manifest, dev, catalog = load_package(package, "development")
    _, holdout, _ = load_package(package, "holdout")
    assert len(dev) == 70 and len(holdout) == 30 and len(catalog) == 9
    assert not set(manifest["question_sets"]["development"]["groups"]) & set(manifest["question_sets"]["holdout"]["groups"])
    assert manifest["original_sealed_file_opened"] is False
    assert manifest["legacy_and_v2_development_question_projections_identical"] is True
    assert "THIS_IS_NOT_GOLD" not in (package / "questions.legacy-development.json").read_text()
    assert "candidate_relevant" not in (package / "questions.holdout.json").read_text()
    assert not (package / "source-proposals.holdout.json").exists()
    with pytest.raises(PermissionError, match="sealed"):
        read_json(package.parent.parent / "old-benchmark/holdout_queries.sealed.json")


def test_frozen_package_and_questions_cannot_be_silently_changed(package):
    path = package / "questions.development.json"
    path.write_text(path.read_text() + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA"):
        load_package(package, "development")


def test_holdout_workbook_contains_only_pending_judgments_not_proposals(package):
    template = read_json(package / "review.holdout.template.json")
    auth = read_json(package / "holdout-authorization.template.json")
    assert template["actor_type"] is None and template["status"] == "draft"
    assert len(template["query_reviews"]) == 30
    assert sum(len(row["candidate_judgments"]) for row in template["query_reviews"]) == 270
    assert all(j["relevance"] == "unjudged" and j["source_citations"] == []
               for row in template["query_reviews"] for j in row["candidate_judgments"])
    assert auth["status"] == "pending" and auth["actor_type"] is None
    assert not list(package.glob("*responses*"))


def test_review_binding_names_actual_manifest_file_sha_separately(package):
    template = read_json(package / "review.holdout.template.json")
    assert template["binding"]["benchmark_manifest_sha256"] == file_sha(package / "manifest.json")
    assert template["binding"]["benchmark_manifest_content_binding_sha256"] == read_json(package / "manifest.json")["manifest_binding_sha256"]


def test_validation_only_cli_never_requires_service_url(package, tmp_path):
    _, _, _, review, _ = human_fixture(package)
    path = tmp_path / "synthetic-human-review.json"
    write_json(path, review)
    script = Path(__file__).resolve().parents[1] / "scripts/evaluate_retrieval_review.py"
    result = subprocess.run([sys.executable, str(script), "--package", str(package), "--partition", "holdout",
                             "--human-gold", str(path), "--validate-only"], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert json.loads(result.stdout)["system_queries_executed"] == 0
    assert not (package.parent / "holdout-run-receipts").exists()


def test_question_origin_trial_and_pmid_overlap_is_detected(package):
    _, development, catalog = load_package(package, "development")
    _, holdout, _ = load_package(package, "holdout")
    catalog[0]["pubmed_id"] = "12345"
    catalog[1]["pubmed_id"] = "12345"
    result = split_audit({"development": development, "holdout": holdout}, catalog)
    assert result["study_id_overlap"] == []
    assert result["primary_pmid_overlap"] == ["12345"]


def test_manifest_binding_tamper_is_rejected(package):
    manifest = read_json(package / "manifest.json")
    manifest["release_id"] = "changed"
    write_json(package / "manifest.json", manifest)
    with pytest.raises(ValueError, match="manifest"):
        load_package(package, "development")


def test_holdout_missing_gold_is_rejected_before_transport(package, tmp_path):
    calls = []
    with pytest.raises(PermissionError, match="no HTTP"):
        evaluate_review(package, tmp_path / "eval", "http://example.test", partition="holdout", software_version="fixture",
                        transport=fake_transport(calls=calls))
    assert calls == []


@pytest.mark.parametrize("field,value", [("actor_type", "ai_source_proposal"), ("reviewer", "ChatGPT"), ("status", "draft")])
def test_ai_or_unfrozen_gold_cannot_self_approve(package, field, value):
    manifest, questions, catalog, review, _ = human_fixture(package)
    review[field] = value
    with pytest.raises(PermissionError):
        validate_human_gold(review, manifest, questions, catalog, "holdout")


def test_no_system_output_attestation_required(package):
    manifest, questions, catalog, review, _ = human_fixture(package)
    review["attestation"]["no_system_results_used"] = False
    with pytest.raises(PermissionError, match="Source-only"):
        validate_human_gold(review, manifest, questions, catalog, "holdout")


@pytest.mark.parametrize("mutation", ["binding", "query_hash", "source_hash", "missing_location", "missing_reason", "causal_confusion", "partial_complete"])
def test_relevance_gold_requires_hashes_locations_complete_and_noncausal_scope(package, mutation):
    manifest, questions, catalog, review, _ = human_fixture(package)
    row = review["query_reviews"][0]
    judgment = row["candidate_judgments"][0]
    if mutation == "binding":
        review["binding"]["release_id"] = "changed"
    elif mutation == "query_hash":
        row["question_sha256"] = "changed"
    elif mutation == "source_hash":
        judgment["source_citations"][0]["source_record_sha256"] = "changed"
    elif mutation == "missing_location":
        judgment["source_citations"][0]["source_location"] = ""
    elif mutation == "missing_reason":
        judgment["reason"] = ""
    elif mutation == "causal_confusion":
        judgment["context_relation"] = "food_prevents_disease"
    elif mutation == "partial_complete":
        judgment["relevance"] = "unjudged"
    with pytest.raises(PermissionError):
        validate_human_gold(review, manifest, questions, catalog, "holdout")


def test_holdout_requires_separate_once_protocol_authorization(package):
    manifest, questions, catalog, review, authorization = human_fixture(package)
    assert len(require_holdout_authorization(review, manifest, questions, catalog, authorization)) == 30
    authorization["protocol_amendment_accepted"] = False
    with pytest.raises(PermissionError, match="incomplete"):
        require_holdout_authorization(review, manifest, questions, catalog, authorization)


def test_holdout_authorization_not_silently_reused(package, tmp_path):
    _, _, _, review, authorization = human_fixture(package)
    first = evaluate_review(package, tmp_path / "run1", "http://example.test", partition="holdout", software_version="fixture",
                            human_gold=review, authorization=authorization, transport=fake_transport())
    assert first["query_count"] == 30
    calls = []
    with pytest.raises(PermissionError, match="already"):
        evaluate_review(package, tmp_path / "run2", "http://example.test", partition="holdout", software_version="fixture",
                        human_gold=review, authorization=authorization, transport=fake_transport(calls=calls))
    assert calls == []


def test_development_uses_same_candidate_universe_dedup_and_not_gold(package, tmp_path):
    calls = []
    result = evaluate_review(package, tmp_path / "dev", "http://example.test", software_version="fixture",
                             transport=fake_transport(calls=calls))
    assert result["query_count"] == 70 and result["available_queries"] == 70
    assert result["methods"] == list(METHODS)
    assert result["actual_unique_match_requests"] < 70
    assert len(calls) == result["actual_unique_match_requests"] + 1
    assert result["human_gold_scientific_comparison"] is None
    assert result["scientific_accuracy_status"] == "not_evaluated_no_human_gold"
    assert all(m["recall"] is None and m["scientific_precision"] is None for m in result["provisional_source_scope_comparison"].values())
    assert result["source_fact_candidates_are_gold"] is False


def test_live_release_mismatch_precedes_question_requests(package, tmp_path):
    calls = []
    with pytest.raises(ValueError, match="release"):
        evaluate_review(package, tmp_path / "run", "http://example.test", software_version="fixture",
                        transport=fake_transport(release="wrong", calls=calls))
    assert len(calls) == 1 and calls[0][0].endswith("/meta")


def test_actual_empty_query_is_no_answer_without_match_request(package, tmp_path):
    # Re-freeze a distinct synthetic question version, never alter a real benchmark.
    manifest = read_json(package / "manifest.json")
    path = package / "questions.legacy-development.json"
    questions = read_json(path)
    for question in questions:
        question.update(query="", component_keys=[], food_query=None)
    write_json(path, questions)
    manifest["question_sets"]["legacy-development"]["sha256"] = file_sha(path)
    manifest.pop("manifest_binding_sha256")
    manifest["manifest_binding_sha256"] = object_sha(manifest)
    write_json(package / "manifest.json", manifest)
    calls = []
    result = evaluate_review(package, tmp_path / "empty", "http://example.test", software_version="fixture",
                             transport=fake_transport(calls=calls))
    assert result["available_queries"] == 70 and result["actual_unique_match_requests"] == 0
    assert len(calls) == 1  # Metadata only; no empty match request.
    assert result["provisional_source_scope_comparison"]["bridge"]["precision_on_judged_pairs"]["estimate"] is None


def test_frozen_proposals_checked_before_live_requests(package, tmp_path):
    path = package / "source-proposals.legacy-development.json"
    path.write_text(path.read_text() + " ", encoding="utf-8")
    calls = []
    with pytest.raises(ValueError, match="leakage"):
        evaluate_review(package, tmp_path / "run", "http://example.test", software_version="fixture", transport=fake_transport(calls=calls))
    assert calls == []


def test_transport_failure_is_unavailable_not_zero_result(package, tmp_path):
    def unavailable(base, route, payload=None):
        if route.endswith("meta"):
            return {"release_id": "synthetic-public-v2"}, "synthetic"
        raise OSError("synthetic transport failure")
    result = evaluate_review(package, tmp_path / "run", "http://example.test", software_version="fixture", transport=unavailable)
    assert result["available_queries"] == 0
    assert all(row["status"] == "unavailable" and row["errors"] for row in result["outcomes"])
    assert result["provisional_source_scope_comparison"]["bridge"]["precision_on_judged_pairs"]["estimate"] is None


def test_precision_denominators_unjudged_and_trial_dependence():
    outcomes = [{"query_id": "q", "source_group": "trial:S01", "status": "available", "complete_relevance_set": False,
                 "methods": {method: {"returned_study_ids": ["A", "A", "B", "C"]} for method in METHODS}}]
    labels = [{"query_id": "q", "study_id": "A", "relevance": "relevant"},
              {"query_id": "q", "study_id": "B", "relevance": "irrelevant"}]
    result = metric_comparison(outcomes, labels, human=True)["bridge"]
    assert result["precision_on_judged_pairs"]["successes"] == 1
    assert result["precision_on_judged_pairs"]["denominator"] == 2
    assert result["counts"]["returned"] == 3 and result["counts"]["unjudged_returned"] == 1
    assert result["scientific_precision"] is None and result["recall"] is None
    assert "trial:S01" in result["per_source_trial"]


def test_empty_and_answerless_queries_are_normal_not_perfect_precision():
    outcomes = [{"query_id": "empty", "source_group": "trial:S01", "status": "available", "complete_relevance_set": True,
                 "methods": {method: {"returned_study_ids": []} for method in METHODS}}]
    result = metric_comparison(outcomes, [{"query_id": "empty", "study_id": "A", "relevance": "irrelevant"}], human=True)["bridge"]
    assert result["precision_on_judged_pairs"]["estimate"] is None
    assert result["answerless_query_correctness"]["successes"] == 1
    assert result["answerless_query_correctness"]["denominator"] == 1


def test_machine_contract_detects_causality_unknown_context_and_clinical_gap():
    result = machine_policy({"observed_evidence": [{"study_evidence": {"species": "Homo sapiens"}, "causal_inference": True,
                             "clinical_results": [{"value": 1}], "applicability": [{"dimension": "tissue", "status": "match"}]}],
                             "evidence_gaps": ["현재 이 오믹스 중재 조회에는 수치형 임상 결과가 연결되어 있지 않습니다"]}, {})
    assert set(result["failures"]) == {"causal_inference", "unspecified_dimension_remains_unknown", "clinical_gap_does_not_contradict_attached_records"}


def test_wilson_zero_invalid_and_nonzero_counts():
    assert wilson(0, 0)["estimate"] is None
    assert wilson(95, 100)["denominator"] == 100
    assert wilson(95, 100)["ci95"][0] < .95 < wilson(95, 100)["ci95"][1]
    with pytest.raises(ValueError):
        wilson(2, 1)
