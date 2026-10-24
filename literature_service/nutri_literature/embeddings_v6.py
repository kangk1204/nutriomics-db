"""Pinned frozen BiomedBERT, CPU-only, complete Unicode coverage, private cache."""

import json
from pathlib import Path

from .candidates_v4 import directory_hash
from .db import digest, dumps
from .nlp_v2 import cpu_inference_lock

VECTOR_SPEC = {
    "schema": "frozen-context-v6-1",
    "layer": "last_hidden_state",
    "pooling": "sentence/entity-means",
    "chunk_size": 256,
    "overlap": 32,
    "weights_updated": False,
    "device": "cpu",
}


class FrozenEncoder:
    def __init__(self, model_path, cache):
        self.path, self.cache = Path(model_path), Path(cache)
        self.model = self.tokenizer = None
        self.ner_model = None
        self.manifest = {"available": False, **VECTOR_SPEC}

    def load(self):
        if self.model is not None:
            return
        import torch
        from transformers import AutoModelForTokenClassification, AutoTokenizer

        checkpoint = self.path / "model.safetensors"
        if (
            not checkpoint.exists()
            or digest(checkpoint.read_bytes())
            != "16b4c517cc3adaa654e2f7cfc2315939c5e93f5d7ad2d5b8d3fa3dc48e456971"
        ):
            raise ValueError("Frozen BC2GM checkpoint missing or changed")
        torch.set_num_threads(1)
        torch.set_num_interop_threads(1) if torch.get_num_interop_threads() != 1 else None
        tokenizer = AutoTokenizer.from_pretrained(self.path, local_files_only=True, use_fast=True)
        if not tokenizer.is_fast:
            raise ValueError("Frozen encoder requires original Unicode offsets")
        loaded = AutoModelForTokenClassification.from_pretrained(self.path, local_files_only=True)
        loaded = loaded.to("cpu").eval()
        model = loaded.base_model
        for parameter in loaded.parameters():
            parameter.requires_grad_(False)
        self.model, self.tokenizer = model, tokenizer
        self.ner_model = loaded
        self.manifest = {
            "available": True,
            **VECTOR_SPEC,
            "model_sha256": directory_hash(self.path),
            "tokenizer_sha256": digest(tokenizer.backend_tokenizer.to_str()),
            "vector_spec_sha256": digest(dumps(VECTOR_SPEC)),
            "dimension": model.config.hidden_size,
        }
        self.cache.mkdir(parents=True, exist_ok=True)

    def encode(self, text, sentence, entities=()):
        import numpy as np

        self.load()
        for entity in entities:
            if text[entity["start"] : entity["end"]] != entity["text"]:
                raise ValueError("Embedding entity/source offset mismatch")
        if text[sentence["start"] : sentence["end"]] != sentence["text"]:
            raise ValueError("Embedding sentence/source mismatch")
        key = digest(
            dumps(
                {
                    "source": digest(text),
                    "sentence": [sentence["start"], sentence["end"]],
                    "entities": [(e["start"], e["end"]) for e in entities],
                    "encoder": self.manifest,
                }
            )
        )
        path, receipt = self.cache / (key + ".npy"), self.cache / (key + ".json")
        # Serialize writes for this key, separately from the global NLP slot.
        # This also prevents a second process from recomputing a cache miss.
        with cpu_inference_lock(self.cache / (key + ".lock")):
            if path.exists() and receipt.exists():
                record = json.loads(receipt.read_bytes())
                if record.get("key") != key or digest(path.read_bytes()) != record.get("sha256"):
                    raise ValueError("Embedding cache integrity failure")
                vector = np.load(path, allow_pickle=False)
                if list(vector.shape) != record.get("shape") or vector.shape != (
                    len(entities) + 1,
                    self.manifest["dimension"],
                ):
                    raise ValueError("Embedding cache shape differs")
                return vector
            vector = self._compute(sentence, entities)
            temporary = self.cache / (key + ".tmp.npy")
            np.save(temporary, vector, allow_pickle=False)
            temporary.replace(path)
            temporary_receipt = receipt.with_suffix(".tmp")
            temporary_receipt.write_text(
                dumps(
                    {
                        "key": key,
                        "sha256": digest(path.read_bytes()),
                        "shape": list(vector.shape),
                        "manifest": self.manifest,
                    }
                ),
                encoding="utf-8",
            )
            temporary_receipt.replace(receipt)
            return vector

    def _compute(self, sentence, entities):
        import numpy as np
        import torch

        encoded = self.tokenizer(sentence["text"], add_special_tokens=False, return_offsets_mapping=True)
        ids, offsets = encoded["input_ids"], encoded["offset_mapping"]
        slots = [None, *entities]
        sums = np.zeros((len(slots), self.manifest["dimension"]), dtype=np.float64)
        counts = np.zeros(len(slots), dtype=np.int64)
        seen = set()
        with cpu_inference_lock(), torch.inference_mode():
            for begin in range(0, len(ids), 222):
                end = min(begin + 254, len(ids))
                prepared = self.tokenizer.prepare_for_model(
                    ids[begin:end], return_tensors="pt", return_attention_mask=True
                )
                prepared.pop("token_type_ids", None)
                prepared = {
                    name: value.unsqueeze(0) if getattr(value, "ndim", 0) == 1 else value
                    for name, value in prepared.items()
                }
                hidden = self.model(**prepared).last_hidden_state[0, 1 : 1 + end - begin].cpu().numpy()
                for j, vector in enumerate(hidden):
                    i = begin + j
                    if i in seen or offsets[i][0] == offsets[i][1]:
                        continue
                    seen.add(i)
                    lo, hi = offsets[i][0] + sentence["start"], offsets[i][1] + sentence["start"]
                    sums[0] += vector
                    counts[0] += 1
                    for k, entity in enumerate(entities, 1):
                        if lo < entity["end"] and entity["start"] < hi:
                            sums[k] += vector
                            counts[k] += 1
                if end == len(ids):
                    break
        if not counts[0] or any(counts[k] == 0 for k in range(1, len(slots))):
            raise ValueError("Frozen encoder lost a sentence/entity token")
        return (sums / counts[:, None]).astype(np.float32)


def pooled_pair_features(pairs, text, encoder):
    """Mean/max over witnesses; document labels are never copied to sentences."""
    import numpy as np
    from .features_v5 import pool_features

    rows = [encoder.encode(text, p["sentence"], (p["chemical"], p["gene"])).reshape(-1) for p in pairs]
    if not rows:
        raise ValueError("A relation bag requires evidence")
    matrix = np.asarray(rows)
    features = pool_features([p["features"] for p in pairs])
    features.update({"embedding_mean_" + str(i): float(v) for i, v in enumerate(matrix.mean(axis=0))})
    features.update({"embedding_max_" + str(i): float(v) for i, v in enumerate(matrix.max(axis=0))})
    return features


def span_context_features(mention, text, sentence, encoder):
    from .features_v5 import span_features

    features = span_features(mention, text)
    vector = encoder.encode(text, sentence, (mention,)).reshape(-1)
    features.update({"frozen_span_" + str(i): float(value) for i, value in enumerate(vector)})
    return features
