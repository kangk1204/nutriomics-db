"""Explicit scope-only CPU recovery when legacy full v5 artifacts are unreachable."""

import json
from pathlib import Path

from .db import digest


def feature_hash():
    folder = Path(__file__).parent
    return digest(b"\0".join((folder / name).read_bytes()
                            for name in ("features_v5.py", "scope_v5.py", "training_v5.py")))


class RecoveredScope:
    def __init__(self, directory):
        self.path = Path(directory)
        self.models = None
        self.manifest = {"available": False}

    def load(self):
        if self.models is not None:
            return
        raw = (self.path / "manifest.json").read_bytes()
        record = json.loads(raw)
        if (record.get("schema") != "recovered-scope-v6-1"
                or record.get("training_split") != "train"
                or record.get("feature_sha256") != feature_hash()
                or record.get("test_used_for_selection") is not False
                or record.get("protected_overlap") != 0):
            raise ValueError("Recovered scope training/feature provenance differs")
        path = self.path / "scope.joblib"
        if digest(path.read_bytes()) != record["model_sha256"]:
            raise ValueError("Recovered scope model hash differs")
        import joblib

        models = joblib.load(path)
        if set(models) != {kind + "_" + field for kind in ("negation", "speculation")
                           for field in ("cue", "scope")}:
            raise ValueError("Recovered scope model components incomplete")
        self.models = {"scope": models}
        self.manifest = {**record, "available": True, "manifest_sha256": digest(raw),
                         "component_scope": "negation_speculation_only",
                         "legacy_v5_full_model_restored": False}
