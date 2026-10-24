"""Actual listwise cross-encoder and event-bag training with resumable epochs."""
from __future__ import annotations

import copy
import gc
import math
import random
from collections import defaultdict
from pathlib import Path

from .common import (POLICY, TYPES, Ledger, atomic, canonical, canonical_digest, checkpoint, digest,
                     filehash, gpu_setup, home, read, seed_all, sources, wilson)


def features(row, kb):
    """No PMID, ID number or relation score is a learned feature."""
    from rapidfuzz.fuzz import WRatio
    from ..repair_v6_gpu_r2.knowledge import namekey
    m = row["mention"]
    lex = {v: i for i, v in enumerate(m.get("lexical_ids", []))}
    sap = {v: i for i, v in enumerate(m.get("sapbert_ids", []))}
    taxa = m.get("supported_taxa", [])
    result = []
    for identifier in row["menu"]:
        record = kb.by_id(m["kind"], identifier) if identifier else None
        names = record["names"] if record else []
        match = max((WRatio(namekey(m["text"]), namekey(n))/100 for n in names), default=0.)
        exact_name = float(any(namekey(m["text"]) == namekey(n) for n in names))
        tax_match = float(bool(record and len(taxa) == 1 and record.get("taxon") == taxa[0]))
        tax_conflict = float(bool(record and taxa and record.get("taxon") and record["taxon"] not in taxa))
        result.append([float(identifier is None), match, exact_name,
            1/(1+lex[identifier]) if identifier in lex else 0.,
            1/(1+sap[identifier]) if identifier in sap else 0., tax_match, tax_conflict,
            float(identifier == m.get("previous_id") and identifier is not None),
            float(m.get("ner_score", 0.)), float(bool(m.get("cell_evidence")))])
    return result


def fit_linear(rows, directory, C):
    import numpy as np
    from scipy.optimize import minimize
    from sklearn.linear_model import LogisticRegression
    from .retrieval import knowledge
    path = Path(directory)/"model.json"
    contract = canonical_digest([rows, C, sources()])
    if path.exists():
        if read(path)["input_sha256"] != contract:
            raise ValueError("선형 모델 입력 계약 변경")
        return path.parent
    kb = knowledge()
    try:
        xs = [np.asarray(features(r, kb), dtype=np.float64) for r in rows]
    finally:
        kb.close()
    offsets = np.cumsum([0]+[len(x) for x in xs])
    x = np.concatenate(xs)
    y = np.concatenate([r["targets"] for r in rows])
    weights = np.asarray([r["rank_weight"] for r in rows])
    total = max(1., weights.sum())
    if not weights.sum():
        raise ValueError("ID 순위화 지도 신호 없음")
    def objective(w):
        score = x@w
        maximum = np.maximum.reduceat(score, offsets[:-1])
        size = np.diff(offsets)
        exp = np.exp(score-np.repeat(maximum, size))
        sums = np.add.reduceat(exp, offsets[:-1])
        p = exp/np.repeat(sums, size)
        loss = weights@(maximum+np.log(sums)-np.add.reduceat(score*y, offsets[:-1]))/total + (w@w)/(2*C*total)
        grad = x.T@((p-y)*np.repeat(weights, size))/total + w/(C*total)
        return float(loss), grad
    fit = minimize(objective, np.zeros(x.shape[1]), jac=True, method="L-BFGS-B", options={"maxiter": 400})
    if not np.isfinite(fit.fun) or not np.isfinite(fit.x).all():
        raise FloatingPointError("선형 ID 순위 손실 NaN/Inf")
    if not fit.success and fit.status != 1:
        raise RuntimeError("선형 ID 순위 최적화 실패: "+str(fit.message))
    valid_x = x[offsets[:-1]]
    valid_y = np.asarray([r["valid_target"] for r in rows])
    valid_weights = np.asarray([r["valid_weight"] for r in rows])
    mask = valid_weights > 0
    if not mask.any():
        raise ValueError("개체 유효성 지도 신호 없음")
    if len(set(valid_y[mask])) < 2:
        validity = {"constant": float(valid_y[mask][0])}
    else:
        model = LogisticRegression(C=C, max_iter=500).fit(valid_x[mask], valid_y[mask], sample_weight=valid_weights[mask])
        validity = {"coef": model.coef_[0].tolist(), "intercept": float(model.intercept_[0])}
    atomic(path, {"weights": fit.x.tolist(), "validity": validity, "C": C,
                  "input_sha256": contract, "family": "linear-listwise", "objective": float(fit.fun),
                  "optimization_converged": bool(fit.success), "optimization_message": str(fit.message)}, True)
    return path.parent


def linear_scores(rows, directory):
    import numpy as np
    from scipy.special import expit, softmax
    from .retrieval import knowledge
    model = read(Path(directory)/"model.json")
    kb = knowledge()
    result = []
    try:
        for row in rows:
            x = np.asarray(features(row, kb))
            probabilities = softmax(x@np.asarray(model["weights"]))
            v = model["validity"]
            valid = v["constant"] if "constant" in v else float(expit(x[0]@np.asarray(v["coef"])+v["intercept"]))
            result.append({"probabilities": probabilities.tolist(), "validity": valid})
    finally:
        kb.close()
    return result


def model_contract(task, train_rows, validation_rows, config, seed):
    from .dictionary_mentions import asset_contract
    assets, _ = asset_contract()
    return canonical_digest({"task": task, "train": train_rows, "validation": validation_rows,
        "config": config, "seed": seed, "code": sources(), "models": read(home()/"models.json"),
        "dictionary_assets": assets})


def fit_neural(task, train_rows, validation_rows, directory, config, seed, encoder_path=None):
    import torch
    from safetensors.torch import load_file, save_file
    from .networks import (Network, encode_id, encode_relation, forward_chunks,
                           listwise_loss, relation_loss, tokenizer)
    from .retrieval import knowledge
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    expected = model_contract(task, train_rows, validation_rows, config, seed)
    done = directory/"complete.json"
    if done.exists():
        value = read(done)
        if value["contract"] != expected or filehash(directory/"model.safetensors") != value["weights_sha256"]:
            raise ValueError("학습 완료 파일/입력 계약 불일치")
        return directory
    seed_all(seed)
    device, gpu = gpu_setup()
    ledger = Ledger()
    ledger.start(task+"-train")
    tok = tokenizer()
    model = Network(task, len(tok), encoder_path=encoder_path).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=config["learning_rate"], weight_decay=.01)
    kb = knowledge() if task == "id" else None
    state_path = directory/"checkpoint.pt"
    epoch_start, best, stale, history = 0, float("inf"), 0, []
    if state_path.exists():
        state = torch.load(state_path, map_location=device, weights_only=True)
        if state["contract"] != expected:
            raise ValueError("학습 재개 입력/모델 계약 변경")
        model.load_state_dict(state["model"])
        optimizer.load_state_dict(state["optimizer"])
        epoch_start, best, stale, history = state["epoch"], state["best"], state["stale"], state["history"]

    def row_loss(row):
        if task == "ner":
            from .networks import batch_inputs
            encoding = {k: row[k] for k in ("input_ids", "attention_mask", "token_type_ids") if k in row}
            outputs = model(**batch_inputs(tok, [encoding], device))
            return torch.nn.functional.cross_entropy(outputs.float().reshape(-1, 3),
                torch.tensor(row["labels"], device=device).reshape(-1), ignore_index=-100)
        encodings = encode_id(tok, row, kb) if task == "id" else encode_relation(tok, row)
        output = forward_chunks(model, tok, encodings, device)
        if task == "id":
            return listwise_loss(output["rank"], row["targets"], row["rank_weight"],
                output["validity"], row["valid_target"], row["valid_weight"])
        return relation_loss(output, row)

    try:
        if not train_rows:
            raise ValueError("학습 행 0건")
        for epoch in range(epoch_start, config.get("epochs", 5)):
            checkpoint()
            model.train()
            # An epoch is reproducible independently, including a resumed checkpoint.
            seed_all(seed+epoch)
            order = list(range(len(train_rows)))
            random.shuffle(order)
            total, step = 0., 0
            gradient_norms = []
            optimizer.zero_grad(set_to_none=True)
            for at, index in enumerate(order):
                loss = row_loss(train_rows[index])
                if not torch.isfinite(loss):
                    raise FloatingPointError("학습 손실 NaN/Inf")
                total += float(loss.detach())
                (loss/config.get("accumulation", 16)).backward()
                step += 1
                if step % config.get("accumulation", 16) == 0 or at+1 == len(order):
                    norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 1.)
                    if not torch.isfinite(norm):
                        raise FloatingPointError("기울기 NaN/Inf")
                    gradient_norms.append(float(norm))
                    optimizer.step()
                    optimizer.zero_grad(set_to_none=True)
                ledger.check()
            model.eval()
            with torch.inference_mode():
                validation = sum(float(row_loss(r)) for r in validation_rows)/len(validation_rows) if validation_rows else total/len(train_rows)
            if not math.isfinite(validation):
                raise FloatingPointError("검증 손실 NaN/Inf")
            improved = validation < best-1e-6
            if improved:
                best, stale = validation, 0
                save_file({k: v.detach().contiguous().cpu() for k, v in model.state_dict().items()}, directory/"model.safetensors")
            else:
                stale += 1
            history.append({"epoch": epoch+1, "train_loss": total/len(train_rows), "validation_loss": validation,
                            "peak_cuda_bytes": torch.cuda.max_memory_allocated(),
                            "gradient_norm_max": max(gradient_norms, default=0.),
                            "optimizer_updated": any(v > 0 for v in gradient_norms)})
            temporary = directory/"checkpoint.part.pt"
            torch.save({"contract": expected, "model": model.state_dict(), "optimizer": optimizer.state_dict(),
                "epoch": epoch+1, "best": best, "stale": stale, "history": history}, temporary)
            temporary.replace(state_path)
            atomic(directory/"progress.json", {"history": history, "contract": expected})
            if validation_rows and stale >= config.get("patience", 2):
                break
        atomic(done, {"contract": expected, "task": task, "config": config, "seed": seed, "history": history,
                      "weights_sha256": filehash(directory/"model.safetensors"), "gpu_uuid": gpu["uuid"],
                      "validation_is_training_loss": not bool(validation_rows)}, True)
        return directory
    finally:
        ledger.stop()
        if kb:
            kb.close()
        del model, optimizer
        gc.collect()
        torch.cuda.empty_cache()


def neural_scores(task, rows, directory):
    import torch
    from safetensors.torch import load_file
    from .networks import Network, encode_id, encode_relation, forward_chunks, tokenizer
    from .retrieval import knowledge
    tok = tokenizer()
    device, gpu = gpu_setup()
    model = Network(task, len(tok)).to(device)
    model.load_state_dict(load_file(str(Path(directory)/"model.safetensors"), device="cpu"))
    model.eval()
    kb = knowledge() if task == "id" else None
    ledger = Ledger()
    ledger.start(task+"-score")
    result = []
    try:
        with torch.inference_mode():
            for row in rows:
                encodings = encode_id(tok, row, kb) if task == "id" else encode_relation(tok, row)
                out = forward_chunks(model, tok, encodings, device)
                if task == "id":
                    result.append({"probabilities": out["rank"].float().softmax(0).cpu().tolist(),
                                   "validity": float(out["validity"][0].sigmoid())})
                else:
                    result.append({"exists": float((torch.logsumexp(out["exists"].float(), 0)-math.log(len(out["exists"]))).sigmoid()),
                        "types": (torch.logsumexp(out["types"].float(), 0)-math.log(len(out["types"]))).sigmoid().cpu().tolist(),
                        "direction": out["direction"].float().mean(0).softmax(0).cpu().tolist(),
                        "evidence": out["evidence"].sigmoid().cpu().tolist(), "direct_evidence_confirmed": False})
                ledger.check()
        return result
    finally:
        ledger.stop()
        if kb:
            kb.close()
        del model
        gc.collect()
        torch.cuda.empty_cache()


def species_compatible(row, index):
    if row["mention"]["kind"] != "gene":
        return True
    taxa = row["mention"].get("supported_taxa", [])
    menu_taxa = row.get("menu_taxa", [])
    return (len(taxa) == 1 and taxa[0] in POLICY["supported_taxa"] and index < len(menu_taxa)
            and menu_taxa[index] == taxa[0])


def id_policy(rows, scores, action="link"):
    """Policy is chosen only on held training folds, never development answers."""
    candidates = []
    for threshold in POLICY["thresholds"]:
        for margin in (0., .05, .1, .2):
            chosen, correct, eligible = 0, 0, 0
            for row, score in zip(rows, scores):
                if not row["valid_weight"]:
                    continue
                eligible += 1
                ranked = sorted(enumerate(score["probabilities"]), key=lambda p: (-p[1], p[0]))
                (at, value), second = ranked[0], ranked[1][1] if len(ranked)>1 else 0.
                identifier = row["menu"][at]
                eligible_action = ((identifier is not None and score["validity"] >= .5 and species_compatible(row, at))
                                   if action == "link" else (identifier is None and score["validity"] < .5))
                if value >= threshold and value-second >= margin and eligible_action:
                    if identifier != row["mention"].get("previous_id"):
                        chosen += 1
                        correct += int(identifier in row.get("gold_ids", []) or (identifier is None and not row["valid_span"]))
            lower = wilson(correct, chosen)
            candidates.append({"threshold": threshold, "margin": margin, "changed": chosen, "correct": correct,
                               "wilson_lower": lower, "eligible": eligible,
                               "enabled": chosen >= 50 and lower >= .9})
    passed = [v for v in candidates if v["enabled"]]
    selected = max(passed, key=lambda v: (v["correct"], -v["changed"]+v["correct"], v["threshold"])) if passed else max(candidates, key=lambda v: (v["wilson_lower"], v["correct"]))
    return {**selected, "enabled": bool(passed), "action": action,
            "reason": "validated_change_policy" if passed else "candidate_ranking_only"}


def apply_id(row, score, policy):
    ranked = sorted(enumerate(score["probabilities"]), key=lambda v: (-v[1], v[0]))
    at, probability = ranked[0]
    second = ranked[1][1] if len(ranked)>1 else 0.
    proposed, previous = row["menu"][at], row["mention"].get("previous_id")
    policy = policy.get("withdraw" if proposed is None else "link", policy)
    semantic_gate = (score["validity"] < .5) if proposed is None else (score["validity"] >= .5 and species_compatible(row, at))
    automatic = (policy["enabled"] and probability >= policy["threshold"]
                 and probability-second >= policy["margin"] and semantic_gate)
    selected = proposed if automatic else previous
    if selected == previous:
        reason = "retained" if previous else "unresolved"
    else:
        reason = "withdrawn" if selected is None else "changed" if previous else "new"
    return {"selected_id": selected, "previous_id": previous, "proposed_id": proposed, "reason": reason,
            "priority": probability, "validity_priority": score["validity"], "automatic_policy_enabled": policy["enabled"],
            "hypotheses": [{"id": row["menu"][i], "priority": p} for i, p in ranked[:10]]}
