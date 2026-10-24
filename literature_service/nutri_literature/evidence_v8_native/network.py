"""Task-specific native heads; no BioRED labels or causal direction mapping."""
from __future__ import annotations

import torch
from torch import nn

from .common import MODEL, REVISION, home

MARKERS = ["[A1]", "[/A1]", "[A2]", "[/A2]"]


def tokenizer():
    from transformers import AutoTokenizer
    value = AutoTokenizer.from_pretrained(MODEL, revision=REVISION, cache_dir=home()/"cache/huggingface", use_fast=True)
    value.add_special_tokens({"additional_special_tokens": MARKERS})
    return value


def bounded(ids, tok, budget=510):
    markers = tok.convert_tokens_to_ids(MARKERS)
    if any(ids.count(token) != 1 for token in markers):
        raise ValueError("토큰화에서 native 참여자 표식 손실/중복")
    positions = [ids.index(token) for token in markers]
    if len(ids) <= budget:
        return ids
    a, b = min(positions), max(positions)+1
    if b-a <= budget:
        start = max(0, min(a-(budget-(b-a))//2, len(ids)-budget))
        return ids[start:start+budget]
    ranges = sorted([(positions[0], positions[1]+1), (positions[2], positions[3]+1)])
    minimum = sum(end-start for start, end in ranges)
    if minimum+1 > budget:
        raise ValueError("native 참여자 자체가 지원 토큰 창을 초과")
    margin = (budget-minimum-1)//2
    chunks = []
    for start, end in ranges:
        left = min(start, margin//2)
        right = min(len(ids)-end, margin-left)
        chunks.append(ids[start-left:end+right])
    return chunks[0]+[tok.sep_token_id]+chunks[1]


def encode(tok, row):
    text = row["text"]
    for source, target in zip(("[C]", "[/C]", "[G]", "[/G]"), MARKERS):
        text = text.replace(source, target)
    ids = bounded(tok(text, add_special_tokens=False)["input_ids"], tok,
                  512-tok.num_special_tokens_to_add())
    value = dict(tok.prepare_for_model(ids, add_special_tokens=True, return_attention_mask=True, return_token_type_ids=True))
    for name, start, end in (("a_mask", "[A1]", "[/A1]"), ("b_mask", "[A2]", "[/A2]")):
        left, right = tok.convert_tokens_to_ids([start, end])
        a, b = value["input_ids"].index(left), value["input_ids"].index(right)
        if a >= b-1:
            raise ValueError("빈 native 참여자 토큰 구절")
        value[name] = [int(a < i < b) for i in range(len(value["input_ids"]))]
    return value


def batch(tok, rows, device):
    plain = [{k: v for k, v in r.items() if k not in ("a_mask", "b_mask")} for r in rows]
    value = dict(tok.pad(plain, padding=True, return_tensors="pt"))
    length = value["input_ids"].shape[1]
    for name in ("a_mask", "b_mask"):
        value[name] = torch.tensor([r[name]+[0.]*(length-len(r[name])) for r in rows], dtype=torch.float32)
    return {k: v.to(device) for k, v in value.items()}


class NativeNetwork(nn.Module):
    def __init__(self, labels, vocab_size, frozen=False):
        super().__init__()
        from transformers import AutoModel
        self.encoder = AutoModel.from_pretrained(MODEL, revision=REVISION, cache_dir=home()/"cache/huggingface",
                                                  attn_implementation="sdpa")
        self.encoder.resize_token_embeddings(vocab_size, mean_resizing=False)
        self.encoder.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        if frozen:
            self.encoder.requires_grad_(False)
        size = self.encoder.config.hidden_size
        self.dropout = nn.Dropout(.1)
        self.types = nn.Linear(3*size, labels)
        self.exists = nn.Linear(3*size, 1)
        self.register_buffer("supported", torch.ones(labels))
        self.register_buffer("positive_weights", torch.ones(labels))

    def forward(self, input_ids, attention_mask, a_mask, b_mask, token_type_ids=None, features_only=False):
        inputs = {"input_ids": input_ids, "attention_mask": attention_mask}
        if token_type_ids is not None:
            inputs["token_type_ids"] = token_type_ids
        hidden = self.encoder(**inputs).last_hidden_state
        def pool(mask):
            return (hidden*mask.unsqueeze(-1)).sum(1)/mask.sum(1, keepdim=True).clamp_min(1)
        vector = torch.cat((hidden[:, 0], pool(a_mask), pool(b_mask)), -1)
        if features_only:
            return vector
        vector = self.dropout(vector)
        return {"types": self.types(vector), "exists": self.exists(vector).squeeze(-1)}


def native_loss(output, targets, task, supported, positive_weights):
    targets = torch.as_tensor(targets, device=output["types"].device, dtype=torch.float32)
    mask = supported.float()
    per = torch.nn.functional.binary_cross_entropy_with_logits(output["types"].float(), targets,
        reduction="none", pos_weight=positive_weights)
    value = (per*mask).sum()/mask.sum().clamp_min(1)/len(targets)
    # CG candidates are supplied annotated event-role links; no invented negatives.
    if task == "drugprot":
        exists = (targets.sum(-1) > 0).float()
        value = value+torch.nn.functional.binary_cross_entropy_with_logits(output["exists"].float(), exists)
    return value
