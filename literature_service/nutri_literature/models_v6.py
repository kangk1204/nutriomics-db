"""Lightweight, train-only CPU heads; no backbone optimization."""

import json
from pathlib import Path

from .db import digest, dumps

SEED = 20261010
C_GRID = (0.1, 1.0, 10.0)
THRESHOLDS = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95)
ID_MARGINS = (0.0, 0.25, 0.5, 1.0, 2.0, 4.0)
FEATURE_SPEC = {
    "schema": "cpu-v6-1",
    "ids_in_features": False,
    "bag": "surface-definition-official-unique-alias-taxon-v6",
    "pooling": "mean/max frozen context + mean masked dependency features",
    "heads": ["span", "existence", "type", "direction"],
    "span": "raw union NER + frozen span/context; no early candidate deletion",
    "identity": "pairwise candidate differences",
    "direction_policy": "BioREDirect positive+no subject=No_Direct; unannotated other corpora excluded",
    "seed": SEED,
    "C_grid": C_GRID,
    "identity_margin_grid": ID_MARGINS,
    "threshold_source": "train-paper-OOF-only",
}
FEATURE_SHA = digest(dumps(FEATURE_SPEC))


class LinearHead:
    def fit(self, rows, labels, c=1):
        from sklearn.feature_extraction import DictVectorizer
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import MaxAbsScaler

        self.vectorizer = DictVectorizer()
        matrix = self.vectorizer.fit_transform(rows)
        self.scale = MaxAbsScaler().fit(matrix)
        matrix = self.scale.transform(matrix)
        self.constant = labels[0] if len(set(labels)) == 1 else None
        self.model = (
            None
            if self.constant is not None
            else LogisticRegression(C=c, class_weight="balanced", random_state=SEED, max_iter=1000).fit(
                matrix, labels
            )
        )
        self.classes_ = [self.constant] if self.model is None else list(self.model.classes_)
        return self

    def probabilities(self, rows):
        import numpy as np

        if not rows:
            return []
        if self.model is None:
            return [{str(self.constant): 1.0} for _ in rows]
        values = self.model.predict_proba(self.scale.transform(self.vectorizer.transform(rows)))
        return [{str(k): float(v) for k, v in zip(self.classes_, row)} for row in np.asarray(values)]

    def rank(self, rows):
        if self.model is None:
            return [0.0] * len(rows)
        return self.model.decision_function(self.scale.transform(self.vectorizer.transform(rows))).tolist()


class Models:
    def __init__(self, directory):
        self.path = Path(directory)
        self.manifest = {"available": False}
        self.heads = {}
        self.margin = 1.0

    def load(self):
        if self.heads:
            return
        import joblib

        raw = (self.path / "manifest.json").read_bytes()
        manifest = json.loads(raw)
        if (
            manifest.get("feature_sha256") != FEATURE_SHA
            or manifest.get("training_split") != "train"
            or manifest.get("test_used_for_selection") is not False
        ):
            raise ValueError("V6 requires an audited train-only artifact")
        for name, sha in manifest["files"].items():
            if Path(name).name != name or digest((self.path / name).read_bytes()) != sha:
                raise ValueError("V6 model file/hash mismatch")
        from .features_v6 import source_hashes

        if manifest.get("feature_source_hashes") != source_hashes():
            raise ValueError("V6 feature implementation changed; use a newly trained artifact")
        self.heads = {name[:-7]: joblib.load(self.path / name) for name in manifest["files"]}
        clinical = {}
        for kind in ("outcome", "evidence"):
            directory = self.path.parent / ("clinical-v6-" + kind)
            if not (directory / "manifest.json").exists():
                continue
            raw_clinical = (directory / "manifest.json").read_bytes()
            record = json.loads(raw_clinical)
            if (
                record.get("feature_sha256") != FEATURE_SHA
                or record.get("feature_source_hashes") != source_hashes("clinical")
                or record.get("encoder") != manifest["encoder"]
                or record.get("training_split") != "train"
                or record.get("test_used_for_selection") is not False
            ):
                raise ValueError("Clinical V6 provenance differs")
            for filename, sha in record["files"].items():
                if Path(filename).name != filename or digest((directory / filename).read_bytes()) != sha:
                    raise ValueError("Clinical V6 file/hash differs")
            self.heads[kind] = joblib.load(directory / (kind + ".joblib"))
            clinical[kind] = {**record, "manifest_sha256": digest(raw_clinical)}
        self.margin = manifest.get("identity_margin", 1.0)
        self.manifest = {
            **manifest,
            "available": True,
            "device": "cpu",
            "manifest_sha256": digest(raw),
            "clinical_heads": clinical,
        }

    def rank(self, rows):
        self.load()
        return self.heads["identity"].rank(rows)

    def predict(self, features):
        self.load()
        existence = self.heads["existence"].probabilities(features)
        types = {
            label: head.probabilities(features)
            for label, head in self.heads.items()
            if label.startswith("type_")
        }
        directions = self.heads["direction"].probabilities(features)
        result = []
        for i, scores in enumerate(existence):
            result.append(
                {
                    "existence": scores.get("1", 0),
                    "types": {label[5:]: values[i].get("1", 0) for label, values in types.items()},
                    "direction": directions[i],
                }
            )
        return result
