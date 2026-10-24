"""Finite training grid; every selected policy is learned without development labels."""
from __future__ import annotations

import copy
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

from .common import (POLICY, TYPES, atomic, canonical, canonical_digest, checkpoint, digest, exact,
                     filehash, groups, home, metric, read, sources)
from .data import documents
from .learning import (apply_id, fit_linear, fit_neural, id_policy, linear_scores,
                       neural_scores)


def example_rows(split):
    return read(home()/"examples"/("id-"+split+".json"))["rows"]


def model_key(task, rows, valid, config, seed):
    return canonical_digest([task, rows, valid, config, seed, sources()])[:24]


def fit(task, rows, valid, config, seed=20261023):
    directory = home()/"fits"/task/model_key(task, rows, valid, config, seed)
    if config.get("family") == "linear":
        return fit_linear(rows, directory, config["C"])
    return fit_neural(task, rows, valid, directory, config, seed)


def score(task, rows, directory):
    directory = Path(directory)
    weights = directory/("model.json" if (directory/"model.json").exists() else "model.safetensors")
    key = canonical_digest([task, filehash(weights), rows, sources()])
    path = home()/"scores"/(key+".json")
    if not path.exists():
        values = linear_scores(rows, directory) if weights.name == "model.json" else neural_scores(task, rows, directory)
        atomic(path, {"scores": values, "weights_sha256": filehash(weights), "row_sha256": canonical_digest(rows)}, True)
    return read(path)["scores"]


def configurations(task):
    neural = [{"family": "crossencoder" if task == "id" else "event-bag", "learning_rate": lr,
               "epochs": 5, "patience": 2, "accumulation": 16} for lr in POLICY["learning_rates"]]
    return ([{"family": "linear", "C": c} for c in POLICY["C"]] if task == "id" else []) + neural


def identity_policies(rows, scores):
    result = {}
    for kind in ("gene", "chemical"):
        indices = [i for i, r in enumerate(rows) if r["mention"]["kind"] == kind]
        if not indices:
            result[kind] = {"enabled": False, "threshold": .99, "margin": .2, "correct": 0,
                            "changed": 0, "reason": "no_annotated_candidates"}
        else:
            subrows, subscores = [rows[i] for i in indices], [scores[i] for i in indices]
            result[kind] = {action: id_policy(subrows, subscores, action) for action in ("link", "withdraw")}
    return result


def identity_quality(rows, scores, policies, gold_docs=None):
    total, correct, changed, changed_correct = 0, 0, 0, 0
    diagnostics = Counter()
    observed = set()
    for row, output in zip(rows, scores):
        d = apply_id(row, output, policies[row["mention"]["kind"]])
        truth = set(row.get("gold_ids", []))
        if row["valid_weight"]:
            total += 1
            correct += int(d["selected_id"] in truth or (d["selected_id"] is None and not row["valid_span"]))
            if d["reason"] in ("new", "changed", "withdrawn"):
                changed += 1
                changed_correct += int(d["selected_id"] in truth or (d["selected_id"] is None and not row["valid_span"]))
            diagnostics[d["reason"]] += 1
            diagnostics["retrieval_miss"] += bool(row["retrieval_miss"])
            if d["selected_id"]:
                m = row["mention"]
                observed.add((row["pmid"], m["kind"], m["start"], m["end"], d["selected_id"]))
    strict = None
    if gold_docs is not None:
        expected = {(str(doc["pmid"]), e["kind"].replace("component", "chemical"), e["start"], e["end"], str(identifier))
                    for doc in gold_docs for e in doc.get("entities", []) for identifier in e.get("native_ids", []) if identifier}
        strict = metric(expected, observed)
    return {"total": total, "correct": correct, "accuracy": correct/total if total else 0.,
            "changed": changed, "changed_correct": changed_correct, "states": dict(diagnostics),
            "strict_span_ID": strict}


def sample_training(rows):
    """All positives retained; negative selection is source keyed, not dev-error keyed."""
    positive = [r for r in rows if r["exists"]]
    negative = defaultdict(list)
    for row in rows:
        if not row["exists"]:
            negative[row["pmid"]].append(row)
    for _, group in sorted(negative.items()):
        hard = sorted(group, key=lambda r: (-sum(v["same_sentence"] for v in r["views"]), r["pair_key"]))[:32]
        used = {r["pair_key"] for r in hard}
        randoms = sorted([r for r in group if r["pair_key"] not in used], key=lambda r: digest("20261023"+r["pair_key"]))[:32]
        positive.extend(hard+randoms)
    return positive


def inner_select(task, rows, docs, salt):
    fold = groups(docs, 3, salt)
    candidates = []
    for config in configurations(task):
        outputs = [] if task == "id" else [None]*len(rows)
        evaluation_rows = [] if task == "id" else rows
        histories = []
        for k in range(3):
            checkpoint()
            if task == "id":
                from .ner import crossfit, infer
                training_docs = [d for d in docs if fold[str(d["pmid"])] != k]
                held_docs = [d for d in docs if fold[str(d["pmid"])] == k]
                train, valid = crossfit(training_docs), infer(training_docs, held_docs)
            else:
                train = [r for r in rows if fold[r["pmid"]] != k]
                valid = [r for r in rows if fold[r["pmid"]] == k]
            if not train or not valid:
                raise ValueError("내부 교차검증 후보 행 부족")
            path = fit(task, sample_training(train) if task == "relation" else train, valid, config)
            prediction = score(task, valid, path)
            if task == "id":
                outputs.extend(prediction)
                evaluation_rows.extend(valid)
            else:
                at = 0
                for i, row in enumerate(rows):
                    if fold[row["pmid"]] == k:
                        outputs[i] = prediction[at]
                        at += 1
            if (path/"complete.json").exists():
                histories.append(read(path/"complete.json")["history"])
        if any(v is None for v in outputs):
            raise ValueError("내부 OOF 예측 누락")
        if task == "id":
            policies = identity_policies(evaluation_rows, outputs)
            quality = identity_quality(evaluation_rows, outputs, policies, docs)
            value = {"config": config, "policies": policies, "quality": quality, "scores": outputs}
            value["rank"] = [int(any(p.get("enabled", False) or any(v.get("enabled", False) for v in p.values() if isinstance(v, dict)) for p in policies.values())),
                             quality["strict_span_ID"]["f1"], quality["strict_span_ID"]["recall"],
                             quality["changed_correct"], -quality["changed"], -float(config.get("C", 0))]
        else:
            from .relations import select_policy
            policy = select_policy(rows, outputs, docs)
            value = {"config": config, "policy": policy, "scores": outputs}
            # If no candidate passes precision, respect the recall floor first.
            value["rank"] = [int(policy["passed"]), int(policy["recall"] >= policy["recall_floor"]),
                             policy["recall"] if policy["passed"] else policy["precision"] if policy["recall"] >= policy["recall_floor"] else policy["recall"],
                             policy["f1"], -config["learning_rate"]]
        best_epochs = [min(h, key=lambda r: r["validation_loss"])["epoch"] for h in histories if h]
        value["final_epochs"] = int(statistics.median(best_epochs)) if best_epochs else 5
        candidates.append(value)
    winner = max(candidates, key=lambda v: tuple(v["rank"]))
    return winner, [{k: v for k, v in c.items() if k != "scores"} for c in candidates]


def train(task):
    if task == "id":
        # Outer/inner partitions regenerate their own OOF upstream mentions.
        # The full 96k-row profile is only needed for final selection/training;
        # retaining it during every nested fold adds no supervision and can OOM.
        rows = None
        docs = documents("identity-train")
    else:
        from .relations import build_rows
        if not (home()/"selected/id.json").exists():
            raise FileNotFoundError("관계 학습 전 외부 415편 ID 모델 선택 필요")
        decisions = identity_decisions("train")
        rows = build_rows("train", decisions)
        docs = documents("relations-train")
        atomic(home()/"examples/relation-train.json", {"rows": rows, "span_labels_independent_of_ID": True}, True)
    outer = groups(docs, 5, "20261023-outer-"+task)
    summaries, oof = [], []
    for k in range(5):
        checkpoint()
        train_docs = [d for d in docs if outer[str(d["pmid"])] != k]
        valid_docs = [d for d in docs if outer[str(d["pmid"])] == k]
        if task == "id":
            from .ner import crossfit, infer
            train_rows, valid_rows = crossfit(train_docs), infer(train_docs, valid_docs)
        else:
            train_rows = [r for r in rows if outer[r["pmid"]] != k]
            valid_rows = [r for r in rows if outer[r["pmid"]] == k]
        selected, candidates = inner_select(task, train_rows, train_docs, f"20261023-{task}-outer{k}")
        config = {**selected["config"], "epochs": selected["final_epochs"]}
        path = fit(task, sample_training(train_rows) if task == "relation" else train_rows, [], config)
        predictions = score(task, valid_rows, path)
        oof.extend({"row": r, "score": s, "outer_fold": k} for r, s in zip(valid_rows, predictions))
        if task == "id":
            quality = identity_quality(valid_rows, predictions, selected["policies"], valid_docs)
        else:
            from .relations import accepted, expected
            quality = metric(expected(valid_docs), accepted(valid_rows, predictions, selected["policy"]["threshold"]))
        summary = {"outer_fold": k, "config": config, "quality": quality, "inner_candidates": candidates,
                   "train_pmids": sorted(str(d["pmid"]) for d in train_docs),
                   "validation_pmids": sorted(str(d["pmid"]) for d in valid_docs)}
        atomic(home()/"cross-validation"/task/(f"outer-{k}.json"), summary, True)
        summaries.append(summary)
        del train_rows, valid_rows, predictions, selected, candidates
    if task == "id":
        rows = example_rows("normalization")
    selected, candidates = inner_select(task, rows, docs, "20261023-final-"+task)
    config = {**selected["config"], "epochs": selected["final_epochs"]}
    # Recalibrate the exact three-seed averaging rule on held training papers.
    # A threshold chosen for a single model must not silently serve an ensemble.
    calibration_rows, calibration_scores = [], []
    calibration_folds = groups(docs, 3, "20261023-ensemble-calibration-"+task)
    for k in range(3):
        train_docs = [d for d in docs if calibration_folds[str(d["pmid"])] != k]
        valid_docs = [d for d in docs if calibration_folds[str(d["pmid"])] == k]
        if task == "id":
            from .ner import crossfit, infer
            training_rows, validation_rows = crossfit(train_docs), infer(train_docs, valid_docs)
        else:
            training_rows = [r for r in rows if calibration_folds[r["pmid"]] != k]
            validation_rows = [r for r in rows if calibration_folds[r["pmid"]] == k]
        paths = []
        for seed in POLICY["seeds"]:
            fitted = fit(task, sample_training(training_rows) if task == "relation" else training_rows, [], config, seed)
            relative = str(fitted.relative_to(home())).replace("\\", "/")
            if relative not in paths:
                paths.append(relative)
            if config.get("family") == "linear":
                break
        calibration_rows.extend(validation_rows)
        calibration_scores.extend(ensemble_scores(task, validation_rows, {"models": paths}))
    if task == "id":
        selected["policies"] = identity_policies(calibration_rows, calibration_scores)
    else:
        from .relations import select_policy
        selected["policy"] = select_policy(calibration_rows, calibration_scores, docs)
    final_models = []
    for seed in POLICY["seeds"]:
        path = fit(task, sample_training(rows) if task == "relation" else rows, [], config, seed)
        # The linear baseline is deterministic and should not masquerade as 3 models.
        relative = str(path.relative_to(home())).replace("\\", "/")
        if relative not in final_models:
            final_models.append(relative)
        if config.get("family") == "linear":
            break
    result = {"task": task, "config": config, "models": final_models, "selection_candidates": candidates,
              "outer_folds": summaries, "training_only_selection": True, "development_labels_read": False,
              "evaluation_scope": "v8_owned_nested_upstream_NER" if task == "id" else "relation conditional on external-only v8 NER and ID",
              "upstream_ner_crossfit_matches_v8_groups": True,
              "independent_full_pipeline_performance": False,
              "row_sha256": canonical_digest(rows), "source_sha256": sources()}
    result["ensemble_calibration"] = {"folds": 3, "held_training_only": True,
                                      "seeds": POLICY["seeds"], "papers": len(docs),
                                      "threshold_distribution_matches_inference": True}
    result["policies" if task == "id" else "policy"] = selected["policies" if task == "id" else "policy"]
    atomic(home()/"selected"/(task+".json"), result, True)
    atomic(home()/"cross-validation"/task/"oof.json", {"scores": oof, "row_sha256": canonical_digest(rows)}, True)
    return result


def ensemble_scores(task, rows, selected):
    import numpy as np
    outputs = [score(task, rows, home()/path) for path in selected["models"]]
    result = []
    for i in range(len(rows)):
        if task == "id":
            result.append({"probabilities": np.mean([v[i]["probabilities"] for v in outputs], axis=0).tolist(),
                           "validity": float(np.mean([v[i]["validity"] for v in outputs]))})
        else:
            result.append({key: (np.mean([v[i][key] for v in outputs], axis=0).tolist())
                           for key in ("exists", "types", "direction", "evidence")})
    return result


def identity_decisions(split):
    from .ner import infer
    name = "relations-train" if split == "train" else "development-sources"
    rows = infer(documents("identity-train"), documents(name))
    path = home()/"examples"/("id-"+split+".json")
    atomic(path, {"rows": rows, "gold_injected": False, "upstream_held_group_excluded": True}, True)
    # Keep source-only mentions for relation construction, independently from chosen IDs.
    by_paper = defaultdict(list)
    for r in rows:
        by_paper[r["pmid"]].append(r["mention"])
    for d in documents(name):
        atomic(home()/"v8-prepared"/split/("paper-"+str(d["pmid"])+".json"),
               {"mentions": by_paper[str(d["pmid"])], "source_sha256": digest(d["text"]), "source_only_mentions": True}, True)
    selected = read(home()/"selected/id.json")
    outputs = ensemble_scores("id", rows, selected)
    result = {}
    serial = []
    for row, output in zip(rows, outputs):
        m = row["mention"]
        key = (row["pmid"], m["kind"], m["start"], m["end"])
        value = apply_id(row, output, selected["policies"][m["kind"]])
        if key in result:
            raise ValueError("ID 후보 언급 중복")
        result[key] = value
        serial.append({"pmid": row["pmid"], "kind": m["kind"], "start": m["start"], "end": m["end"], **value})
    atomic(home()/"normalized"/(split+".json"), {"decisions": serial,
           "selection_sha256": filehash(home()/"selected/id.json")}, True)
    return result


def freeze():
    selected = {task: read(home()/"selected"/(task+".json")) for task in ("id", "relation")}
    files = [home()/"policy.json", home()/"models.json", home()/"inputs/import.json",
             home()/"examples/manifest.json", home()/"retrieval/manifest.json", home()/"ner/manifest.json",
             home()/"dictionary-assets.json"]
    for task, value in selected.items():
        files.append(home()/"selected"/(task+".json"))
        for model in value["models"]:
            for name in ("model.json", "model.safetensors", "complete.json"):
                if (home()/model/name).exists():
                    files.append(home()/model/name)
    profiles = list((home()/"ner/profiles").glob("*.json"))
    if not profiles:
        raise ValueError("새 v8 상류 NER crossfit 프로필 없음")
    for path in profiles:
        p = read(path)
        if not p.get("held_group_excluded") or set(p["training_pmids"]) & set(p["predicted_pmids"]):
            raise ValueError("상류 NER crossfit 학습·예측 그룹 충돌")
    # Store a compact manifest; profiles/models are independently hash checked.
    upstream_files = [*profiles, *(home()/"ner/models").glob("*/training-provenance.json"),
                      *(home()/"ner/models").glob("*/model.safetensors")]
    atomic(home()/"ner/freeze-manifest.json", {"files": {str(p.relative_to(home())).replace("\\", "/"): filehash(p)
           for p in upstream_files}, "full_held_groups_excluded": True}, True)
    files.append(home()/"ner/freeze-manifest.json")
    result = {"engine": "nutriomics-evidence-v8", "code": sources(),
              "files": {str(p.relative_to(home())).replace("\\", "/"): filehash(p) for p in files},
              "selection_uses_development": False, "additional_model_calls": 0,
              "training_goal_passed": selected["relation"]["policy"]["passed"],
              "sealed_evaluation_allowed": False,
              "full_pipeline_crossfit_audit_complete": True,
              "limitation": "Full public-corpus duplicate/provenance audit and all task gates must pass before any protected final evaluation",
              "clinical_and_dapt_are_separate_experiments": True}
    atomic(home()/"freeze.json", result, True)
    return result


def verify_freeze():
    frozen = read(home()/"freeze.json")
    if frozen["code"] != sources():
        raise ValueError("동결 후 v8 코드 변경: 새 실험 버전 필요")
    for relative, expected in frozen["files"].items():
        if filehash(home()/relative) != expected:
            raise ValueError("동결 산출물 변경: " + relative)
    for manifest, prefix in (("inputs/import.json", ""), ("examples/manifest.json", "examples"),
                             ("retrieval/manifest.json", ""), ("ner/freeze-manifest.json", "")):
        for relative, expected in read(home()/manifest)["files"].items():
            if filehash(home()/prefix/relative) != expected:
                raise ValueError("동결된 하위 입력 변경: " + relative)
    return frozen


def predict():
    verify_freeze()
    from .relations import build_rows, paper_prediction
    docs = documents("development-sources")
    if len(docs) != 100:
        raise ValueError("100편 고정 코호트 필요")
    # Neither this function nor build_rows imports any gold-development loader.
    rows = build_rows("development", identity_decisions("development"))
    selected = read(home()/"selected/relation.json")
    outputs = ensemble_scores("relation", rows, selected)
    files = []
    for d in docs:
        checkpoint()
        prediction = paper_prediction(d, rows, outputs, selected["policy"]["threshold"])
        path = home()/"predictions/final"/("paper-"+str(d["pmid"])+".json")
        atomic(path, prediction, True)
        files.append({"path": str(path.relative_to(home())).replace("\\", "/"), "sha256": filehash(path)})
    result = {"papers": 100, "files": files, "freeze_sha256": filehash(home()/"freeze.json"),
              "normalization_sha256": filehash(home()/"normalized/development.json"),
              "complete_before_scoring": True, "development_labels_read": False}
    atomic(home()/"predictions/manifest.json", result, True)
    return result


def export_verified():
    """Run on the training host after full weight/input verification, then export."""
    frozen = verify_freeze()
    manifest = read(home()/"predictions/manifest.json")
    if manifest.get("complete_before_scoring") is not True or manifest.get("papers") != 100 or len(manifest["files"]) != 100:
        raise ValueError("100편 전체 예측 저장 전 검증 export 금지")
    if manifest["freeze_sha256"] != filehash(home()/"freeze.json"):
        raise ValueError("예측·동결 불일치")
    files = ["freeze.json", "predictions/manifest.json", "normalized/development.json", "data/development-sources.json"]
    files += [entry["path"] for entry in manifest["files"]]
    for entry in manifest["files"]:
        if filehash(home()/entry["path"]) != entry["sha256"]:
            raise ValueError("export 예측 해시 불일치")
    if manifest["normalization_sha256"] != filehash(home()/"normalized/development.json"):
        raise ValueError("export ID결정 해시 불일치")
    result = {"format": "v8-source-only-verified-export/1", "full_remote_freeze_verified": True,
              "freeze_sha256": filehash(home()/"freeze.json"), "code": sources(),
              "verified_frozen_files": frozen["files"],
              "verified_upstream_files": read(home()/"ner/freeze-manifest.json")["files"],
              "export_files": {relative: filehash(home()/relative) for relative in files},
              "development_labels_included": False, "model_weights_included": False,
              "transport_requirement": "authenticated SSH plus file hashes; preserve this receipt immutably"}
    atomic(home()/"export-verification.json", result, True)
    return result


def verify_saved_export():
    receipt = read(home()/"export-verification.json")
    frozen = read(home()/"freeze.json")
    if (receipt.get("format") != "v8-source-only-verified-export/1"
            or receipt.get("full_remote_freeze_verified") is not True
            or receipt.get("development_labels_included") is not False
            or receipt.get("model_weights_included") is not False):
        raise ValueError("명시적 원격 전체 검증 receipt 필요")
    if receipt["code"] != sources() or frozen["code"] != sources():
        raise ValueError("원격·로컬 평가 코드 불일치")
    if receipt["freeze_sha256"] != filehash(home()/"freeze.json") or receipt["verified_frozen_files"] != frozen["files"]:
        raise ValueError("원격 전체 검증과 로컬 동결 산출물 계약 불일치")
    if not receipt.get("verified_upstream_files"):
        raise ValueError("상류 NER 전체 검증 파일 해시 없음")
    for relative, expected in receipt["export_files"].items():
        p = (home()/relative).resolve()
        if not p.is_relative_to(home().resolve()) or filehash(p) != expected:
            raise ValueError("회수된 source-only 산출물 해시 불일치")
    return receipt


def evaluate_saved():
    """Explicit, receipt-verified local scoring without downloading every weight."""
    return evaluate(verified_export=True)


def evaluate(verified_export=False):
    if verified_export:
        verify_saved_export()
    else:
        verify_freeze()
    manifest = read(home()/"predictions/manifest.json")
    docs = documents("development-sources")
    expected_paths = {"predictions/final/paper-"+str(d["pmid"])+".json" for d in docs}
    if not manifest["complete_before_scoring"] or manifest["papers"] != 100 or len(manifest["files"]) != 100:
        raise ValueError("100편 전체 예측 저장 전 정답 읽기 금지")
    if {p["path"] for p in manifest["files"]} != expected_paths:
        raise ValueError("예측 누락/중복")
    if manifest["freeze_sha256"] != filehash(home()/"freeze.json"):
        raise ValueError("예측 동결 해시 불일치")
    if manifest["normalization_sha256"] != filehash(home()/"normalized/development.json"):
        raise ValueError("예측 후 ID결정 변경")
    for entry in manifest["files"]:
        if filehash(home()/entry["path"]) != entry["sha256"]:
            raise ValueError("저장 예측 변경")
    # Local-only evaluation. Remote source-only training hosts do not receive labels.
    from ..repair_v3.runner import gold
    from ..repair_v3.common import ROOT, native_metrics
    from .. import benchmarks, evaluation_v6
    from ..chatgpt_evaluation import _bootstrap
    reference, gold_sha = gold()
    if {str(d["pmid"]) for d in reference} != {str(d["pmid"]) for d in docs}:
        raise ValueError("평가 코호트 변경")
    by_pmid = {str(d["pmid"]): d for d in docs}
    for d in reference:
        if digest(d["text"]) != digest(by_pmid[str(d["pmid"])] ["text"]):
            raise ValueError("평가 원문 변경")
    arms = {"v2": "runtime/chatgpt-repair-v2/predictions/combined",
            "v3": "runtime/chatgpt-repair-v3/predictions/combined",
            "v4": "runtime/chatgpt-repair-v4/predictions/combined",
            "v5_r1": "runtime/chatgpt-repair-v5-r1/predictions/combined",
            "identity_baseline": "runtime/chatgpt-repair-v5-r1/predictions/identity_baseline"}
    predictions = {name: {str(d["pmid"]): read(ROOT/path/("paper-"+str(d["pmid"])+".json"))
                         for d in reference} for name, path in arms.items()}
    predictions["nutriomics-evidence-v8"] = {str(d["pmid"]): read(home()/"predictions/final"/("paper-"+str(d["pmid"])+".json")) for d in reference}
    decisions = {(str(r["pmid"]), r["kind"], r["start"], r["end"]): r
                 for r in read(home()/"normalized/development.json")["decisions"]}
    only_id, only_relation = {}, {}
    for d in reference:
        pmid = str(d["pmid"])
        prediction = predictions["nutriomics-evidence-v8"][pmid]
        if prediction["source_sha256"] != digest(d["text"]):
            raise ValueError("예측·원문 불일치")
        for r in prediction["relations"]:
            if not all(exact(d["text"], s) for s in r["evidence"]+r["endpoints"]):
                raise ValueError("수락 인용 원문 위치 오류")
        base = copy.deepcopy(predictions["identity_baseline"][pmid])
        old_ids = defaultdict(set)
        for r in base["relations"]:
            for i, endpoint in enumerate(r.get("endpoints", [])):
                if i >= 2:
                    raise ValueError("기준선 관계 참여자 개수 오류")
                key = (pmid, endpoint["kind"].replace("component", "chemical"), endpoint["start"], endpoint["end"])
                field = "entity1" if i == 0 else "entity2"
                old_ids[key].add(r[field])
                selected_id = decisions.get(key, {}).get("selected_id", r[field])
                if r.get("subject") == r[field]:
                    r["subject"] = selected_id
                r[field] = selected_id
        base["relations"] = [r for r in base["relations"] if r["entity1"] and r["entity2"]]
        only_id[pmid] = base
        rel = copy.deepcopy(prediction)
        for r in rel["relations"]:
            for i, endpoint in enumerate(r["endpoints"]):
                key = (pmid, endpoint["kind"].replace("component", "chemical"), endpoint["start"], endpoint["end"])
                values = old_ids.get(key, set())
                old_id = next(iter(values)) if len(values) == 1 else endpoint.get("previous_id")
                field = "entity1" if i == 0 else "entity2"
                if r.get("subject") == r[field]:
                    r["subject"] = old_id
                r[field] = old_id
        rel["relations"] = [r for r in rel["relations"] if r["entity1"] and r["entity2"]]
        only_relation[pmid] = rel
    predictions["v8_identity_only"], predictions["v8_relation_existing_ID_only"] = only_id, only_relation
    scores = {name: native_metrics(reference, prediction) for name, prediction in predictions.items()}
    if tuple(scores["identity_baseline"]["metrics"][k] for k in ("tp", "fp", "fn")) != (65, 63, 89):
        raise ValueError("기준선 65/63/89 재현 실패")
    transitions = {}
    for name, prediction in predictions.items():
        counts = Counter()
        for d in reference:
            gold_set, old, _, _ = evaluation_v6.relation_sets(d, predictions["identity_baseline"][str(d["pmid"])])
            _, new, _, _ = evaluation_v6.relation_sets(d, prediction[str(d["pmid"])])
            old_tp, new_tp = old & gold_set, new & gold_set
            counts.update({"retained": len(old_tp & new_tp), "lost": len(old_tp-new_tp), "new": len(new_tp-old_tp)})
        transitions[name] = dict(counts)
    delta = {name: _bootstrap({p: v["relation"] for p, v in scores["identity_baseline"]["per_paper"].items()},
                             {p: v["relation"] for p, v in score["per_paper"].items()}, 20261023)
             for name, score in scores.items()}
    m = scores["nutriomics-evidence-v8"]["metrics"]
    result = {"engine": "nutriomics-evidence-v8", "papers": 100, "gold_relations": 154, "gold_sha256": gold_sha,
              "scores": scores, "transitions": transitions, "paired_bootstrap_2000_delta_95": delta,
              "evaluator_sha256": benchmarks.evaluator_source_hashes(),
              "passed": m["tp"] >= 65 and m["precision"] >= .85 and m["recall"] >= 65/154,
              "independent_final_performance": False, "nutrition_accuracy": "unmeasured",
              "verification_mode": "authenticated_source_only_export" if verified_export else "full_local_weights",
              "sealed_test_opened": False, "default_engine": "heuristic-v1"}
    atomic(home()/"evaluation.json", result, True)
    return result


def execute(stage):
    from .data import audit, prepare
    if stage == "audit":
        return audit()
    if stage == "prepare":
        return prepare()
    if stage == "train-ner":
        from .ner import train_ner
        return train_ner()
    if stage in ("train-id", "train-relations"):
        return train("id" if stage == "train-id" else "relation")
    if stage == "freeze":
        return freeze()
    if stage == "predict":
        return predict()
    if stage == "evaluate":
        return evaluate()
    if stage == "evaluate-saved":
        return evaluate_saved()
    if stage == "verify-export":
        return export_verified()
    if stage in ("clinical", "clinical-role", "dapt"):
        from .auxiliary import clinical, clinical_role, dapt
        return {"clinical": clinical, "clinical-role": clinical_role, "dapt": dapt}[stage]()
    if stage == "resume":
        for name, receipt in (("audit", "audit.json"), ("prepare", "prepared-v8.json"), ("train-ner", "ner/manifest.json"),
                              ("train-id", "selected/id.json"), ("train-relations", "selected/relation.json"),
                              ("freeze", "freeze.json"), ("predict", "predictions/manifest.json")):
            if not (home()/receipt).exists():
                return execute(name)
        return {"status": "predictions_ready", "next": "local-only evaluate", "gold_read": False}
    raise ValueError(stage)
