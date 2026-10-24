"""Train-only noise-aware CPU relation heads, frozen upstream deployment path."""
from collections import Counter
import gzip
import json
from pathlib import Path

from .db import digest, dumps, now

TYPES = ("Association", "Bind", "Negative_Correlation", "Positive_Correlation")
GRID = (0.1, 1.0, 10.0)
THRESHOLDS = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95)
LOADED_SOURCE_SHA = digest(Path(__file__).read_bytes())


def source_sha():
    current = digest(Path(__file__).read_bytes())
    if current != LOADED_SOURCE_SHA:
        raise ValueError("Relation implementation changed after import; restart with a frozen source")
    return current


class NoiseAwareHead:
    def fit(self, rows, labels, weights, c):
        import numpy as np
        from sklearn.feature_extraction import DictVectorizer
        from sklearn.preprocessing import MaxAbsScaler
        from sklearn.linear_model import LogisticRegression

        self.vectorizer = DictVectorizer(dtype=np.float32)
        matrix = self.vectorizer.fit_transform(rows)
        self.scaler = MaxAbsScaler().fit(matrix)
        matrix = self.scaler.transform(matrix)
        self.models = {}
        for label in TYPES:
            y = [int(label in values) for values in labels]
            if len(set(y)) == 1:
                self.models[label] = float(y[0])
                continue
            model = LogisticRegression(C=c, solver="liblinear", class_weight="balanced", max_iter=400,
                                       random_state=20261010).fit(matrix, y, sample_weight=weights)
            self.models[label] = model
        return self

    def predict(self, rows):
        import numpy as np
        if not rows:
            return []
        matrix = self.scaler.transform(self.vectorizer.transform(rows))
        columns = {key: np.full(len(rows), model) if isinstance(model, float) else model.predict_proba(matrix)[:, list(model.classes_).index(1)] for key, model in self.models.items()}
        return [{key: float(values[i]) for key, values in columns.items()} for i in range(len(rows))]


def train(features, gold_export, output):
    import joblib
    from .training_v6 import folds
    from .automatic_features_v6 import source_sha as feature_source
    from .benchmarks import _metrics, _write_immutable

    features, output = Path(features), Path(output)
    manifest = json.loads((features / "manifest.json").read_bytes())
    assert manifest["training_split"] == "train" and manifest["identity"]["source_sha256"] == feature_source()
    records, gold, pmids = [], set(), set()
    for file in manifest["files"]:
        raw = (features / file["path"]).read_bytes()
        assert digest(raw) == file["sha256"]
        doc = json.loads(raw)
        pmid = doc["pmid"]
        pmids.add(pmid)
        for r in doc["gold_relations"]:
            if r["predicate"] in TYPES:
                # Gold corpus includes other entity-pair types; only annotate
                # chemical-gene keys represented in the official export below.
                pass
        for i, bag in enumerate(doc["bags"]):
            ids = []
            for role in ("chemical", "gene"):
                from .automatic_features_v6 import native_id
                values = set().union(*(native_id(p[role]) for p in bag["endpoints"]))
                ids.append(sorted(values))
            records.append({"pmid": pmid, "features": bag["features"], "labels": bag["labels"],
                            "weight": 1.0, "source": "automatic", "ids": ids})
    archive = json.loads(gzip.decompress(Path(gold_export).read_bytes()))
    assert archive["source_features_manifest"]["audit_sha256"] == manifest["identity"]["audit_sha256"]
    # Correct gold chemical-gene targets are recovered from fully annotated
    # training documents through the exact official bags, without adding other
    # entity-pair relations to the denominator.
    for doc in archive["papers"]:
        if doc["pmid"] not in pmids:
            continue
        for bag in doc["bags"]:
            clean_quality = {"quality:witnesses": bag["witnesses"]}
            for role in ("chemical", "gene"):
                clean_quality.update({"quality:" + role + ":resolved": 1., "quality:" + role + ":span_score": 1.,
                                      "quality:" + role + ":selected": 1., "quality:" + role + ":reason:None": 1.})
            records.append({"pmid": doc["pmid"], "features": {**bag["features"], **clean_quality}, "labels": bag["labels"],
                            "weight": 0.5, "source": "gold_augmentation", "ids": []})
    # OOF uses all automatic bags and their own labels, including endpoint
    # mistakes. Missing candidate relations are separately reported, never
    # removed from development scoring.
    automatic = [i for i, r in enumerate(records) if r["source"] == "automatic"]
    expected = {(i, t) for i in automatic for t in records[i]["labels"]}
    assignment = folds(sorted(pmids))
    configurations = []
    for c in GRID:
        scores = {}
        for fold in range(5):
            fit = [r for r in records if assignment[r["pmid"]] != fold]
            held = [i for i in automatic if assignment[records[i]["pmid"]] == fold]
            model = NoiseAwareHead().fit([r["features"] for r in fit], [r["labels"] for r in fit], [r["weight"] for r in fit], c)
            scores.update(zip(held, model.predict([records[i]["features"] for i in held])))
        for threshold in THRESHOLDS:
            predicted = {(i, t) for i, row in scores.items() for t, score in row.items()
                         if score >= threshold and all(records[i]["ids"])}
            configurations.append({"C": c, "threshold": threshold, **_metrics(expected, predicted)})
        print(dumps({"stage": "automatic_relation_oof", "C": c, "configurations": configurations[-len(THRESHOLDS):]}), flush=True)
    eligible = [r for r in configurations if r["tp"] and r["precision"] >= .85 and r["recall"] >= .30]
    selection = max(eligible or configurations, key=lambda r: (r["f1"], r["recall"], r["precision"], -r["C"]))
    output.mkdir(parents=True, exist_ok=True)
    if (output / "manifest.json").exists():
        raise ValueError("Model output is immutable")
    model = NoiseAwareHead().fit([r["features"] for r in records], [r["labels"] for r in records], [r["weight"] for r in records], selection["C"])
    joblib.dump(model, output / "relations.joblib")
    receipt = {
        "schema": "automatic-noise-relation-v6-r4-1", "created_at": now(),
        "source_sha256": source_sha(), "feature_source_sha256": feature_source(),
        "training_split": "train", "training_pmids": sorted(pmids), "test_used_for_selection": False,
        "feature_manifest_sha256": digest((features / "manifest.json").read_bytes()),
        "gold_export_sha256": digest(Path(gold_export).read_bytes()), "selection": selection,
        "selection_source": "paper-separated train OOF automatic bags; frozen upstream models not fold-refit",
        "selection_denominator": "recoverable automatic training bags, not full-corpus recall",
        "upstream_crossfit_limitation": True, "configurations": configurations,
        "counts": dict(Counter(r["source"] for r in records)),
        "positive_automatic_bags": sum(bool(records[i]["labels"]) for i in automatic),
        "files": {"relations.joblib": digest((output / "relations.joblib").read_bytes())},
        "fold_assignments": assignment, "ids_in_features": False,
    }
    _write_immutable(output / "manifest.json", receipt)
    return receipt


def load(path):
    import joblib
    from .automatic_features_v6 import source_sha as feature_source
    path = Path(path)
    manifest = json.loads((path / "manifest.json").read_bytes())
    assert manifest["source_sha256"] == source_sha() and manifest["feature_source_sha256"] == feature_source()
    assert manifest["training_split"] == "train" and manifest["test_used_for_selection"] is False
    for name, sha in manifest["files"].items():
        assert Path(name).name == name and digest((path / name).read_bytes()) == sha
    return joblib.load(path / "relations.joblib"), manifest
