"""Paper-separated outcome/evidence heads using only audited training spans."""

import json
import os
from pathlib import Path
import re

from .benchmarks import _write_immutable
from .clinical_v6 import outcome_candidates
from .clinical_v4 import ClinicalModels
from .db import digest, dumps, now
from .embeddings_v6 import FrozenEncoder
from .models_v6 import C_GRID, FEATURE_SHA, SEED
from .training_v6 import folds, threshold
from .text import sentence_spans
from .training_v4 import text_key
from .nlp_v2 import parser_for, validate_tokens
from .features_v6 import source_hashes


def inference_features(text, sentence, comparison, encoder):
    vector = encoder.encode(text, sentence).reshape(-1)
    features = {"context_" + str(i): float(v) for i, v in enumerate(vector)}
    words = set(re.findall(r"\w+", sentence["text"].casefold()))
    for field in ("intervention_description", "comparator_description", "outcome_description"):
        description = comparison[field]
        prompt = {"start": 0, "end": len(description), "text": description}
        if not description.strip():
            features[field + "_missing"] = 1
            continue
        prompt_vector = encoder.encode(description, prompt).reshape(-1)
        import numpy as np

        features[field + "_similarity"] = float(
            np.dot(vector, prompt_vector) / max(1e-12, np.linalg.norm(vector) * np.linalg.norm(prompt_vector))
        )
        tokens = set(re.findall(r"\w+", description.casefold()))
        features[field + "_coverage"] = len(tokens & words) / max(1, len(tokens))
    return features


class DenseClinicalHead:
    """Fixed numeric columns, avoiding a Python dictionary per cached vector."""

    def __init__(self, columns):
        self.columns = tuple(columns)

    def fit(self, matrix, labels, c=1):
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import MaxAbsScaler

        self.scale = MaxAbsScaler().fit(matrix)
        self.constant = int(labels[0]) if len(set(labels)) == 1 else None
        self.model = None if self.constant is not None else LogisticRegression(
            C=c, class_weight="balanced", random_state=SEED, max_iter=1000
        ).fit(self.scale.transform(matrix), labels)
        self.classes_ = [self.constant] if self.model is None else list(self.model.classes_)
        if not np.isfinite(self.scale.scale_).all():
            raise ValueError("Clinical scale contains non-finite values")
        return self

    def matrix_probabilities(self, matrix):
        import numpy as np

        if self.model is None:
            return np.ones((len(matrix), 1), dtype=np.float32)
        return self.model.predict_proba(self.scale.transform(matrix))

    def probabilities(self, rows):
        import numpy as np

        if not rows:
            return []
        matrix = np.asarray([[r.get(k, 0) for k in self.columns] for r in rows], dtype=np.float32)
        values = self.matrix_probabilities(matrix)
        return [{str(k): float(v) for k, v in zip(self.classes_, row)} for row in values]


def numeric_examples(documents, columns, directory, identity, generate):
    """Hash-checked paper checkpoints and an on-disk matrix; no corpus-size dict list."""
    import numpy as np
    import os
    import uuid

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    checkpoints = []
    columns = tuple(columns)
    for index, doc in enumerate(documents):
        key = digest(dumps({"identity": identity, "document_sha256": digest(dumps(doc))}))
        data, receipt = directory / (key[:32] + ".npz"), directory / (key[:32] + ".json")
        expected = {"key": key, "pmid": str(doc["pmid"]), "columns": list(columns),
                    "filename": data.name}
        if receipt.exists():
            record = json.loads(receipt.read_bytes())
            if any(record.get(k) != v for k, v in expected.items()) or not data.exists():
                raise ValueError("Clinical feature checkpoint metadata differs")
            if digest(data.read_bytes()) != record.get("sha256"):
                raise ValueError("Clinical feature checkpoint hash differs")
            with np.load(data, allow_pickle=False) as saved:
                matrix, labels = saved["matrix"], saved["labels"]
        else:
            vectors, labels = [], []
            for features, label in generate(doc):
                if set(features) - set(columns):
                    raise ValueError("Unknown clinical feature column")
                vectors.append(np.asarray([features.get(k, 0) for k in columns], dtype=np.float32))
                labels.append(int(label))
            matrix = np.asarray(vectors, dtype=np.float32).reshape(-1, len(columns))
            labels = np.asarray(labels, dtype=np.int8)
            temporary = directory / (uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("wb") as stream:
                    np.savez_compressed(stream, matrix=matrix, labels=labels)
                os.replace(temporary, data)
            finally:
                temporary.unlink(missing_ok=True)
            record = {**expected, "rows": len(labels), "sha256": digest(data.read_bytes())}
            _write_immutable(receipt, record)
        if (
            matrix.shape != (record["rows"], len(columns))
            or labels.shape != (record["rows"],)
            or matrix.dtype != np.float32
            or not np.isfinite(matrix).all()
            or not set(labels.tolist()).issubset({0, 1})
        ):
            raise ValueError("Invalid clinical numeric feature checkpoint")
        checkpoints.append(record)
        del matrix, labels
        if (index + 1) % 10 == 0:
            print(dumps({"clinical_train_prepared": index + 1, "articles": len(documents),
                         "examples": sum(r["rows"] for r in checkpoints)}), flush=True)
    total = sum(record["rows"] for record in checkpoints)
    if not total:
        raise ValueError("Clinical candidate generation produced no training examples")
    # Rebuild these disposable matrices from immutable paper checkpoints after interruption.
    matrix = np.lib.format.open_memmap(directory / "matrix.npy", mode="w+", dtype=np.float32,
                                       shape=(total, len(columns)))
    labels = np.lib.format.open_memmap(directory / "labels.npy", mode="w+", dtype=np.int8, shape=(total,))
    pmids, offset = [], 0
    for record in checkpoints:
        with np.load(directory / record["filename"], allow_pickle=False) as saved:
            end = offset + record["rows"]
            matrix[offset:end], labels[offset:end] = saved["matrix"], saved["labels"]
            pmids.extend([record["pmid"]] * record["rows"])
            offset = end
    matrix.flush()
    labels.flush()
    return matrix, labels, pmids, checkpoints


def train(data, kind, models, cache, audit_path, output):
    from .locking import lock

    if kind not in {"outcome", "evidence"}:
        raise ValueError("Unknown clinical learning task")
    with lock(Path(cache) / "clinical-training" / (kind + "-fitting.lock")):
        return _train(data, kind, models, cache, audit_path, output)


def _train(data, kind, models, cache, audit_path, output):
    import joblib

    value = json.loads(Path(data).read_bytes())
    audit = json.loads(Path(audit_path).read_bytes())
    if value.get("split") != "train" or not audit.get("passed") or audit.get("protected_overlap", 1) != 0:
        raise ValueError("Clinical heads require audited train-only inputs")
    if kind == "evidence" and any(
        doc.get("comparisons") and doc.get("pmid_mapping_known") is not True for doc in value["documents"]
    ):
        raise ValueError("ICO learning requires official PMCID-to-PMID mapping before the overlap audit")
    if digest(Path(data).read_bytes()) != audit.get("audited_training_sha256"):
        raise ValueError("Clinical data differs from audited source")
    allowed = set(audit["training_pmids"])
    protected = set(audit.get("protected_text_hashes", []))
    directory = Path(models)
    from .audit_v6 import upstream_clinical

    clinical_directory = Path(os.environ.get("LITERATURE_V6_CLINICAL_HOME", str(directory / "clinical")))
    upstream_audit = upstream_clinical(clinical_directory, audit)
    encoder = FrozenEncoder(directory / "gene/selected_model", cache)
    encoder.load()
    clinical = ClinicalModels(clinical_directory)
    clinical.load()
    parser = parser_for({})
    documents = [doc for doc in value["documents"]
                 if str(doc["pmid"]) in allowed and text_key(doc["text"]) not in protected]
    if kind not in {"outcome", "evidence"}:
        raise ValueError("Unknown clinical learning task")

    def generate(doc):
        sentences = sentence_spans(doc["text"], doc["sections"])
        if kind == "outcome":
            annotated = clinical.predict(sentences)
            spans = [s for s in doc.get("annotated_spans", []) if s["label"] == "outcomes"]
            for sentence in annotated:
                tokens = validate_tokens(parser.parse(sentence), sentence)
                candidates = outcome_candidates(sentence, tokens)
                if not candidates:
                    continue
                vectors = encoder.encode(doc["text"], sentence, tuple(candidates))
                if vectors.shape != (len(candidates) + 1, encoder.manifest["dimension"]):
                    raise ValueError("Batched clinical context/span vector shape differs")
                for index, candidate in enumerate(candidates):
                    label = int(
                        any(
                            max(0, min(candidate["end"], s["end"]) - max(candidate["start"], s["start"]))
                            / max(1, max(candidate["end"], s["end"]) - min(candidate["start"], s["start"]))
                            >= 0.5
                            for s in spans
                        )
                    )
                    # Each entity mean uses the same frozen sentence forward pass.
                    vector = vectors[[0, index + 1]].reshape(-1)
                    yield {str(k): float(v) for k, v in enumerate(vector)}, label
        elif kind == "evidence":
            for comparison in doc.get("comparisons", []):
                for sentence in sentences:
                    label = int(
                        any(
                            q["start"] < sentence["end"] and sentence["start"] < q["end"]
                            for q in comparison["evidence"]
                        )
                    )
                    yield inference_features(doc["text"], sentence, comparison, encoder), label
    dimension = encoder.manifest["dimension"]
    columns = [str(k) for k in range(2 * dimension)] if kind == "outcome" else (
        ["context_" + str(k) for k in range(dimension)] +
        [field + suffix for field in ("intervention_description", "comparator_description",
                                      "outcome_description")
         for suffix in ("_similarity", "_coverage", "_missing")]
    )
    identity = {"kind": kind, "features": source_hashes("clinical"), "encoder": encoder.manifest,
                "training_sha256": digest(Path(data).read_bytes()),
                "audit_sha256": digest(Path(audit_path).read_bytes()),
                "upstream_clinical_audit": upstream_audit}
    matrix, labels, pmids, checkpoints = numeric_examples(
        documents, columns, Path(cache) / "clinical-training" / digest(dumps(identity))[:24], identity, generate
    )
    if len(set(pmids)) < 5:
        raise ValueError("At least five independent training articles are required")
    if set(labels.tolist()) != {0, 1}:
        raise ValueError("Clinical learning requires both annotated positives and negative candidates")
    # Release the encoder before fitting dense matrices; retain its fixed manifest.
    encoder_manifest = encoder.manifest
    del generate
    encoder = clinical = parser = None
    import gc
    import numpy as np

    gc.collect()
    assignments = folds(pmids)
    fold_ids = np.asarray([assignments[pmid] for pmid in pmids], dtype=np.int8)
    configurations = []
    for c in C_GRID:
        oof_labels, scores = [], []
        for fold in range(5):
            fitting, held = fold_ids != fold, fold_ids == fold
            head = DenseClinicalHead(columns).fit(matrix[fitting], labels[fitting], c)
            oof_labels.extend(labels[held].tolist())
            values = head.matrix_probabilities(matrix[held])
            scores.extend(values[:, head.classes_.index(1)].tolist() if 1 in head.classes_
                          else [0.0] * int(held.sum()))
        configurations.append({"C": c, **threshold(oof_labels, scores)})
    eligible = [r for r in configurations if r["target_met"]]
    selected = max(
        eligible or configurations,
        key=lambda r: ((r["recall"] or 0) if eligible else (r["f1"] or 0), r["f1"] or 0, -r["C"]),
    )
    head = DenseClinicalHead(columns).fit(matrix, labels, selected["C"])
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    model = output / (kind + ".joblib")
    if model.exists():
        raise ValueError("Clinical model artifact output must be new")
    joblib.dump(head, model)
    manifest = {
        "schema": "clinical-head-v6-2",
        "kind": kind,
        "feature_sha256": FEATURE_SHA,
        "feature_source_hashes": source_hashes("clinical"),
        "encoder": encoder_manifest,
        "training_split": "train",
        "test_used_for_selection": False,
        "training_pmids": sorted(assignments),
        "fold_assignments": assignments,
        "selection": selected,
        "configurations": configurations,
        "numeric_features": {"dtype": "float32", "columns": columns, "rows": len(pmids),
                             "paper_checkpoints": checkpoints,
                             "identity_sha256": digest(dumps(identity))},
        "files": {model.name: digest(model.read_bytes())},
        "audit_sha256": digest(Path(audit_path).read_bytes()),
        "upstream_clinical_audit": upstream_audit,
        "training_sha256": digest(Path(data).read_bytes()),
        "candidate_label_policy": "outcome interval IoU >= .5"
        if kind == "outcome"
        else "annotated evidence overlaps sentence",
        "prompt_descriptions_are_span_gold": False,
        "created_at": now(),
    }
    _write_immutable(output / "manifest.json", manifest)
    return manifest
