from __future__ import annotations

import statistics
from collections import Counter
from pathlib import Path

from .common import (POLICY, atomic, canonical, checkpoint, digest, filehash, groups,
                     home, metric, read, sources)
from .data import load
from .learning import (extract_features, fit_neural, labels_for, linear_fit,
                       linear_score, neural_score)


def score_sets(rows, predictions, labels, threshold, task):
    expected, observed = set(), set()
    for r, p in zip(rows, predictions):
        expected.update((r["pmid"], r["key"], label) for label in r["labels"] if label in labels)
        if task == "drugprot" and p["exists"] < threshold:
            continue
        observed.update((r["pmid"], r["key"], label) for label, value in zip(labels, p["types"]) if value >= threshold)
    result = metric(expected, observed)
    by_type = {label: metric({r for r in expected if r[2] == label}, {r for r in observed if r[2] == label}) for label in labels}
    result["per_type"] = by_type
    result["macro_f1"] = sum(v["f1"] for v in by_type.values())/len(by_type) if by_type else 0.
    result["gold_pair_condition"] = "provided_gold_arguments"
    result["full_end_to_end_event_score"] = False
    return result


def select_threshold(rows, scores, labels, task):
    choices = [{"threshold": t, **score_sets(rows, scores, labels, t, task)} for t in POLICY["thresholds"]]
    return max(choices, key=lambda v: (v["f1"], v["macro_f1"], v["recall"], v["threshold"]))


def configurations():
    return ([{"family": "frozen-linear", "C": c} for c in POLICY["C"]]
            + [{"family": "finetuned", "learning_rate": lr, "epochs": 5} for lr in POLICY["learning_rates"]])


def fit(rows, indices, valid_indices, task, labels, config, features):
    key = digest(canonical([task, [rows[i] for i in indices], [rows[i] for i in valid_indices], labels, config, sources()]))[:24]
    directory = home()/"models"/task/key
    if config["family"] == "frozen-linear":
        return linear_fit(rows, labels, features, indices, config["C"], directory)
    return fit_neural([rows[i] for i in indices], [rows[i] for i in valid_indices], task, labels, config, directory)


def score(rows, indices, labels, features, directory):
    name = "model.json" if (directory/"model.json").exists() else "model.safetensors"
    key = digest(canonical([filehash(directory/name), [rows[i] for i in indices], labels, sources()]))
    path = home()/"scores"/(key+".json")
    if not path.exists():
        predictions = linear_score(features, indices, directory) if name == "model.json" else neural_score([rows[i] for i in indices], labels, directory)
        atomic(path, {"predictions": predictions, "weights_sha256": filehash(directory/name)}, True)
    return read(path)["predictions"]


def select(rows, docs, indices, task, labels, features, salt):
    assignment = groups(docs, 3, salt)
    candidates = []
    for config in configurations():
        scores, validation_rows, epochs = [], [], []
        for k in range(3):
            checkpoint()
            training = [i for i in indices if assignment[rows[i]["pmid"]] != k]
            validation = [i for i in indices if assignment[rows[i]["pmid"]] == k]
            fitted = fit(rows, training, validation, task, labels, config, features)
            scores.extend(score(rows, validation, labels, features, fitted))
            validation_rows.extend(rows[i] for i in validation)
            if (fitted/"complete.json").exists():
                history = read(fitted/"complete.json")["history"]
                epochs.append(min(history, key=lambda v: v["validation_loss"])["epoch"])
        policy = select_threshold(validation_rows, scores, labels, task)
        candidates.append({"config": config, "policy": policy, "final_epochs": int(statistics.median(epochs)) if epochs else None})
    winner = max(candidates, key=lambda v: (v["policy"]["f1"], v["policy"]["macro_f1"],
        v["policy"]["recall"], v["config"]["family"] == "frozen-linear", -v["config"].get("C", 0)))
    return winner, candidates


def sum_metrics(values):
    tp, fp, fn = (sum(v[k] for v in values) for k in ("tp", "fp", "fn"))
    precision = tp/(tp+fp) if tp+fp else 0.
    recall = tp/(tp+fn) if tp+fn else 0.
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall,
            "f1": 2*precision*recall/(precision+recall) if precision+recall else 0.}


def encode():
    value = load()
    results = {}
    for task in ("drugprot", "cg"):
        rows = [r for r in value["pairs"] if r["task"] == task]
        results[task] = str(extract_features(rows, task, labels_for(value, task)).relative_to(home())).replace("\\", "/")
    atomic(home()/"features/manifest.json", {"features": results, "input_sha256": filehash(home()/"data/train.json")}, True)
    return results


def train(task):
    value = load()
    if task not in ("drugprot", "cg"):
        raise ValueError("미지원 native 학습 과제")
    rows = [r for r in value["pairs"] if r["task"] == task]
    labels = labels_for(value, task)
    pmids = {r["pmid"] for r in rows}
    docs = [d for d in value["documents"] if d["pmid"] in pmids]
    features = extract_features(rows, task, labels)
    assignment = groups(docs, 5, task+"-outer-20261023")
    outer, oof_rows, oof_predictions = [], [], []
    for k in range(5):
        training = [i for i, r in enumerate(rows) if assignment[r["pmid"]] != k]
        validation = [i for i, r in enumerate(rows) if assignment[r["pmid"]] == k]
        train_docs = [d for d in docs if assignment[d["pmid"]] != k]
        selected, candidates = select(rows, train_docs, training, task, labels, features, task+f"-inner-{k}")
        config = {**selected["config"]}
        if config["family"] == "finetuned":
            config["epochs"] = selected["final_epochs"]
        fitted = fit(rows, training, [], task, labels, config, features)
        predictions = score(rows, validation, labels, features, fitted)
        summary = {"outer_fold": k, "selected": selected, "candidates": candidates,
                   "metrics": score_sets([rows[i] for i in validation], predictions, labels, selected["policy"]["threshold"], task),
                   "train_pmids": sorted(d["pmid"] for d in train_docs),
                   "held_pmids": sorted(d["pmid"] for d in docs if assignment[d["pmid"]] == k)}
        ablations = {}
        for family in ("frozen-linear", "finetuned"):
            eligible = [v for v in candidates if v["config"]["family"] == family]
            choice = max(eligible, key=lambda v: (v["policy"]["f1"], v["policy"]["macro_f1"],
                v["policy"]["recall"], -v["config"].get("C", 0)))
            family_config = {**choice["config"]}
            if family == "finetuned":
                family_config["epochs"] = choice["final_epochs"]
            family_model = fit(rows, training, [], task, labels, family_config, features)
            family_scores = score(rows, validation, labels, features, family_model)
            ablations[family] = score_sets([rows[i] for i in validation], family_scores,
                                          labels, choice["policy"]["threshold"], task)
        summary["family_ablations"] = ablations
        atomic(home()/"cross-validation"/task/(f"outer-{k}.json"), summary, True)
        outer.append(summary)
        oof_rows.extend(rows[i] for i in validation)
        oof_predictions.extend(predictions)
    selected, candidates = select(rows, docs, list(range(len(rows))), task, labels, features, task+"-final-inner")
    config = {**selected["config"]}
    if config["family"] == "finetuned":
        config["epochs"] = selected["final_epochs"]
    fitted = fit(rows, list(range(len(rows))), [], task, labels, config, features)
    result = {"task": task, "labels": labels, "selected": selected, "candidates": candidates,
              "model": str(fitted.relative_to(home())).replace("\\", "/"), "outer_folds": outer,
              "training_papers": len(docs), "training_pairs": len(rows),
              "entity_condition": "provided_gold_arguments", "automatic_entity_condition": "not_measured",
              "full_event_extraction": False, "independent_final_performance": False,
              "core_model_transfer": False, "cross_task_label_mapping": False}
    result["nested_oof_metrics"] = sum_metrics([v["metrics"] for v in outer])
    result["family_ablations_oof"] = {family: sum_metrics([v["family_ablations"][family] for v in outer])
                                      for family in ("frozen-linear", "finetuned")}
    atomic(home()/"selected"/(task+".json"), result, True)
    atomic(home()/"cross-validation"/task/"oof-predictions.json", {"rows": oof_rows, "scores": oof_predictions}, True)
    return result


def freeze():
    selected = {task: read(home()/"selected"/(task+".json")) for task in ("drugprot", "cg")}
    files = [home()/"audit.json", home()/"policy.json", home()/"data/train.json"]
    for task, value in selected.items():
        files.append(home()/"selected"/(task+".json"))
        model = home()/value["model"]
        files.extend(p for name in ("model.json", "model.safetensors", "complete.json") if (p := model/name).exists())
    result = {"engine": "nutriomics-native-v8", "code": sources(),
              "files": {str(p.relative_to(home())).replace("\\", "/"): filehash(p) for p in files},
              "entity_condition": "provided_gold_arguments", "automatic_entities": "not_measured",
              "full_event_extraction": False, "core_model_transfer": False}
    atomic(home()/"freeze.json", result, True)
    return result


def verify_freeze():
    value = read(home()/"freeze.json")
    if value["code"] != sources():
        raise ValueError("native 동결 후 소스 변경")
    for relative, expected in value["files"].items():
        if filehash(home()/relative) != expected:
            raise ValueError("native 동결 파일 변경")
    return value


def predict():
    """Optional native held-out provided-pair inputs; labels must be separate."""
    verify_freeze()
    path = home()/"data/evaluation-inputs.json"
    if not path.exists():
        raise FileNotFoundError("공식 native 평가 source-only 참여자 입력 미준비")
    value = read(path)
    if value.get("entity_condition") != "provided_gold_arguments":
        raise FileNotFoundError("자동 개체 조건의 native 정답 위치 대조 어댑터 미준비")
    rows = value["pairs"]
    if any(any(k in r for k in ("labels", "target", "gold", "predicate")) for r in rows):
        raise ValueError("native 예측 입력에 정답 레이블 포함")
    output = []
    training = load()
    for task in ("drugprot", "cg"):
        task_rows = [r for r in rows if r["task"] == task]
        if not task_rows:
            continue
        selected = read(home()/"selected"/(task+".json"))
        labels = labels_for(training, task)
        model = home()/selected["model"]
        if selected["selected"]["config"]["family"] == "frozen-linear":
            features = extract_features(task_rows, task, labels)
            predictions = linear_score(features, list(range(len(task_rows))), model)
        else:
            predictions = neural_score(task_rows, labels, model)
        for row, prediction in zip(task_rows, predictions):
            accepted = [label for label, score in zip(labels, prediction["types"])
                        if score >= selected["selected"]["policy"]["threshold"]
                        and (task == "cg" or prediction["exists"] >= selected["selected"]["policy"]["threshold"])]
            output.append({"pmid": row["pmid"], "key": row["key"], "task": task,
                           "labels": accepted, "priority": prediction, "entity_condition": "provided_gold_arguments"})
    result = {"predictions": output, "input_sha256": filehash(path), "freeze_sha256": filehash(home()/"freeze.json"),
              "complete_before_scoring": len(output) == len(rows), "entity_condition": "provided_gold_arguments"}
    atomic(home()/"predictions.json", result, True)
    atomic(home()/"prediction-receipt.json", {"predictions_sha256": filehash(home()/"predictions.json"),
           "input_sha256": result["input_sha256"], "freeze_sha256": result["freeze_sha256"]}, True)
    return {"pairs": len(output), "labels_read": False}


def evaluate():
    verify_freeze()
    receipt = read(home()/"prediction-receipt.json")
    if receipt["predictions_sha256"] != filehash(home()/"predictions.json"):
        raise ValueError("native 저장 예측 해시 변경")
    prediction = read(home()/"predictions.json")
    inputs = read(home()/"data/evaluation-inputs.json")
    if not prediction["complete_before_scoring"] or len(prediction["predictions"]) != len(inputs["pairs"]):
        raise ValueError("전체 native 예측 전 정답 읽기 금지")
    predicted_keys = [(r["task"], r["pmid"], r["key"]) for r in prediction["predictions"]]
    input_keys = [(r["task"], r["pmid"], r["key"]) for r in inputs["pairs"]]
    if len(set(predicted_keys)) != len(predicted_keys) or set(predicted_keys) != set(input_keys):
        raise ValueError("native 예측 누락·중복")
    if prediction["input_sha256"] != filehash(home()/"data/evaluation-inputs.json") or prediction["freeze_sha256"] != filehash(home()/"freeze.json"):
        raise ValueError("native 예측·입력·동결 버전 불일치")
    gold = read(home()/"data/evaluation-gold.json")
    scores = {}
    for task in ("drugprot", "cg"):
        expected = {(r["pmid"], r["key"], label) for r in gold["pairs"] if r["task"] == task for label in r["labels"] if not label.endswith(":NONE")}
        observed = {(r["pmid"], r["key"], label) for r in prediction["predictions"] if r["task"] == task for label in r["labels"]}
        scores[task] = metric(expected, observed)
    result = {"scores": scores, "entity_condition": "provided_gold_arguments", "automatic_entity_condition": "not_measured",
              "gold_sha256": filehash(home()/"data/evaluation-gold.json"), "full_event_extraction": False,
              "official_full_task_score": False, "scope": "native labeled candidate-pair classification",
              "core_model_transfer": False}
    atomic(home()/"evaluation.json", result, True)
    return result
