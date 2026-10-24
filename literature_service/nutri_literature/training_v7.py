"""Nested paper-separated fitting, whole-corpus strict-native denominators."""
import json
from pathlib import Path
import time
from .contract_v7 import C_GRID, THRESHOLDS, POLICY, canonical, counts, immutable, passes, sha, sources
from .learning_v7 import SeparatedHead, selected
from .splits_v7 import grouped_folds
from .supervision_v7 import bag_targets, gold_keys, predicted_keys


def prepare_cached(data_path, audit_path, cache, output):
    raw_data = Path(data_path).read_bytes()
    data, audit = json.loads(raw_data), json.loads(Path(audit_path).read_bytes())
    if (data.get('split') != 'train' or audit.get('passed') is not True
            or audit.get('protected_overlap') != 0 or audit['audited_training_sha256'] != sha(raw_data)
            or {str(d['pmid']) for d in data['documents']} != set(audit['training_pmids'])):
        raise ValueError('Exact audited official training cohort required')
    cache, output = Path(cache), Path(output)
    manifest = json.loads((cache / "manifest.json").read_bytes())
    if manifest["identity"]["training_sha256"] != sha(Path(data_path).read_bytes()):
        raise ValueError("Automatic source cohort differs")
    documents = {str(d["pmid"]): d for d in data["documents"]}
    files, totals = [], {"semantic_positive_id_negative": 0, "masked_bags": 0, "bags": 0}
    for member in manifest["files"]:
        raw = (cache / member["path"]).read_bytes()
        if sha(raw) != member["sha256"]:
            raise ValueError("Automatic cache changed")
        record = json.loads(raw)
        doc = dict(documents[record["pmid"]])
        if doc.get("relation_annotation_complete") is False:
            raise ValueError("Normalization-only corpus entered complete relation cache")
        # V6 official BioREDirect adapter omitted True, but explicitly marks
        # normalization-only documents False. Require the exact audited cache.
        doc["relation_annotation_complete"] = True
        if record["source_hash"] != sha(doc["text"]):
            raise ValueError("Training source differs")
        for bag in record["bags"]:
            bag["targets"] = bag_targets(doc, bag["endpoints"])
            totals["bags"] += 1
            totals["masked_bags"] += not bag["targets"]["semantic_supervised"]
            totals["semantic_positive_id_negative"] += bool(bag["targets"]["semantic_labels"] and not bag["targets"]["strict_native_labels"])
        payload = {"pmid": doc["pmid"], "text": doc["text"], "sections": doc["sections"],
                   "gold_keys": sorted(gold_keys(doc)), "bags": record["bags"],
                   "entities": record["entities"], "source_hash": record["source_hash"]}
        target = immutable(output / member["path"], payload)
        files.append({"path": target.name, "sha256": sha(target.read_bytes())})
    result = {"schema": "separated-supervision-v7-1", "split": "train", "files": files,
              "counts": totals, "legacy_manifest_sha256": sha((cache / "manifest.json").read_bytes()),
              "audit_sha256": sha(Path(audit_path).read_bytes()), "policy": POLICY,
              "source_sha256": sha(Path(__file__).read_bytes()), "supervision_sha256": sha(Path(__file__).with_name('supervision_v7.py').read_bytes())}
    immutable(output / "manifest.json", result)
    return result


def load_features(path):
    path = Path(path)
    manifest = json.loads((path / "manifest.json").read_bytes())
    if manifest["split"] != "train":
        raise ValueError("Only official training features may fit models")
    documents, records = [], []
    for member in manifest["files"]:
        raw = (path / member["path"]).read_bytes()
        if sha(raw) != member["sha256"]:
            raise ValueError("Feature hash changed")
        d = json.loads(raw)
        documents.append(d)
        records.extend({**b, "pmid": d["pmid"]} for b in d["bags"])
    expected = {(p, tuple(ids), t) for d in documents for p, ids, t in d["gold_keys"]}
    return documents, records, expected


def score_predictions(records, scores, threshold, expected):
    predicted = set()
    for r, s in zip(records, scores):
        predicted.update(predicted_keys(r["pmid"], r["endpoints"], selected(s, threshold)))
    return counts(expected, predicted)


def choose(configurations):
    eligible = [r for r in configurations if passes(r)]
    chosen = max(eligible or configurations, key=lambda x: (x["recall"] if eligible else x["f1"], x["f1"], x["precision"], -x["C"], x["threshold"]))
    return {**chosen, "targets_met": bool(eligible)}


def fit_cached(path, output):
    import joblib
    documents, records, expected = load_features(path)
    output = Path(output)
    code = Path(__file__).parent
    names = ('contract_v7.py', 'supervision_v7.py', 'splits_v7.py', 'learning_v7.py', 'training_v7.py')
    identity = {"features": sha((Path(path) / "manifest.json").read_bytes()),
                "sources": {n: sha((code / n).read_bytes()) for n in names}, "policy": POLICY}
    immutable(output / "training-contract.json", identity)
    assignment = grouped_folds(documents)
    outer_predictions, selections = {}, []
    for outer in range(5):
        outer_train = [d for d in documents if assignment[d["pmid"]] != outer]
        inner = grouped_folds(outer_train, 3, "inner:" + str(outer))
        configurations = []
        fit_records = [r for r in records if r["pmid"] in inner]
        inner_expected = {key for key in expected if key[0] in inner}
        for c in C_GRID:
            held_scores = {}
            for fold in range(3):
                model_path = output / f"outer-{outer}-C-{c}-inner-{fold}.joblib"
                train = [r for r in fit_records if inner[r["pmid"]] != fold]
                held = [(i, r) for i, r in enumerate(fit_records) if inner[r["pmid"]] == fold]
                if model_path.exists():
                    model = joblib.load(model_path)
                else:
                    model = SeparatedHead().fit(train, c)
                    joblib.dump(model, model_path)
                held_scores.update(zip((i for i, _ in held), model.predict([r for _, r in held])))
                print(canonical({"stage": "nested-fit", "outer": outer, "inner": fold, "C": c}), flush=True)
            values = [held_scores[i] for i in range(len(fit_records))]
            configurations.extend({"C": c, "threshold": t, **score_predictions(fit_records, values, t, inner_expected)} for t in THRESHOLDS)
        selection = choose(configurations)
        train = [r for r in records if assignment[r["pmid"]] != outer]
        held = [(i, r) for i, r in enumerate(records) if assignment[r["pmid"]] == outer]
        model = SeparatedHead().fit(train, selection["C"])
        joblib.dump(model, output / f"outer-{outer}.joblib")
        scores = model.predict([r for _, r in held])
        for (i, _), row in zip(held, scores):
            outer_predictions[i] = {"scores": row, "threshold": selection["threshold"]}
        immutable(output / f"outer-{outer}-selection.json", {"selection": selection, "configurations": configurations,
                  "train_pmids": sorted(inner), "held_pmids": sorted({r["pmid"] for _, r in held})})
        selections.append(selection)
    predicted = set()
    for i, r in enumerate(records):
        v = outer_predictions[i]
        predicted.update(predicted_keys(r["pmid"], r["endpoints"], selected(v["scores"], v["threshold"])))
    # Final C/threshold are selected only from training cross-fit scores.
    configurations = []
    for c in C_GRID:
        scores = {}
        for fold in range(5):
            train = [r for r in records if assignment[r["pmid"]] != fold]
            held = [(i, r) for i, r in enumerate(records) if assignment[r["pmid"]] == fold]
            model = SeparatedHead().fit(train, c)
            scores.update(zip((i for i, _ in held), model.predict([r for _, r in held])))
        configurations.extend({"C": c, "threshold": t, **score_predictions(records, [scores[i] for i in range(len(records))], t, expected)} for t in THRESHOLDS)
    selection = choose(configurations)
    final = SeparatedHead().fit(records, selection["C"])
    joblib.dump(final, output / "relations.joblib")
    report = {"schema": "nested-separated-v7-1", "selection": selection, "outer_metrics": counts(expected, predicted),
              "outer_selections": selections, "configurations": configurations, "fold_assignments": assignment,
              "training_pmids": sorted(assignment), "full_gold_relations": len(expected), "bags": len(records),
              "clinical_unchanged": True, "upstream_models_fixed": True, "whole_pipeline_independence": False,
              "nutrition_accuracy": "unmeasured", "sealed_test_opened": False,
              "model_sha256": sha((output / "relations.joblib").read_bytes()), **identity}
    immutable(output / "manifest.json", report)
    return report
