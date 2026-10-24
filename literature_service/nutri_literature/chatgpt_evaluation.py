"""Immutable, source-first three-way comparison on the existing development100.

Inference is supplied by the authenticated official client. This module never
reads a login credential and never sends labels or algorithm predictions to it.
"""

from collections import Counter
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import random

from .contract_v7 import canonical, sha
from .chatgpt_extraction import INSTRUCTIONS, SCHEMA, dictionary_index, project, source_input

INPUT_SHA256 = "59e03449a6320b701c0f7a2416662e658c94d8817243c99c8f297ed89b37735a"
GOLD_SHA256 = "01fe711f2c3387572a7ed025e91d0fd62b8cc12046ca2fb3a04769ccfb3cecdf"
PAPERS = 100
GOLD_RELATIONS = 154
BASELINE_COUNTS = (16, 82, 138)
SEED = 20261017


def _read(path):
    path = Path(path)
    raw = path.read_bytes()
    return json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)


def _durable(path, value):
    path = Path(path)
    data = canonical(value).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError("Immutable experiment artifact differs: " + str(path))
        return path
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def _settings(home):
    from .settings import Settings
    return Settings(Path(home), Path(__file__).parents[1] / "config/topics.json")


def _prepared(home, prepared_dir=None):
    if prepared_dir:
        directory = Path(prepared_dir)
        return {"inputs_path": str(directory / "inputs.json"), "gold_path": str(directory / "gold.json")}
    index = _read(Path(home) / "public_benchmarks/v2-measurement-index.json")
    return index["steps"]["prepare:development:bioredirect:abstract"]


def prepare_experiment(home, baseline, output, model, dictionary_path=None, prepared_dir=None, reasoning_effort=None):
    """Freeze source, baseline, prompt and dictionary; do not open any gold file."""
    from . import benchmarks
    from . import evaluation_v6
    output, baseline = Path(output), Path(baseline)
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Select an available official model before freezing the experiment")
    if reasoning_effort not in (None, "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
        raise ValueError("Invalid reasoning effort")
    prepared = _prepared(home, prepared_dir)
    inputs, receipt, input_sha = benchmarks._load_inputs(_settings(home), prepared["inputs_path"])
    if (input_sha != INPUT_SHA256 or receipt.get("gold_sha256") != GOLD_SHA256
            or receipt.get("corpus") != "bioredirect" or receipt.get("split") != "development"
            or not receipt.get("complete_partition") or not receipt.get("official_split")):
        raise ValueError("Only the pinned exposed development cohort is permitted; protected inputs are forbidden")
    documents = sorted(inputs["documents"], key=lambda d: sha("v3-development-20261003:" + d["pmid"]))[:PAPERS]
    if len(documents) != PAPERS or len({d["pmid"] for d in documents}) != PAPERS:
        raise ValueError("The complete development cohort is required")
    source_hashes, baseline_hashes = {}, {}
    for document in documents:
        pmid, text = document["pmid"], document["text"]
        if not isinstance(pmid, str) or not pmid.isdigit() or sha(text) != document["source_hash"]:
            raise ValueError("Invalid source identity")
        raw = (baseline / ("paper-" + pmid + ".json")).read_bytes()
        saved = json.loads(raw)
        if saved.get("source_hash") != sha(text) or not isinstance(saved.get("relations"), list):
            raise ValueError("Existing algorithm/source mismatch")
        # Copy predictions for evaluation only, never into inference inputs.
        _durable(output / "baseline" / ("paper-" + pmid + ".json"), saved)
        baseline_hashes[pmid] = sha(canonical(saved))
        source = {"pmid": pmid, "text": text, "source_hash": sha(text)}
        _durable(output / "sources" / ("paper-" + pmid + ".json"), source)
        source_hashes[pmid] = sha(canonical(source))
    dictionary_sha, dictionary_file = None, None
    if dictionary_path is not None:
        dictionary = _read(dictionary_path)
        dictionary_index(dictionary)  # Fail closed on unprovenanced identity data.
        dictionary_file = "dictionary.json.gz" if Path(dictionary_path).suffix == ".gz" else "dictionary.json"
        data = Path(dictionary_path).read_bytes()
        target = output / dictionary_file
        if target.exists() and target.read_bytes() != data:
            raise ValueError("Immutable dictionary changed")
        if not target.exists():
            with target.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        dictionary_sha = sha(data)
    sources = {name: sha(Path(__file__).with_name(name).read_bytes())
               for name in ("chatgpt_extraction.py", "chatgpt_evaluation.py")}
    contract = {
        "schema_version": "literature-chatgpt-comparison-v2", "model": model,
        "reasoning_effort": reasoning_effort,
        "cohort": "bioredirect-existing-development100", "papers": PAPERS,
        "expected_gold_relations": GOLD_RELATIONS, "input_sha256": input_sha,
        "gold_sha256": GOLD_SHA256, "source_hashes": source_hashes,
        "baseline_prediction_sha256": baseline_hashes,
        "baseline_name": "heuristic-v6 existing-ID fixed union",
        "prompt_sha256": sha(INSTRUCTIONS), "response_schema": SCHEMA,
        "source_alignment": "exact_literal_common_v2; unique source text or unique entity inside unique evidence; no first-occurrence ties",
        "dictionary_sha256": dictionary_sha, "dictionary_file": dictionary_file, "implementation_sha256": sources,
        "evaluator_sha256": sha(Path(evaluation_v6.__file__).read_bytes()),
        "scorer_sources_sha256": benchmarks.evaluator_source_hashes(),
        "bootstrap_iterations": 2000, "seed": SEED,
        "annotation_reference": "https://ftp.ncbi.nlm.nih.gov/pub/lu/BioRED/BioRED_Annotation_Guideline.pdf",
        "identity_validation": "official_exact_alias_and_explicit_species_membership; unresolved identities remain failures",
        "test_status": "exposed_development_not_independent_final",
        "default_engine": "heuristic-v1", "nutrition_accuracy": "unmeasured",
    }
    _durable(output / "instructions.json", {"instructions": INSTRUCTIONS})
    _durable(output / "contract.json", contract)
    # Deliberately separate the local scoring locator from source-only payloads.
    _durable(output / "scoring-locator.json", {
        "inputs_path": str(Path(prepared["inputs_path"]).resolve()),
        "gold_path": str(Path(prepared["gold_path"]).resolve()),
        "prepared_manifest_sha256": sha(canonical(receipt))})
    return status_experiment(output)


def _contract(output):
    from . import benchmarks
    output = Path(output)
    contract = _read(output / "contract.json")
    if contract["schema_version"] != "literature-chatgpt-comparison-v2":
        raise ValueError("Unknown comparison contract")
    if contract["prompt_sha256"] != sha(INSTRUCTIONS):
        raise ValueError("Prompt changed after freezing")
    if contract["scorer_sources_sha256"] != benchmarks.evaluator_source_hashes():
        raise ValueError("A transitive scorer source changed after freezing")
    for name, checksum in contract["implementation_sha256"].items():
        if sha(Path(__file__).with_name(name).read_bytes()) != checksum:
            raise ValueError("Implementation changed; create a new experiment")
    for pmid, checksum in contract["source_hashes"].items():
        if sha(canonical(_read(output / "sources" / ("paper-" + pmid + ".json")))) != checksum:
            raise ValueError("Experiment source changed")
        if sha(canonical(_read(output / "baseline" / ("paper-" + pmid + ".json")))) != contract["baseline_prediction_sha256"][pmid]:
            raise ValueError("Baseline prediction changed")
    dictionary = None
    if contract["dictionary_sha256"] is not None:
        path = output / contract["dictionary_file"]
        if sha(path.read_bytes()) != contract["dictionary_sha256"]:
            raise ValueError("Dictionary changed after freezing")
        value = _read(path)
        dictionary = dictionary_index(value)
    return contract, dictionary


def status_experiment(output):
    output = Path(output)
    contract = _read(output / "contract.json")
    completed = sum((output / "predictions" / ("paper-" + pmid + ".json")).exists()
                    for pmid in contract["source_hashes"])
    report = output / "comparison.json"
    return {"prepared": True, "papers": contract["papers"], "completed_papers": completed,
            "pending_papers": contract["papers"] - completed,
            "state": "evaluated" if report.exists() else "ready_to_score" if completed == contract["papers"] else "awaiting_authenticated_inference",
            "metrics": _read(report)["metrics"] if report.exists() else None,
            "hybrid_dictionary_available": contract["dictionary_sha256"] is not None,
            "default_engine": "heuristic-v1", "nutrition_accuracy": "unmeasured"}


def run_experiment(output, infer, max_papers=None):
    """Resumable synchronous batch. ``infer(instructions, input)`` is auth-owned.

    It returns text/model/response_id/usage and optionally status, incomplete,
    finish_reason and test_only. Tokens, headers and cookies are never persisted.
    """
    output = Path(output)
    contract, dictionary = _contract(output)
    identity = sha(canonical(contract))
    if infer is None:
        return status_experiment(output)
    if hasattr(infer, "model") and infer.model != contract["model"]:
        raise ValueError("Client model differs from frozen experiment")
    if hasattr(infer, "reasoning_effort") and infer.reasoning_effort != contract["reasoning_effort"]:
        raise ValueError("Client reasoning effort differs from frozen experiment")
    processed = 0
    for pmid in contract["source_hashes"]:
        target = output / "predictions" / ("paper-" + pmid + ".json")
        if target.exists():
            if _read(target)["contract_sha256"] != identity:
                raise ValueError("Cached prediction belongs to another experiment")
            continue
        if max_papers is not None and processed >= max_papers:
            break
        source = _read(output / "sources" / ("paper-" + pmid + ".json"))
        prompt_input = source_input(source)
        request = {"model": contract["model"], "instructions": INSTRUCTIONS, "input": prompt_input}
        if contract["reasoning_effort"] is not None:
            request["reasoning"] = {"effort": contract["reasoning_effort"]}
        request_sha = sha(canonical(request))
        _durable(output / "requests" / ("paper-" + pmid + ".json"), {
            "contract_sha256": identity, "request_sha256": request_sha, **request})
        raw_path = output / "responses" / ("paper-" + pmid + ".json")
        if raw_path.exists():
            response = _read(raw_path)
            if response["request_sha256"] != request_sha:
                raise ValueError("Response/request mismatch")
        else:
            try:
                received = infer(INSTRUCTIONS, prompt_input)
                # Whitelist safe response fields; auth-bearing objects cannot leak.
                response = {key: received.get(key) for key in (
                    "text", "model", "response_id", "usage", "status", "finish_reason", "incomplete", "test_only",
                    "account_profile_sha256", "actual_model", "elapsed_seconds", "reasoning_effort")}
                response.update({"request_sha256": request_sha, "contract_sha256": identity})
                if (not isinstance(response.get("text"), str) or not response.get("model")
                        or response.get("status") not in {None, "completed"}
                        or response.get("incomplete") or response.get("finish_reason") in {"length", "content_filter"}):
                    raise ValueError("Incomplete inference response")
                _durable(raw_path, response)
            except Exception as error:
                # Exception messages may contain secrets; retain only safe class.
                failure = {"pmid": pmid, "error_type": type(error).__name__,
                           "request_sha256": request_sha, "at": datetime.now(timezone.utc).isoformat()}
                _durable(output / "failures" / (pmid + "-" + sha(canonical(failure)) + ".json"), failure)
                return {**status_experiment(output), "state": "inference_failed", "failed_pmid": pmid,
                        "error_type": type(error).__name__}
        projections = project(source["text"], response["text"], dictionary)
        _durable(target, {"pmid": pmid, "source_hash": source["source_hash"],
                          "contract_sha256": identity, "request_sha256": request_sha,
                          "response_sha256": sha(canonical(response)), "model": response["model"],
                          "response_id": response.get("response_id"), "test_only": bool(response.get("test_only")),
                          **projections})
        processed += 1
    return status_experiment(output)


def _metric(rows):
    from .benchmarks import _from_counts
    return _from_counts(*(sum(row[field] for row in rows) for field in ("tp", "fp", "fn")))


def _bootstrap(before, after, seed):
    rng = random.Random(seed)
    ids = sorted(before)
    samples = {key: [] for key in ("precision", "recall", "f1")}
    for _ in range(2000):
        chosen = rng.choices(ids, k=len(ids))
        left, right = _metric([before[i] for i in chosen]), _metric([after[i] for i in chosen])
        for key in samples:
            if left[key] is not None and right[key] is not None:
                samples[key].append(right[key] - left[key])
    return {key: {"lower": sorted(values)[int(.025 * (len(values) - 1))],
                  "upper": sorted(values)[int(.975 * (len(values) - 1))], "valid_draws": len(values)} if values else None
            for key, values in samples.items()}


def evaluate_experiment(output, home):
    """Score only after every raw response AND both predictions are durable."""
    from . import benchmarks, evaluation_v6
    output = Path(output)
    contract, dictionary = _contract(output)
    if contract["dictionary_sha256"] is None:
        raise ValueError("A frozen official dictionary is required for the hybrid comparison")
    if sha(Path(evaluation_v6.__file__).read_bytes()) != contract["evaluator_sha256"]:
        raise ValueError("Frozen evaluator changed")
    identity, predictions = sha(canonical(contract)), {}
    for pmid in contract["source_hashes"]:
        path = output / "predictions" / ("paper-" + pmid + ".json")
        if not path.exists():
            raise ValueError("Full-cohort inference must complete before gold is opened")
        prediction = _read(path)
        response = _read(output / "responses" / path.name)
        request = _read(output / "requests" / path.name)
        expected_request = {"model": contract["model"], "instructions": INSTRUCTIONS,
                            "input": source_input(_read(output / "sources" / path.name))}
        if contract["reasoning_effort"] is not None:
            expected_request["reasoning"] = {"effort": contract["reasoning_effort"]}
        reprojection = project(_read(output / "sources" / path.name)["text"], response["text"], dictionary)
        if (prediction["contract_sha256"] != identity or prediction["test_only"]
                or prediction["response_sha256"] != sha(canonical(response))
                or prediction["request_sha256"] != response["request_sha256"]
                or response["request_sha256"] != request["request_sha256"]
                or request["request_sha256"] != sha(canonical(expected_request))
                or any(request.get(k) != v for k, v in expected_request.items())
                or any(prediction[k] != reprojection[k] for k in ("chatgpt_only", "hybrid"))):
            raise ValueError("Prediction provenance mismatch or test-only inference")
        predictions[pmid] = prediction
    locator = _read(output / "scoring-locator.json")
    gold, receipt, gold_sha = benchmarks._load_gold(_settings(home), locator["gold_path"])
    if (gold_sha != contract["gold_sha256"] or receipt.get("inputs_sha256") != contract["input_sha256"]
            or sha(canonical(receipt)) != locator["prepared_manifest_sha256"]):
        raise ValueError("Scoring release differs from frozen inference contract")
    labels = {doc["pmid"]: doc for doc in gold["documents"]}
    if not set(predictions) <= labels.keys():
        raise ValueError("Gold cohort differs")
    per_paper, decisions = [], Counter()
    for pmid, prediction in predictions.items():
        row = {"pmid": pmid}
        for name, pred in (("existing_algorithm", _read(output / "baseline" / ("paper-" + pmid + ".json"))),
                           ("chatgpt_only", prediction["chatgpt_only"]), ("hybrid", prediction["hybrid"])):
            expected, actual, directed, predicted_direction = evaluation_v6.relation_sets(labels[pmid], pred)
            row[name] = {"relation": benchmarks._metrics(expected, actual),
                         "relation_direction": benchmarks._metrics(directed, predicted_direction)}
        for decision in prediction["hybrid"]["decisions"]:
            decisions.update(decision["reasons"])
        per_paper.append(row)
    arms = ("existing_algorithm", "chatgpt_only", "hybrid")
    metrics = {name: {endpoint: _metric([r[name][endpoint] for r in per_paper])
                      for endpoint in ("relation", "relation_direction")} for name in arms}
    baseline = metrics["existing_algorithm"]["relation"]
    if ((baseline["tp"], baseline["fp"], baseline["fn"]) != BASELINE_COUNTS
            or baseline["tp"] + baseline["fn"] != GOLD_RELATIONS):
        raise ValueError("Baseline16/82/138 or the154-relation denominator failed reproduction")
    differences = {}
    for left, right in (("existing_algorithm", "chatgpt_only"), ("existing_algorithm", "hybrid"), ("chatgpt_only", "hybrid")):
        a, b = metrics[left]["relation"], metrics[right]["relation"]
        differences[right + "_minus_" + left] = {
            "delta": {key: b[key] - a[key] if a[key] is not None and b[key] is not None else None
                      for key in ("tp", "fp", "fn", "precision", "recall", "f1")},
            "paired_paper_bootstrap2000_ci95": _bootstrap(
                {r["pmid"]: r[left]["relation"] for r in per_paper},
                {r["pmid"]: r[right]["relation"] for r in per_paper}, contract["seed"])}
    report = {"schema_version": "literature-chatgpt-comparison-report-v2", "papers": len(per_paper),
              "gold_relations": GOLD_RELATIONS, "metrics": metrics, "differences": differences,
              "per_paper": per_paper, "hybrid_rejection_reasons": dict(decisions),
              "parse_failures": sum(bool(p["chatgpt_only"]["parse_errors"]) for p in predictions.values()),
              "standalone_raw_relation_records": sum(p["chatgpt_only"]["raw_relation_count"] for p in predictions.values()),
              "actual_models": sorted({p["model"] for p in predictions.values()}),
              "requested_model": contract["model"], "reasoning_effort": contract["reasoning_effort"],
              "contract_sha256": identity, "input_sha256": contract["input_sha256"], "gold_sha256": gold_sha,
              "source_only_predictions_before_gold": True, "same_raw_response_for_both_arms": True,
              "source_alignment": contract["source_alignment"],
              "evaluation_scope": contract["test_status"], "nutrition_accuracy": "unmeasured",
              "default_engine": "heuristic-v1", "sealed_test_opened": False}
    _durable(output / "comparison.json", report)
    lines = ["# ChatGPT 문헌 관계 추출 비교", "", "동일 개발 문헌과 동일 평가기로 측정한 결과입니다. 독립 최종 성능과 영양학 정확도는 미측정입니다.", "",
             "| 구성 | 정탐 | 오탐 | 누락 | 정밀도 | 재현율 | F1 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for name in arms:
        m = metrics[name]["relation"]
        percentages = ["미정의" if m[k] is None else f"{m[k] * 100:.2f}%" for k in ("precision", "recall", "f1")]
        lines.append(f"| {name} | {m['tp']} | {m['fp']} | {m['fn']} | " + " | ".join(percentages) + " |")
    lines.extend(["", "혼합 구성은 같은 ChatGPT 원본 응답에 원문·ID·의미 검증을 적용했습니다. 보류한 관계도 전체 정답 분모에 남습니다.",
                  "", "구성별 차이와 논문 단위 bootstrap 2,000회 구간, 원본 응답 해시와 검증 사유는 comparison.json에 기록했습니다."])
    markdown = "\n".join(lines) + "\n"
    path = output / "comparison.md"
    if path.exists() and path.read_text(encoding="utf-8") != markdown:
        raise ValueError("Immutable comparison text changed")
    if not path.exists():
        with path.open("x", encoding="utf-8") as stream:
            stream.write(markdown)
            stream.flush()
            os.fsync(stream.fileno())
    return report
