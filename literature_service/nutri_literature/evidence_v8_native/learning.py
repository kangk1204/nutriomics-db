"""Finite frozen-linear / fine-tuned native comparisons, CPU/GPU isolated."""
from __future__ import annotations

import gc
import random
from pathlib import Path

from .common import (POLICY, Ledger, atomic, canonical, checkpoint, digest, filehash,
                     home, read, seed_all, setup_gpu, sources)


def labels_for(value, task):
    return [v for v in value["labels"] if v.startswith(task+":") and not v.endswith(":NONE")]


def extract_features(rows, task, labels):
    import numpy as np
    import torch
    from .network import NativeNetwork, batch, encode, tokenizer
    key = digest(canonical([task, rows, labels, sources()]))[:24]
    directory = home()/"features"/key
    receipt = directory/"manifest.json"
    if receipt.exists():
        value = read(receipt)
        if filehash(directory/"features.npy") != value["sha256"]:
            raise ValueError("고정 native 특징 변경")
        return directory
    directory.mkdir(parents=True, exist_ok=True)
    seed_all(20261023)
    tok = tokenizer()
    device, gpu = setup_gpu()
    model = NativeNetwork(len(labels), len(tok), frozen=True).to(device).eval()
    width = model.encoder.config.hidden_size*3
    matrix = np.lib.format.open_memmap(directory/"features.part.npy", mode="w+", dtype=np.float32, shape=(len(rows), width))
    ledger = Ledger(task+"-frozen-encode")
    try:
        with torch.inference_mode():
            for start in range(0, len(rows), 8):
                encodings = [encode(tok, r) for r in rows[start:start+8]]
                matrix[start:start+len(encodings)] = model(**batch(tok, encodings, device), features_only=True).float().cpu().numpy()
                ledger.check()
        matrix.flush()
        del matrix
        (directory/"features.part.npy").replace(directory/"features.npy")
        atomic(receipt, {"rows": len(rows), "width": width, "sha256": filehash(directory/"features.npy"),
                        "fixed_encoder": True, "task_specific_training": False, "labels_are_features": False,
                        "gpu_uuid": gpu["uuid"], "source_sha256": sources()}, True)
        return directory
    finally:
        ledger.stop()
        del model
        gc.collect()
        torch.cuda.empty_cache()


def linear_fit(rows, labels, features, indices, C, directory):
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    path = Path(directory)/"model.json"
    expected = digest(canonical([rows, labels, indices, C, filehash(features/"manifest.json"), sources()]))
    if path.exists():
        if read(path)["contract"] != expected:
            raise ValueError("native 선형 모델 계약 변경")
        return path.parent
    matrix = np.load(features/"features.npy", mmap_mode="r")
    x = np.asarray(matrix[indices], dtype=np.float64)
    scale = StandardScaler().fit(x)
    x = scale.transform(x)
    heads, support = [], []
    for label in labels:
        y = np.asarray([label in rows[i]["labels"] for i in indices], dtype=np.int8)
        support.append(bool(y.sum()))
        if len(set(y)) < 2:
            heads.append({"constant": float(y[0])})
        else:
            fit = LogisticRegression(C=C, max_iter=500, random_state=20261023, class_weight="balanced").fit(x, y)
            heads.append({"coef": fit.coef_[0].tolist(), "intercept": float(fit.intercept_[0])})
    y = np.asarray([any(v in labels for v in rows[i]["labels"]) for i in indices], dtype=np.int8)
    if len(set(y)) < 2:
        exists = {"constant": float(y[0])}
    else:
        fit = LogisticRegression(C=C, max_iter=500, random_state=20261023, class_weight="balanced").fit(x, y)
        exists = {"coef": fit.coef_[0].tolist(), "intercept": float(fit.intercept_[0])}
    atomic(path, {"contract": expected, "labels": labels, "support": support, "heads": heads, "exists": exists,
                  "mean": scale.mean_.tolist(), "scale": scale.scale_.tolist(), "C": C}, True)
    return path.parent


def linear_score(features, indices, model_dir):
    import numpy as np
    from scipy.special import expit
    model = read(Path(model_dir)/"model.json")
    matrix = np.load(features/"features.npy", mmap_mode="r")
    x = (np.asarray(matrix[indices], dtype=np.float64)-np.asarray(model["mean"]))/np.asarray(model["scale"])
    def predict(head):
        return np.full(len(x), head["constant"]) if "constant" in head else expit(x@np.asarray(head["coef"])+head["intercept"])
    types = np.stack([predict(head) if support else np.zeros(len(x)) for head, support in zip(model["heads"], model["support"])], -1)
    exists = predict(model["exists"])
    return [{"types": p.tolist(), "exists": float(e)} for p, e in zip(types, exists)]


def fit_neural(rows, validation, task, labels, config, directory):
    import torch
    from safetensors.torch import save_file
    from .network import NativeNetwork, batch, encode, native_loss, tokenizer
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    contract = digest(canonical([rows, validation, task, labels, config, sources()]))
    complete = directory/"complete.json"
    if complete.exists():
        result = read(complete)
        if result["contract"] != contract or filehash(directory/"model.safetensors") != result["weights_sha256"]:
            raise ValueError("native 신경망 학습 계약 변경")
        return directory
    seed_all(20261023)
    tok = tokenizer()
    device, gpu = setup_gpu()
    model = NativeNetwork(len(labels), len(tok)).to(device)
    positive_counts = torch.tensor([sum(label in r["labels"] for r in rows) for label in labels], device=device)
    model.supported.copy_((positive_counts > 0).float())
    model.positive_weights.copy_(((len(rows)-positive_counts.float())/positive_counts.clamp_min(1)).sqrt().clamp(1, 10))
    optimizer = torch.optim.AdamW(model.parameters(), lr=config["learning_rate"], weight_decay=.01)
    start, best, stale, history = 0, float("inf"), 0, []
    if (directory/"checkpoint.pt").exists():
        checkpoint_value = torch.load(directory/"checkpoint.pt", weights_only=True, map_location=device)
        if checkpoint_value["contract"] != contract:
            raise ValueError("native epoch 재개 계약 변경")
        model.load_state_dict(checkpoint_value["model"])
        optimizer.load_state_dict(checkpoint_value["optimizer"])
        start, best, stale, history = (checkpoint_value[k] for k in ("epoch", "best", "stale", "history"))
    ledger = Ledger(task+"-finetune")
    def loss(samples):
        encodings = [encode(tok, r) for r in samples]
        output = model(**batch(tok, encodings, device))
        target = [[float(label in r["labels"]) for label in labels] for r in samples]
        return native_loss(output, target, task, model.supported, model.positive_weights)
    try:
        for epoch in range(start, config.get("epochs", 5)):
            checkpoint()
            seed_all(20261023+epoch)
            model.train()
            order = list(rows)
            random.shuffle(order)
            total, steps, norms = 0., 0, []
            optimizer.zero_grad(set_to_none=True)
            for start_row in range(0, len(order), 2):
                value = loss(order[start_row:start_row+2])
                if not torch.isfinite(value):
                    raise FloatingPointError("native 손실 NaN/Inf")
                total += float(value.detach())
                (value/8).backward()
                steps += 1
                if steps % 8 == 0 or start_row+2 >= len(order):
                    norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 1.)
                    if not torch.isfinite(norm):
                        raise FloatingPointError("native 기울기 NaN/Inf")
                    norms.append(float(norm))
                    optimizer.step()
                    optimizer.zero_grad(set_to_none=True)
                ledger.check()
            model.eval()
            with torch.inference_mode():
                validation_loss = sum(float(loss(validation[i:i+2])) for i in range(0, len(validation), 2))/max(1, (len(validation)+1)//2) if validation else total/steps
            if validation_loss < best-1e-6:
                best, stale = validation_loss, 0
                save_file({k: v.detach().contiguous().cpu() for k, v in model.state_dict().items()}, directory/"model.safetensors")
            else:
                stale += 1
            history.append({"epoch": epoch+1, "training_loss": total/steps, "validation_loss": validation_loss,
                            "gradient_norm_max": max(norms, default=0.), "peak_cuda_bytes": torch.cuda.max_memory_allocated()})
            temp = directory/"checkpoint.part.pt"
            torch.save({"contract": contract, "model": model.state_dict(), "optimizer": optimizer.state_dict(),
                        "epoch": epoch+1, "best": best, "stale": stale, "history": history}, temp)
            temp.replace(directory/"checkpoint.pt")
            atomic(directory/"progress.json", {"history": history})
            if validation and stale >= 2:
                break
        atomic(complete, {"contract": contract, "config": config, "history": history, "gpu_uuid": gpu["uuid"],
                          "weights_sha256": filehash(directory/"model.safetensors"), "labels": labels,
                          "unsupported_labels": [label for label, count in zip(labels, positive_counts) if int(count) == 0]}, True)
        return directory
    finally:
        ledger.stop()
        del model, optimizer
        gc.collect()
        torch.cuda.empty_cache()


def neural_score(rows, labels, model_dir):
    import torch
    from safetensors.torch import load_file
    from .network import NativeNetwork, batch, encode, tokenizer
    tok = tokenizer()
    device, gpu = setup_gpu()
    model = NativeNetwork(len(labels), len(tok)).to(device)
    model.load_state_dict(load_file(str(Path(model_dir)/"model.safetensors"), device="cpu"))
    model.eval()
    ledger = Ledger("native-finetuned-score")
    result = []
    try:
        with torch.inference_mode():
            for start in range(0, len(rows), 8):
                output = model(**batch(tok, [encode(tok, r) for r in rows[start:start+8]], device))
                probabilities = output["types"].sigmoid()*model.supported
                existence = output["exists"].sigmoid()
                result.extend({"types": p.cpu().tolist(), "exists": float(e)} for p, e in zip(probabilities, existence))
                ledger.check()
        return result
    finally:
        ledger.stop()
        del model
        gc.collect()
        torch.cuda.empty_cache()
