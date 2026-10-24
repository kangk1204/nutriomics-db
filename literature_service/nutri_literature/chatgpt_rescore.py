"""Additive correction of the predeclared saved v6 baseline profile selection.

The frozen extractor, prompts, raw responses, predictions and native scoring
rules are unchanged. The original evaluator validates their provenance first.
Only its exact known baseline-selection guard can activate this correction.
"""

from collections import Counter
from pathlib import Path

from . import benchmarks, chatgpt_evaluation as frozen, evaluation_v6
from .contract_v7 import canonical, sha

KNOWN_BASELINE_ERROR = "Baseline16/82/138 or the154-relation denominator failed reproduction"
ARMS = ("existing_algorithm", "chatgpt_only", "hybrid")


def baseline_profile(saved):
    """Use the predeclared existing-ID union, never select by measured scores."""
    if "recall_ensemble" not in saved:
        return saved, "flat_relations_compatibility"
    profile = saved["recall_ensemble"]
    if not isinstance(profile, dict) or not isinstance(profile.get("relations"), list):
        raise ValueError("Saved recall_ensemble baseline profile is malformed")
    return profile, "recall_ensemble"


def _correction_contract(output):
    original = frozen._read(output / "contract.json")
    receipt = {
        "schema_version": "literature-chatgpt-baseline-adapter-correction-v1",
        "correction_kind": "saved_baseline_profile_selection_only",
        "selection_rule": "baseline.recall_ensemble when present; flat baseline only for compatibility",
        "reason": "The frozen evaluator selected the saved default relations instead of the predeclared existing-ID fixed union profile.",
        "original_contract_sha256": sha(canonical(original)),
        "original_evaluator_source_sha256": sha(Path(frozen.__file__).read_bytes()),
        "corrected_adapter_source_sha256": sha(Path(__file__).read_bytes()),
        "native_scorer_source_sha256": sha(Path(evaluation_v6.__file__).read_bytes()),
        "original_evaluator_expected_error": KNOWN_BASELINE_ERROR,
        "expected_baseline_counts": dict(zip(("tp", "fp", "fn"), frozen.BASELINE_COUNTS)),
        "expected_gold_relations": frozen.GOLD_RELATIONS,
        "prompt_or_model_changed": False,
        "prediction_or_baseline_artifacts_changed": False,
        "native_metric_definition_changed": False,
        "selection_uses_chatgpt_metrics": False,
    }
    frozen._durable(output / "comparison-correction-contract.json", receipt)
    return receipt


def score_corrected_experiment(output, home):
    """Correct only baseline loading after the frozen full provenance preflight."""
    output = Path(output)
    correction = _correction_contract(output)
    try:
        original_report = frozen.evaluate_experiment(output, home)
    except ValueError as error:
        if str(error) != KNOWN_BASELINE_ERROR:
            raise
    else:
        # Flat fixture/legacy baselines that already reproduce the declared
        # profile require no correction and keep their original immutable report.
        return original_report

    contract, _ = frozen._contract(output)
    if len(contract["source_hashes"]) != frozen.PAPERS:
        raise ValueError("Corrected scoring requires the same complete cohort")
    locator = frozen._read(output / "scoring-locator.json")
    gold, receipt, gold_sha = benchmarks._load_gold(frozen._settings(home), locator["gold_path"])
    if (gold_sha != contract["gold_sha256"] or gold_sha != frozen.GOLD_SHA256
            or receipt.get("inputs_sha256") != contract["input_sha256"]
            or sha(canonical(receipt)) != locator["prepared_manifest_sha256"]):
        raise ValueError("Corrected scoring release differs from frozen inference contract")
    labels = {doc["pmid"]: doc for doc in gold["documents"]}
    if not set(contract["source_hashes"]) <= labels.keys():
        raise ValueError("Corrected scoring gold cohort differs")

    # Reproduce the predeclared baseline before deriving any ChatGPT scores.
    baseline_rows, selected_profiles = {}, Counter()
    for pmid in contract["source_hashes"]:
        selected, profile = baseline_profile(frozen._read(output / "baseline" / ("paper-" + pmid + ".json")))
        selected_profiles[profile] += 1
        expected, actual, directed, predicted_direction = evaluation_v6.relation_sets(labels[pmid], selected)
        baseline_rows[pmid] = {"relation": benchmarks._metrics(expected, actual),
                               "relation_direction": benchmarks._metrics(directed, predicted_direction)}
    baseline = frozen._metric([row["relation"] for row in baseline_rows.values()])
    if ((baseline["tp"], baseline["fp"], baseline["fn"]) != frozen.BASELINE_COUNTS
            or baseline["tp"] + baseline["fn"] != frozen.GOLD_RELATIONS):
        raise ValueError("Corrected saved baseline profile still fails16/82/138 or154 denominator")

    per_paper, predictions, decisions = [], {}, Counter()
    for pmid in contract["source_hashes"]:
        prediction = frozen._read(output / "predictions" / ("paper-" + pmid + ".json"))
        predictions[pmid] = prediction
        row = {"pmid": pmid, "existing_algorithm": baseline_rows[pmid]}
        for name in ("chatgpt_only", "hybrid"):
            expected, actual, directed, predicted_direction = evaluation_v6.relation_sets(labels[pmid], prediction[name])
            row[name] = {"relation": benchmarks._metrics(expected, actual),
                         "relation_direction": benchmarks._metrics(directed, predicted_direction)}
        for decision in prediction["hybrid"]["decisions"]:
            decisions.update(decision["reasons"])
        per_paper.append(row)
    metrics = {name: {endpoint: frozen._metric([row[name][endpoint] for row in per_paper])
                      for endpoint in ("relation", "relation_direction")} for name in ARMS}
    differences = {}
    for left, right in (("existing_algorithm", "chatgpt_only"), ("existing_algorithm", "hybrid"), ("chatgpt_only", "hybrid")):
        a, b = metrics[left]["relation"], metrics[right]["relation"]
        differences[right + "_minus_" + left] = {
            "delta": {key: b[key] - a[key] if a[key] is not None and b[key] is not None else None
                      for key in ("tp", "fp", "fn", "precision", "recall", "f1")},
            "paired_paper_bootstrap2000_ci95": frozen._bootstrap(
                {row["pmid"]: row[left]["relation"] for row in per_paper},
                {row["pmid"]: row[right]["relation"] for row in per_paper}, contract["seed"])}
    report = {
        "schema_version": "literature-chatgpt-comparison-report-v2", "papers": len(per_paper),
        "gold_relations": frozen.GOLD_RELATIONS, "metrics": metrics, "differences": differences,
        "per_paper": per_paper, "hybrid_rejection_reasons": dict(decisions),
        "parse_failures": sum(bool(p["chatgpt_only"]["parse_errors"]) for p in predictions.values()),
        "standalone_raw_relation_records": sum(p["chatgpt_only"]["raw_relation_count"] for p in predictions.values()),
        "actual_models": sorted({p["model"] for p in predictions.values()}),
        "requested_model": contract["model"], "reasoning_effort": contract["reasoning_effort"],
        "contract_sha256": sha(canonical(contract)), "input_sha256": contract["input_sha256"], "gold_sha256": gold_sha,
        "source_only_predictions_before_gold": True, "same_raw_response_for_both_arms": True,
        "source_alignment": contract["source_alignment"], "evaluation_scope": contract["test_status"],
        "nutrition_accuracy": "unmeasured", "default_engine": "heuristic-v1", "sealed_test_opened": False,
        "baseline_adapter_correction": {**correction, "selected_profiles": dict(selected_profiles),
                                         "original_full_provenance_preflight_passed": True,
                                         "predeclared_baseline_reproduced": True},
    }
    frozen._durable(output / "comparison.json", report)
    lines = ["# ChatGPT 문헌 관계 추출 비교", "",
             "동일 개발 문헌과 동일 평가기로 측정한 결과입니다. 독립 최종 성능과 영양학 정확도는 미측정입니다.", "",
             "| 구성 | 정탐 | 오탐 | 누락 | 정밀도 | 재현율 | F1 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for name in ARMS:
        m = metrics[name]["relation"]
        percentages = ["미정의" if m[key] is None else f"{m[key] * 100:.2f}%" for key in ("precision", "recall", "f1")]
        lines.append(f"| {name} | {m['tp']} | {m['fp']} | {m['fn']} | " + " | ".join(percentages) + " |")
    lines.extend(["", "기존 알고리즘은 사전에 지정한 저장 결과의 recall_ensemble 구성을 사용했습니다. 기본 relations 필드를 잘못 선택한 평가 연결 오류만 수정했습니다.",
                  "원본 계약·모델·프롬프트·응답·예측과 관계 평가 정의는 유지했습니다. 수정 명세와 소스 해시는 comparison-correction-contract.json에 보존했습니다.",
                  "", "혼합 구성은 같은 ChatGPT 원본 응답에 원문·ID·의미 검증을 적용했습니다. 보류한 관계도 전체 정답 분모에 남습니다.",
                  "", "구성별 차이와 논문 단위 bootstrap 2,000회 구간은 comparison.json에 기록했습니다."])
    data = "\n".join(lines) + "\n"
    path = output / "comparison.md"
    if path.exists() and path.read_text(encoding="utf-8") != data:
        raise ValueError("Immutable corrected comparison text changed")
    if not path.exists():
        import os
        with path.open("x", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    return report
