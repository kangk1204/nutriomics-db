"""Non-generative, task-separated models. Imports are lazy outside GPU tasks."""
from __future__ import annotations

import math

import torch
from torch import nn

from .common import MODEL, REVISION, TYPES, home, read

SPECIAL = ["[C]", "[/C]", "[G]", "[/G]", "[MENTION]", "[/MENTION]"]


def tokenizer():
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(MODEL, revision=REVISION, use_fast=True, cache_dir=home()/"cache/huggingface")
    tok.add_special_tokens({"additional_special_tokens": SPECIAL})
    return tok


class Network(nn.Module):
    def __init__(self, task, tokenizer_size, encoder_path=None, label_count=None):
        super().__init__()
        from transformers import AutoModel
        self.task = task
        self.encoder = AutoModel.from_pretrained(encoder_path or MODEL,
            **({} if encoder_path else {"revision": REVISION}),
            cache_dir=home()/"cache/huggingface", attn_implementation="sdpa")
        self.encoder.resize_token_embeddings(tokenizer_size, mean_resizing=False)
        self.encoder.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        h = self.encoder.config.hidden_size
        self.dropout = nn.Dropout(.1)
        if task == "id":
            self.rank = nn.Linear(h, 1)
            self.validity = nn.Linear(h, 1)
        elif task == "relation":
            self.hidden = nn.Sequential(nn.Linear(3*h, h), nn.GELU(), nn.Dropout(.1))
            self.exists = nn.Linear(h, 1)
            self.types = nn.Linear(h, len(TYPES))
            self.direction = nn.Linear(h, 3)
            self.evidence = nn.Linear(h, 1)
        elif task == "clinical":
            if not label_count:
                raise ValueError("임상 레이블 목록 필요")
            self.labels = nn.Linear(h, label_count)
        elif task == "ner":
            self.labels = nn.Linear(h, 6)
        else:
            raise ValueError(task)

    def forward(self, input_ids, attention_mask, token_type_ids=None, c_mask=None, g_mask=None):
        args = {"input_ids": input_ids, "attention_mask": attention_mask}
        if token_type_ids is not None:
            args["token_type_ids"] = token_type_ids
        h = self.encoder(**args).last_hidden_state
        if self.task == "id":
            v = self.dropout(h[:, 0])
            return {"rank": self.rank(v).squeeze(-1), "validity": self.validity(v).squeeze(-1)}
        if self.task == "clinical":
            return self.labels(self.dropout(h))
        if self.task == "ner":
            return self.labels(self.dropout(h)).reshape(*h.shape[:2], 2, 3)
        def mean(mask):
            return (h*mask.unsqueeze(-1)).sum(1)/mask.sum(1, keepdim=True).clamp_min(1)
        v = self.hidden(torch.cat((h[:, 0], mean(c_mask), mean(g_mask)), dim=-1))
        return {"exists": self.exists(v).squeeze(-1), "types": self.types(v),
                "direction": self.direction(v), "evidence": self.evidence(v).squeeze(-1)}


def listwise_loss(logits, target, rank_weight, valid_logits, valid_target, valid_weight):
    """No independent sigmoid ID ranking; NIL competes with the same 50 candidates."""
    target = torch.as_tensor(target, device=logits.device, dtype=torch.float32)
    rank = -(target*logits.float().log_softmax(0)).sum()*float(rank_weight)
    valid = torch.nn.functional.binary_cross_entropy_with_logits(
        valid_logits[0].float(), torch.as_tensor(valid_target, device=logits.device, dtype=torch.float32))
    return rank + .2*valid*float(valid_weight)


def relation_loss(output, row):
    """Document labels supervise the bag; no copying positives onto every view."""
    exists = torch.logsumexp(output["exists"].float(), 0)-math.log(len(output["exists"]))
    types = torch.logsumexp(output["types"].float(), 0)-math.log(len(output["types"]))
    weight = float(row.get("training_weight", 1.))
    exists_target = torch.as_tensor(row["exists"], device=exists.device, dtype=torch.float32)
    value = torch.nn.functional.binary_cross_entropy_with_logits(exists, exists_target)
    if row["exists"]:
        targets = torch.tensor([float(t in row["semantic_types"]) for t in TYPES], device=types.device)
        value = value + torch.nn.functional.binary_cross_entropy_with_logits(types, targets)
    if row.get("direction_target", -100) != -100:
        value = value + .2*torch.nn.functional.cross_entropy(output["direction"].float().mean(0).unsqueeze(0),
            torch.tensor([row["direction_target"]], device=types.device))
    # Direct evidence is supervised only where that dataset provides an annotation.
    if row.get("evidence_targets") is not None:
        targets = torch.tensor(row["evidence_targets"], device=types.device)
        masks = torch.tensor(row.get("evidence_mask", [1.]*len(targets)), device=types.device)
        if masks.sum():
            value = value + .2*(torch.nn.functional.binary_cross_entropy_with_logits(
                output["evidence"].float(), targets, reduction="none")*masks).sum()/masks.sum()
    return value*weight


def encode_id(tok, row, kb):
    from .retrieval import candidate_description
    m, text = row["mention"], row["text"]
    left, right = max(0, m["start"]-220), min(len(text), m["end"]+220)
    query = text[left:m["start"]]+"[MENTION]"+m["text"]+"[/MENTION]"+text[m["end"]:right]
    descriptions = [candidate_description(kb, m["kind"], identifier, m["text"]) for identifier in row["menu"]]
    # Truncate context before pairing so mention markers cannot disappear silently.
    query_ids = tok(query, add_special_tokens=False)["input_ids"]
    marker = tok.convert_tokens_to_ids("[MENTION]")
    end_marker = tok.convert_tokens_to_ids("[/MENTION]")
    if marker not in query_ids or end_marker not in query_ids:
        raise ValueError("ID 언급 표식 인코딩 실패")
    a, b = query_ids.index(marker), query_ids.index(end_marker)+1
    if b-a > 128:
        raise ValueError("언급 자체가 ID 토큰 창을 초과")
    start = max(0, min(a-(128-(b-a))//2, len(query_ids)-128))
    query_ids = query_ids[start:start+128]
    values = []
    for description in descriptions:
        second = tok(description, add_special_tokens=False)["input_ids"][:120]
        values.append(dict(tok.prepare_for_model(query_ids, pair_ids=second, add_special_tokens=True,
            return_attention_mask=True, return_token_type_ids=True)))
    return values


def encode_relation(tok, row):
    # This pure adapter receives an explicit tokenizer, no old HOME/cache/model state.
    from ..repair_v6_gpu_r2.neural import bounded_ids, marker_masks
    result = []
    for view in row["views"]:
        ids = tok(view["text"], add_special_tokens=False)["input_ids"]
        ids = bounded_ids(ids, tok, 512-tok.num_special_tokens_to_add())
        value = dict(tok.prepare_for_model(ids, add_special_tokens=True, return_attention_mask=True,
                                           return_token_type_ids=True))
        value.update(marker_masks(value["input_ids"], tok))
        result.append(value)
    return result


def batch_inputs(tok, rows, device):
    # Padding c/g masks separately avoids tokenizer treating floats as token IDs.
    basic = [{k: v for k, v in row.items() if k not in ("c_mask", "g_mask")} for row in rows]
    result = dict(tok.pad(basic, padding=True, return_tensors="pt"))
    length = result["input_ids"].shape[1]
    for key in ("c_mask", "g_mask"):
        if key in rows[0]:
            result[key] = torch.tensor([row[key]+[0.]*(length-len(row[key])) for row in rows], dtype=torch.float32)
    return {k: v.to(device) for k, v in result.items()}


def forward_chunks(model, tok, encodings, device, chunk=2):
    values = [model(**batch_inputs(tok, encodings[i:i+chunk], device)) for i in range(0, len(encodings), chunk)]
    if isinstance(values[0], dict):
        return {key: torch.cat([v[key] for v in values], 0) for key in values[0]}
    return torch.cat(values, 0)
