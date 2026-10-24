"""Equivalent frozen vectors: compute each sentence once, pool any mention.

No representation, checkpoint, tokenizer, chunking or summation order changes.
The wrapper is checked against the original encoder before a resumed batch.
"""
from collections import OrderedDict
from pathlib import Path

from .db import digest
from .nlp_v2 import cpu_inference_lock


def source_sha():
    return digest(Path(__file__).read_bytes())


class SentenceMemoEncoder:
    def __init__(self, encoder, capacity=64):
        self.base, self.capacity, self.memo = encoder, capacity, OrderedDict()

    def __getattr__(self, name):
        return getattr(self.base, name)

    def encode(self, text, sentence, entities=()):
        import numpy as np
        import torch
        self.base.load()
        if text[sentence["start"]:sentence["end"]] != sentence["text"]:
            raise ValueError("Frozen sentence/source mismatch")
        for e in entities:
            if text[e["start"]:e["end"]] != e["text"]:
                raise ValueError("Frozen entity/source mismatch")
        key = (digest(text), sentence["start"], sentence["end"])
        if key not in self.memo:
            encoded = self.base.tokenizer(sentence["text"], add_special_tokens=False, return_offsets_mapping=True)
            ids, offsets = encoded["input_ids"], encoded["offset_mapping"]
            hidden_rows, seen = [], set()
            with cpu_inference_lock(), torch.inference_mode():
                for begin in range(0, len(ids), 222):
                    end = min(begin + 254, len(ids))
                    prepared = self.base.tokenizer.prepare_for_model(ids[begin:end], return_tensors="pt", return_attention_mask=True)
                    prepared.pop("token_type_ids", None)
                    prepared = {k: v.unsqueeze(0) if getattr(v, "ndim", 0) == 1 else v for k, v in prepared.items()}
                    hidden = self.base.model(**prepared).last_hidden_state[0, 1:1 + end - begin].cpu().numpy()
                    for j, vector in enumerate(hidden):
                        i = begin + j
                        if i in seen or offsets[i][0] == offsets[i][1]:
                            continue
                        seen.add(i)
                        lo, hi = offsets[i][0] + sentence["start"], offsets[i][1] + sentence["start"]
                        hidden_rows.append((lo, hi, vector.copy()))
                    if end == len(ids):
                        break
            self.memo[key] = hidden_rows
            while len(self.memo) > self.capacity:
                self.memo.popitem(last=False)
        self.memo.move_to_end(key)
        sums = np.zeros((len(entities) + 1, self.base.manifest["dimension"]), dtype=np.float64)
        counts = np.zeros(len(entities) + 1, dtype=np.int64)
        for lo, hi, vector in self.memo[key]:
            sums[0] += vector
            counts[0] += 1
            for k, e in enumerate(entities, 1):
                if lo < e["end"] and e["start"] < hi:
                    sums[k] += vector
                    counts[k] += 1
        if not all(counts):
            raise ValueError("Frozen encoder lost a token")
        return (sums / counts[:, None]).astype(np.float32)
