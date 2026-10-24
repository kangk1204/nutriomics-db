"""Produce the final five-configuration comparison from completed real runs."""
import csv
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def native_error_analysis(folder, paper_ids):
    """Descriptive native-key errors; no predictions or model choices change."""
    from nutri_literature import benchmarks, evaluation_v6
    from nutri_literature.chatgpt_evaluation import _settings
    locator = json.loads((folder / "scoring-locator.json").read_bytes())
    gold, _, _ = benchmarks._load_gold(_settings(ROOT / "runtime/chatgpt-inputs"), locator["gold_path"])
    labels = {doc["pmid"]: doc for doc in gold["documents"]}
    counts = {name: Counter() for name in ("chatgpt_only", "hybrid")}
    for pmid in paper_ids:
        prediction = json.loads((folder / "predictions" / ("paper-" + pmid + ".json")).read_bytes())
        for arm, categories in counts.items():
            expected, actual, _, _ = evaluation_v6.relation_sets(labels[pmid], prediction[arm])
            gold_pairs = {key[:2] for key in expected}
            predicted_pairs = {key[:2] for key in actual}
            for key in actual - expected:
                category = ("unresolved_id" if any(i.startswith("UNRESOLVED") for i in key[1])
                            else "wrong_type_on_gold_pair" if key[:2] in gold_pairs
                            else "wrong_pair_or_entity")
                categories["fp_" + category] += 1
            for key in expected - actual:
                category = "wrong_or_missing_type_on_present_pair" if key[:2] in predicted_pairs else "gold_pair_absent"
                categories["fn_" + category] += 1
    return {name: dict(value) for name, value in counts.items()}


def summarize():
    from nutri_literature.chatgpt_evaluation import _bootstrap
    from nutri_literature.contract_v7 import canonical, sha
    home = ROOT / "runtime/chatgpt-comparison"
    names = [("gpt-6.1-sol", "medium"), ("gpt-6-luna", "max")]
    reports, profiles, latency, validation, response_ids = [], [], [], [], []
    for model, effort in names:
        folder = home / (model + "-" + effort + "-" + sha(model + ":" + effort)[:8])
        path = folder / "comparison.json"
        if not path.exists():
            return {"status": "pending", "missing": str(path)}
        report = json.loads(path.read_bytes())
        if report["papers"] != 100 or report["gold_relations"] != 154:
            raise ValueError("Incomplete scientific comparison")
        reports.append(report)
        validation.append({"model": model, "effort": effort,
                           "parse_failures": report["parse_failures"],
                           "standalone_raw_relation_records": report["standalone_raw_relation_records"],
                           "hybrid_rejection_reasons": report["hybrid_rejection_reasons"],
                           "baseline_adapter_correction": report.get("baseline_adapter_correction"),
                           "native_error_analysis": native_error_analysis(folder, [p["pmid"] for p in report["per_paper"]])})
        for arm, errors in validation[-1]["native_error_analysis"].items():
            metric = report["metrics"][arm]["relation"]
            if any(sum(n for key, n in errors.items() if key.startswith(kind + "_")) != metric[kind]
                   for kind in ("fp", "fn")):
                raise ValueError("Error categories do not reproduce the scored counts")
        if not profiles:
            profiles.append({"configuration": "기존 알고리즘 v6", "model": "heuristic-v6", "effort": None,
                             "endpoint": "existing_algorithm", "metrics": report["metrics"]["existing_algorithm"],
                             "per_paper": {p["pmid"]: p["existing_algorithm"]["relation"] for p in report["per_paper"]}})
        elif report["metrics"]["existing_algorithm"] != profiles[0]["metrics"]:
            raise ValueError("Existing algorithm reproduction differs between models")
        for arm, label in (("chatgpt_only", "단독"), ("hybrid", "혼합")):
            profiles.append({"configuration": model + " " + effort + " · " + label,
                             "model": model, "effort": effort, "endpoint": arm,
                             "metrics": report["metrics"][arm],
                             "per_paper": {p["pmid"]: p[arm]["relation"] for p in report["per_paper"]}})
        responses = [json.loads(p.read_bytes()) for p in (folder / "responses").glob("*.json")]
        if len(responses) != 100 or any(r.get("test_only") for r in responses):
            raise ValueError("Only100 real completed responses may enter the final summary")
        if (len({r["request_sha256"] for r in responses}) != 100
                or any(r.get("status") != "completed" or r.get("actual_model") != model
                       or r.get("reasoning_effort") != effort or not r.get("response_id") for r in responses)):
            raise ValueError("Real completed model/effort or unique-request audit failed")
        response_ids.extend(r["response_id"] for r in responses)
        seconds = sorted(r["elapsed_seconds"] for r in responses)
        latency.append({"model": model, "effort": effort, "real_completed_requests": 100,
                        "mean_seconds": statistics.mean(seconds), "median_seconds": statistics.median(seconds),
                        "p95_seconds": seconds[94],
                        "input_tokens": sum(r.get("usage", {}).get("input_tokens", 0) for r in responses),
                        "output_tokens": sum(r.get("usage", {}).get("output_tokens", 0) for r in responses),
                        "reasoning_tokens": sum(r.get("usage", {}).get("output_tokens_details", {}).get("reasoning_tokens", 0) for r in responses),
                        "actual_models": sorted({r.get("actual_model") or r["model"] for r in responses}),
                        "report_sha256": sha(path.read_bytes())})
    if len(set(response_ids)) != 200:
        raise ValueError("The two models must have200 distinct real response IDs")
    for key in ("input_sha256", "gold_sha256"):
        if reports[0][key] != reports[1][key]:
            raise ValueError("Model comparisons do not share the same source and labels")
    differences = []
    for current in profiles[1:]:
        a, b = profiles[0]["metrics"]["relation"], current["metrics"]["relation"]
        differences.append({"configuration": current["configuration"],
            "delta_vs_existing": {k: None if a[k] is None or b[k] is None else b[k] - a[k]
                                   for k in ("tp", "fp", "fn", "precision", "recall", "f1")},
            "paired_paper_bootstrap2000_ci95": _bootstrap(profiles[0]["per_paper"], current["per_paper"], 20261017)})
    for arm in ("chatgpt_only", "hybrid"):
        sol = next(p for p in profiles if p["model"] == "gpt-6.1-sol" and p["endpoint"] == arm)
        luna = next(p for p in profiles if p["model"] == "gpt-6-luna" and p["endpoint"] == arm)
        differences.append({"configuration": "Luna max minus Sol medium · " + arm,
                            "paired_paper_bootstrap2000_ci95": _bootstrap(sol["per_paper"], luna["per_paper"], 20261017)})
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "status": "complete",
              "unique_papers": 100, "gold_relations_per_configuration": 154, "real_model_requests": 200,
              "distinct_real_response_ids": len(set(response_ids)), "actual_model_and_effort_verified": True,
              "input_sha256": reports[0]["input_sha256"], "gold_sha256": reports[0]["gold_sha256"],
              "configurations": profiles, "differences": differences, "request_performance": latency,
              "validation_diagnostics": validation,
              "hybrid_definition": "same model output plus exact-source alignment, official ID/alias/taxon and semantic validation; no new relation/ID recovery",
              "model_effort_comparison": "Sol medium versus Luna max; this does not isolate model identity from reasoning effort",
              "evaluation_scope": "repeatedly exposed development100, not independent final performance",
              "nutrition_accuracy": "unmeasured", "sealed_test_opened": False, "default_engine": "heuristic-v1",
              "usage_scope": "200 scored requests only; excludes protocol smoke/pilot requests and is not total account usage",
              "source_report_sha256": [sha((home / (m + '-' + e + '-' + sha(m+':'+e)[:8]) / 'comparison.json').read_bytes()) for m,e in names]}
    path = home / "five-configuration-comparison.json"
    # Keep the scientific content stable when the summary command is rerun.
    if path.exists():
        previous = json.loads(path.read_bytes())
        report["created_at"] = previous["created_at"]
        if canonical(previous) != canonical(report):
            raise ValueError("Final summary differs; do not overwrite an existing report")
    else:
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = [["구성", "정탐", "오탐", "누락", "정밀도", "재현율", "F1"]]
    for profile in profiles:
        metric = profile["metrics"]["relation"]
        rows.append([profile["configuration"], metric["tp"], metric["fp"], metric["fn"],
                     *["미정의" if metric[k] is None else f"{100 * metric[k]:.2f}%" for k in ("precision", "recall", "f1")]])
    lines = ["# 개인 ChatGPT 연동 · 동일100편 관계 추출 비교", "",
             "동일 원문 100편과 정답 관계 154건, 동결 v6 평가기로 측정했습니다. 주요 지표는 관계 유형과 표준 ID의 동시 일치입니다. 방향성은 별도 JSON에 기록했습니다.", "",
             "| " + " | ".join(rows[0]) + " |", "|---|---:|---:|---:|---:|---:|---:|"]
    lines.extend("| " + " | ".join(map(str, row)) + " |" for row in rows[1:])
    lines += ["", "이번 구성의 주요 F1은 기존 v6보다 모두 낮아 성능 개선으로 판정하지 않았습니다. Luna 혼합은 채택한 관계가0건이므로 정밀도는 미정의입니다. Sol 혼합의60%는 예측5건 중 정탐3건에 대한 값입니다."]
    for result, perf, report in zip(validation, latency, reports):
        m = report["metrics"]["chatgpt_only"]["relation"]
        unresolved = result["native_error_analysis"]["chatgpt_only"].get("fp_unresolved_id", 0)
        lines += ["", f"{result['model']} {result['effort']}: 파싱 실패{result['parse_failures']}건. 오탐{m['fp']}건 중{unresolved}건에 미확정 ID가 포함됐습니다. 요청당 평균{perf['mean_seconds']:.2f}초, 입력{perf['input_tokens']:,}토큰·출력{perf['output_tokens']:,}토큰입니다. ID가 확정돼도 관계 자체가 정답이라는 뜻은 아닙니다."]
    lines += ["", "두 모델 모두 원문만 입력했고 정답 개체·ID·관계와 기존 엔진 예측을 입력하지 않았습니다. 정확한 인용문의 문자 위치는 동일한 자체 문자열 정렬기로 연결했습니다.", "",
              "기준선은 사전에 지정한 기존 ID 유지·후보 결합(recall_ensemble)으로 16정탐·82오탐·138누락을 재현했습니다. 저장 파일의 다른 기본 구성을 읽던 연결 오류만 별도 어댑터에서 수정하고 수정 명세·해시를 보존했습니다. 프롬프트·응답·예측·평가 공식은 변경하지 않았습니다.", "",
              "혼합은 같은 모델 응답을 검증한 구성입니다. 새로운 관계나 ID를 복원하는 모델은 포함하지 않으며, 기각한 정답은 누락으로 계산합니다.", "",
              "Sol medium과 Luna max는 추론 수준이 다른 구성입니다. 반복 노출된 공개 개발 평가이므로 독립 최종 성능과 영양학 정확도로 주장하지 않습니다. 기본 엔진은 v1을 유지합니다.", "",
              "원문·정답 해시, 논문별 정탐·오탐·누락, 논문 bootstrap 2,000회 구간과 요청 시간·토큰은 JSON에 보존했습니다. 사용량 집계는 본 평가 200요청만 포함하며 초기 통신 점검·시범 요청과 전체 계정 사용량은 포함하지 않습니다.", ""]
    (home / "five-configuration-comparison.md").write_text("\n".join(lines), encoding="utf-8")
    with (home / "five-configuration-comparison.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        csv.writer(stream).writerows(rows)
    return {"status": "complete", "path": str(path), "configurations": [{"name": p["configuration"], **p["metrics"]["relation"]} for p in profiles]}


if __name__ == "__main__":
    print(json.dumps(summarize(), ensure_ascii=False))
