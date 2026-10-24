"""Paper-separated training, fixed grids, no test labels in model selection."""

from collections import defaultdict
import json
from pathlib import Path
import time

from .benchmarks import _write_immutable, _metrics
from .candidates_v4 import GeneIndex, taxon_context, term_key
from .candidates_v5 import UnionCandidates
from .db import digest, dumps, now
from .features_v5 import (
    C_GRID,
    FEATURE_SHA,
    FEATURE_SPEC,
    SEED,
    THRESHOLDS,
    identity_features,
    pair_candidates,
    pool_features,
    scope_features,
    span_features,
    tokens_for,
)
from .nlp_v2 import parser_for, validate_tokens
from .relations_v5 import BagClassifier, binary_fit
from .text import sentence_spans


def native_ids(entity):
    return entity.get("native_ids", []) or [entity.get("normalized_id") or entity["text"]]


def prepare_features(data, models, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    raw = json.loads(Path(data).read_bytes())
    if raw.get("split") != "train":
        raise ValueError("Candidate features require official train labels")
    pointer = json.loads((Path(models) / "gene-snapshot.json").read_bytes())
    index = GeneIndex.load(Path(models) / "genes" / pointer["filename"])
    provider = UnionCandidates(models)
    provider.load()
    parser = parser_for({})
    files = []
    for number, doc in enumerate(raw["documents"]):
        path = output / (doc["pmid"] + ".json")
        if not path.exists():
            sentences = sentence_spans(doc["text"], doc["sections"])
            pairs = pair_candidates(sentences, doc["entities"], lambda s: validate_tokens(parser.parse(s), s))
            grouped = defaultdict(list)
            for pair in pairs:
                for chemical in native_ids(pair["chemical"]):
                    for gene in native_ids(pair["gene"]):
                        grouped[(chemical, gene)].append(pair["features"])
            labels = defaultdict(set)
            directions = {}
            for relation in doc["relations"]:
                for key in grouped:
                    if set(key) == {relation["entity1"], relation["entity2"]}:
                        labels[key].add(relation["predicate"])
                        if relation.get("subject"):
                            directions[key] = int(relation["subject"] == key[0])
            bags = [
                {
                    "group": list(key),
                    "features": rows,
                    "labels": sorted(labels[key]),
                    "direction": directions.get(key),
                }
                for key, rows in grouped.items()
            ]
            gold = {(e["start"], e["end"]) for e in doc["entities"] if e["kind"] == "gene"}
            predicted = [e for e in provider.predict(doc["text"]) if e["kind"] == "gene"]
            spans = [
                {
                    "features": span_features(e, doc["text"]),
                    "label": int((e["start"], e["end"]) in gold),
                    "origin": e["payload"]["origin"],
                    "start": e["start"],
                    "end": e["end"],
                }
                for e in predicted
            ]
            identities = []
            for entity in doc["entities"]:
                if entity["kind"] != "gene":
                    continue
                sentence = next(
                    (s for s in sentences if s["start"] <= entity["start"] < entity["end"] <= s["end"]), None
                )
                if sentence is None:
                    continue
                taxon, _, _ = taxon_context(sentence["text"], doc["text"], {})
                for record in index.index.get(term_key(entity["text"]), []):
                    identities.append(
                        {
                            "features": identity_features(entity, record, sentence["text"], taxon),
                            "label": int(record["native_id"] in native_ids(entity)),
                        }
                    )
            _write_immutable(
                path,
                {
                    "pmid": doc["pmid"],
                    "source_hash": digest(doc["text"]),
                    "bags": bags,
                    "spans": spans,
                    "identities": identities,
                    "gold_gene_count": len(gold),
                    "feature_sha256": FEATURE_SHA,
                    "provider_manifest": provider.manifest,
                },
            )
        value = json.loads(path.read_bytes())
        if value["source_hash"] != digest(doc["text"]) or value["feature_sha256"] != FEATURE_SHA:
            raise ValueError("Cached train features differ")
        files.append({"pmid": doc["pmid"], "filename": path.name, "sha256": digest(path.read_bytes())})
        if (number + 1) % 10 == 0:
            print(
                dumps({"stage": "train_candidates", "articles": number + 1, "total": len(raw["documents"])}),
                flush=True,
            )
    manifest = {
        "training_split": "train",
        "source_sha256": digest(Path(data).read_bytes()),
        "feature_sha256": FEATURE_SHA,
        "files": files,
        "provider": provider.manifest,
    }
    _write_immutable(output / "manifest.json", manifest)
    return manifest


def folds(pmids):
    ranked = sorted(set(pmids), key=lambda p: digest(str(SEED) + ":" + p))
    return {p: i % 5 for i, p in enumerate(ranked)}


def counts(y, scores, threshold):
    expected = {i for i, v in enumerate(y) if v}
    predicted = {i for i, v in enumerate(scores) if v >= threshold}
    return _metrics(expected, predicted)


def choose_threshold(y, scores, precision=0.85):
    values = [{"threshold": t, **counts(y, scores, t)} for t in THRESHOLDS]
    eligible = [
        r for r in values if r["precision"] is not None and r["precision"] >= precision and r["tp"] > 0
    ]
    selected = (
        max(eligible, key=lambda r: (r["recall"], r["f1"], r["threshold"]))
        if eligible
        else next(r for r in values if r["threshold"] == 0.5)
    )
    return {**selected, "precision_target_met": bool(eligible), "target_precision": precision, "grid": values}


def train(features, output, audit):
    import joblib
    import numpy as np

    started = time.monotonic()
    output = Path(output)
    if (output / "manifest.json").exists():
        raise ValueError("Training artifacts immutable; choose a new directory")
    source = json.loads((Path(features) / "manifest.json").read_bytes())
    documents = []
    for entry in source["files"]:
        path = Path(features) / entry["filename"]
        if digest(path.read_bytes()) != entry["sha256"]:
            raise ValueError("Train feature file changed")
        documents.append(json.loads(path.read_bytes()))
    audit_record = json.loads(Path(audit).read_bytes())
    protected = set(audit_record["protected_pmids"])
    if protected & {d["pmid"] for d in documents}:
        raise ValueError("Held-out PMID in train features")
    fold = folds([d["pmid"] for d in documents])
    rows = [(d["pmid"], b) for d in documents for b in d["bags"]]
    labels = sorted({label for _, b in rows for label in b["labels"]})
    experiments = []
    for mode in ("logistic", "mil"):
        for C in C_GRID:
            start = time.monotonic()
            scores = [None] * len(rows)
            for k in range(5):
                train_rows = [b for p, b in rows if fold[p] != k]
                test_indices = [i for i, (p, _) in enumerate(rows) if fold[p] == k]
                model = BagClassifier(mode, C).fit(
                    [b["features"] for b in train_rows], [b["labels"] for b in train_rows], labels
                )
                predicted = model.predict([rows[i][1]["features"] for i in test_indices])
                for i, value in zip(test_indices, predicted):
                    scores[i] = value
            y = [int(label in b["labels"]) for _, b in rows for label in labels]
            flat = [scores[i][label]["score"] for i in range(len(rows)) for label in labels]
            selected = choose_threshold(y, flat)
            experiments.append(
                {"mode": mode, "C": C, "selection": selected, "seconds": time.monotonic() - start}
            )
            print(dumps({"stage": "relation_cv", **experiments[-1]}), flush=True)
    eligible = [e for e in experiments if e["selection"]["precision_target_met"]]
    chosen = max(
        eligible or experiments,
        key=lambda e: (
            e["selection"]["recall"] if eligible else e["selection"]["f1"],
            e["selection"]["f1"],
            -e["seconds"],
        ),
    )
    relation = BagClassifier(chosen["mode"], chosen["C"]).fit(
        [b["features"] for _, b in rows], [b["labels"] for _, b in rows], labels
    )
    selected_models = {"relation": relation}
    for experiment in experiments:
        name = "relation_" + experiment["mode"] + "_" + str(experiment["C"]).replace(".", "p")
        selected_models[name] = BagClassifier(experiment["mode"], experiment["C"]).fit(
            [b["features"] for _, b in rows], [b["labels"] for _, b in rows], labels
        )
    cv = {"relation": experiments}
    for name, field, target in (("span", "spans", 0.9), ("identity", "identities", 0.9)):
        samples = [(d["pmid"], r) for d in documents for r in d[field]]
        trials = []
        for C in C_GRID:
            scores = np.zeros(len(samples))
            for k in range(5):
                train_rows = [r for p, r in samples if fold[p] != k]
                indices = [i for i, (p, _) in enumerate(samples) if fold[p] == k]
                model = binary_fit([r["features"] for r in train_rows], [r["label"] for r in train_rows], C)
                if indices:
                    scores[indices] = model.predict_proba([samples[i][1]["features"] for i in indices])[:, 1]
            trials.append(
                {"C": C, "selection": choose_threshold([r["label"] for _, r in samples], scores, target)}
            )
        valid = [r for r in trials if r["selection"]["precision_target_met"]]
        best = max(
            valid or trials,
            key=lambda r: (r["selection"]["recall"] if valid else r["selection"]["f1"], r["selection"]["f1"]),
        )
        selected_models[name] = binary_fit(
            [r["features"] for _, r in samples], [r["label"] for _, r in samples], best["C"]
        )
        cv[name] = {"trials": trials, "selected": best, "candidate_samples": len(samples)}
    directed = [b for _, b in rows if b["direction"] is not None]
    span_profiles = {}
    for provider, origin in (("biomedbert", "bc2gm_biomedbert"), ("bionlp13cg", "bionlp13cg")):
        samples = [(d["pmid"], r) for d in documents for r in d["spans"] if r["origin"] in {origin, "union"}]

        def provider_features(row):
            return {
                **{k: v for k, v in row["features"].items() if not k.startswith("origin=")},
                "origin=" + origin: 1,
            }

        trials = []
        for C in C_GRID:
            scores = np.zeros(len(samples))
            for k in range(5):
                tr = [r for p, r in samples if fold[p] != k]
                ti = [i for i, (p, _) in enumerate(samples) if fold[p] == k]
                model = binary_fit([provider_features(r) for r in tr], [r["label"] for r in tr], C)
                if ti:
                    scores[ti] = model.predict_proba([provider_features(samples[i][1]) for i in ti])[:, 1]
            trials.append(
                {"C": C, "selection": choose_threshold([r["label"] for _, r in samples], scores, 0.9)}
            )
        valid = [r for r in trials if r["selection"]["precision_target_met"]]
        best = max(
            valid or trials,
            key=lambda r: (
                (r["selection"]["recall"] if valid else r["selection"]["f1"]) or 0,
                r["selection"]["f1"] or 0,
            ),
        )
        selected_models["span_" + provider] = binary_fit(
            [provider_features(r) for _, r in samples], [r["label"] for _, r in samples], best["C"]
        )
        span_profiles[provider] = {
            "threshold": best["selection"]["threshold"],
            "selected": best,
            "trials": trials,
        }
    selected_models["direction"] = binary_fit(
        [pool_features(b["features"]) for b in directed], [b["direction"] for b in directed], chosen["C"]
    )
    output.mkdir(parents=True, exist_ok=True)
    for name, model in selected_models.items():
        joblib.dump(model, output / (name + ".joblib"), compress=3)
    import shutil

    scope = output.parent / "scope.joblib"
    scope_manifest = json.loads((output.parent / "scope-manifest.json").read_bytes())
    if digest(scope.read_bytes()) != scope_manifest["sha256"]:
        raise ValueError("Scope model changed before publication")
    shutil.copy2(scope, output / "scope.joblib")
    manifest = {
        "schema": "cpu-learners-v5",
        "created_at": now(),
        "training_split": "train",
        "test_used_for_selection": False,
        "feature_sha256": FEATURE_SHA,
        "feature_spec": FEATURE_SPEC,
        "seed": SEED,
        "fold_assignments": fold,
        "training_pmids": sorted(fold),
        "feature_manifest_sha256": digest((Path(features) / "manifest.json").read_bytes()),
        "audit_sha256": digest(Path(audit).read_bytes()),
        "labels": labels,
        "relation_mode": chosen["mode"],
        "relation_threshold": chosen["selection"]["threshold"],
        "relation_selection": chosen,
        "relation_configurations": [
            "relation_" + e["mode"] + "_" + str(e["C"]).replace(".", "p") for e in experiments
        ],
        "span_threshold": cv["span"]["selected"]["selection"]["threshold"],
        "identity_threshold": cv["identity"]["selected"]["selection"]["threshold"],
        "identity_margin": 0.15,
        "span_profiles": span_profiles,
        "direction_threshold": 0.7,
        "cv": cv,
        "scores_are_nutrition_probabilities": False,
        "scope_training": scope_manifest,
        "elapsed_seconds": time.monotonic() - started,
        "files": {p.name: digest(p.read_bytes()) for p in output.glob("*.joblib")},
    }
    _write_immutable(output / "manifest.json", manifest)
    return manifest


def train_scope(gold, reserved_pmids, output):
    import joblib
    import sklearn_crfsuite

    raw = json.loads(Path(gold).read_bytes())
    if raw.get("split") != "development":
        raise ValueError("BioScope test labels cannot train CRFs")
    documents = [d for d in raw["documents"] if d["pmid"] not in set(reserved_pmids)]
    Xc = {k: [] for k in ("negation", "speculation")}
    Yc = {k: [] for k in Xc}
    Xs = {k: [] for k in Xc}
    Ys = {k: [] for k in Xc}
    for doc in documents:
        for sentence in sentence_spans(doc["text"], doc["sections"]):
            tokens = tokens_for(sentence)
            if not tokens:
                continue
            for kind in Xc:
                cues = [
                    c
                    for c in doc.get("cues", [])
                    if c["kind"] == kind and sentence["start"] <= c["start"] < c["end"] <= sentence["end"]
                ]
                Xc[kind].append([scope_features(tokens, i) for i in range(len(tokens))])
                Yc[kind].append(
                    [
                        "C" if any(c["start"] < t["end"] and t["start"] < c["end"] for c in cues) else "O"
                        for t in tokens
                    ]
                )
                for cue in cues:
                    indices = [
                        i for i, t in enumerate(tokens) if cue["start"] < t["end"] and t["start"] < cue["end"]
                    ]
                    linked = [s for s in doc["scopes"] if s["cue_id"] == cue["cue_id"]]
                    Xs[kind].append([scope_features(tokens, i, indices) for i in range(len(tokens))])
                    Ys[kind].append(
                        [
                            "S"
                            if any(s["start"] < t["end"] and t["start"] < s["end"] for s in linked)
                            else "O"
                            for t in tokens
                        ]
                    )
    models = {}
    for kind in Xc:
        for field, X, Y in (("cue", Xc[kind], Yc[kind]), ("scope", Xs[kind], Ys[kind])):
            if not X:
                raise ValueError("No training annotations for " + kind + field)
            model = sklearn_crfsuite.CRF(
                algorithm="lbfgs", c1=0.1, c2=0.1, max_iterations=50, all_possible_transitions=True
            )
            model.fit(X, Y)
            models[kind + "_" + field] = model
    output = Path(output)
    if output.exists():
        raise ValueError("Scope model path already exists")
    joblib.dump(models, output, compress=3)
    return {
        "training_pmids": sorted(d["pmid"] for d in documents),
        "reserved_pmids": sorted(reserved_pmids),
        "source_sha256": digest(Path(gold).read_bytes()),
        "sha256": digest(output.read_bytes()),
        "parameters": {"c1": 0.1, "c2": 0.1, "iterations": 50},
        "preexisting_development_exposure": True,
    }
