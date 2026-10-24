"""Immutable joblib CPU artifacts. Deserialization is local, hash checked first."""

import json
from pathlib import Path

from .db import digest
from .features_v5 import FEATURE_SHA


class LearnedModels:
    def __init__(
        self, directory, relation_configuration=None, relation_threshold=None, span_provider="union"
    ):
        self.path = Path(directory)
        self.manifest = {"available": False}
        self.models = None
        self.configuration = relation_configuration
        self.threshold = relation_threshold
        self.span_provider = span_provider

    def load(self):
        if self.models is not None:
            return
        import joblib

        value = json.loads((self.path / "manifest.json").read_bytes())
        if value.get("feature_sha256") != FEATURE_SHA or value.get("training_split") != "train":
            raise ValueError("V5 feature definition or training split differs")
        for name, expected in value["files"].items():
            if Path(name).name != name or digest((self.path / name).read_bytes()) != expected:
                raise ValueError("V5 model artifact changed: " + name)
        self.models = {name.removesuffix(".joblib"): joblib.load(self.path / name) for name in value["files"]}
        self.manifest = {
            **value,
            "available": True,
            "manifest_sha256": digest((self.path / "manifest.json").read_bytes()),
        }
        if self.span_provider != "union":
            if self.span_provider not in value.get("span_profiles", {}):
                raise ValueError("Provider-specific span reranker missing")
            self.models["span"] = self.models["span_" + self.span_provider]
            self.manifest["span_threshold"] = value["span_profiles"][self.span_provider]["threshold"]
        self.manifest["span_provider"] = self.span_provider
        if self.configuration is not None:
            if self.configuration not in value.get("relation_configurations", []):
                raise ValueError("Unknown trained relation configuration")
            self.models["relation"] = self.models[self.configuration]
            self.manifest["active_relation_configuration"] = self.configuration
        if self.threshold is not None:
            from .features_v5 import THRESHOLDS

            if self.threshold not in THRESHOLDS:
                raise ValueError("Threshold outside preregistered grid")
            self.manifest["relation_threshold"] = self.threshold

    def score(self, name, features):
        self.load()
        model = self.models[name]
        from .nlp_v2 import cpu_inference_lock

        with cpu_inference_lock():
            return model.predict_proba(features)[:, list(model.classes_).index(1)]
