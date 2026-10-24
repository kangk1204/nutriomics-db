"""Source-only MLM and annotation-only clinical training in independent outputs."""
from __future__ import annotations

import gc
import json
import math
import random
import re
from collections import Counter
from pathlib import Path

from .common import (MODEL, REVISION, POLICY, Ledger, atomic, canonical, checkpoint,
                     digest, exact, filehash, gpu_setup, groups, home, metric, read,
                     seed_all, sources, textkey)
from .data import CorpusGuard, documents


def corpus_contract(path):
    path = Path(path)
    if path.is_dir():
        path = path/"manifest.json"
    value = read(path)
    if (value.get("training_ready") is not True or value.get("status") != "ready"
            or value.get("all_public_gold_excluded") is not True
            or not value.get("exact_tokenization") or not value.get("actual_tokens")):
        raise FileNotFoundError("DAPT 코퍼스의 전체 공개 정답 제외·정확 토큰화 감사 미완료")
    if not value.get("public_gold_sources"):
        raise ValueError("모든 공개 학습·평가 자료 보호 출처 감사 필요")
    protection_path = path.parent/value.get("exclusions", value.get("exclusion_manifest", value.get("protection_path", "protection.json")))
    expected = value.get("exclusion_manifest_sha256", value.get("protection_sha256"))
    if not expected or filehash(protection_path) != expected:
        raise ValueError("DAPT 공개 정답 보호 manifest 해시 불일치")
    protection = read(protection_path)
    if protection.get("source_only") is not True or protection.get("all_public_gold_excluded") is not True:
        raise ValueError("공개 정답 전체 보호 계약 필요")
    if not protection.get("public_gold_sources"):
        raise ValueError("보호 목록의 공개 자료 출처 없음")
    if value.get("tokenizer", {}).get("name") != MODEL or value["tokenizer"].get("revision") != REVISION:
        raise ValueError("DAPT 코퍼스 정확 토큰화와 고정 모델 revision 불일치")
    protection = {**protection, "text_hashes": protection.get("normalized_text_hashes", protection.get("text_hashes", []))}
    for source in value["files"]:
        candidate = (path.parent/source["path"]).resolve()
        if not candidate.is_relative_to(path.parent.resolve()) or filehash(candidate) != source["sha256"]:
            raise ValueError("허용 코퍼스 경로 또는 해시 위반")
    return path, value, protection


def tokenize_corpus(path=None):
    from transformers import AutoTokenizer
    path, contract, protection = corpus_contract(path or home()/"corpus")
    guard = CorpusGuard(protection, documents("relations-train")+documents("identity-train")+documents("development-sources"))
    directory = home()/"dapt"
    directory.mkdir(parents=True, exist_ok=True)
    receipt = directory/"tokens-manifest.json"
    if receipt.exists():
        result = read(receipt)
        if result["corpus_sha256"] != filehash(path) or filehash(directory/"tokens.jsonl") != result["tokens_sha256"]:
            raise ValueError("DAPT 토큰 체크포인트 불일치")
        return result
    tok = AutoTokenizer.from_pretrained(MODEL, revision=REVISION, use_fast=True, cache_dir=home()/"cache/huggingface")
    seen_pmid, seen_text, stats = set(), set(), Counter()
    temporary = directory/"tokens.part.jsonl"
    tokens, stop = 0, False
    with temporary.open("w", encoding="utf8", newline="\n") as output:
        for source in contract["files"]:
            if stop:
                break
            with (path.parent/source["path"]).open(encoding="utf8") as stream:
                for line in stream:
                    checkpoint()
                    d = json.loads(line)
                    if any(k in d for k in ("entities", "relations", "labels", "gold")):
                        raise ValueError("DAPT 입력은 원문만 허용")
                    if d.get("text_sha256", d.get("source_sha256")) != digest(d["text"]):
                        raise ValueError("DAPT 원문 해시 불일치")
                    reason = guard.reason(d)
                    key = textkey(d["text"])
                    if reason or str(d.get("pmid")) in seen_pmid or key in seen_text:
                        stats[reason or "duplicate"] += 1
                        continue
                    seen_pmid.add(str(d.get("pmid")))
                    seen_text.add(key)
                    ids = tok(d["text"], add_special_tokens=False, truncation=False)["input_ids"]
                    for offset in range(0, len(ids), 510):
                        budget = min(512, POLICY["dapt_tokens"]-tokens)
                        if budget < 4:
                            stop = True
                            break
                        chunk = ids[offset:offset+budget-2]
                        if not chunk:
                            continue
                        input_ids = [tok.cls_token_id, *chunk, tok.sep_token_id]
                        output.write(canonical({"pmid": str(d["pmid"]), "input_ids": input_ids,
                                                "source_sha256": d["source_sha256"], "text_sha256": digest(d["text"]), "offset_tokens": offset})+"\n")
                        tokens += len(input_ids)
                        stats["chunks"] += 1
                    stats["documents"] += 1
                    if stop:
                        break
    if not stats["chunks"]:
        temporary.unlink(missing_ok=True)
        raise FileNotFoundError("DAPT 허용·보호 검사 후 학습 문헌 0건")
    temporary.replace(directory/"tokens.jsonl")
    result = {"actual_tokens": tokens, "maximum_tokens": POLICY["dapt_tokens"], "one_pass": True,
              "stats": dict(stats), "corpus_sha256": filehash(path), "tokenizer": {"model": MODEL, "revision": REVISION},
              "tokens_sha256": filehash(directory/"tokens.jsonl"), "all_public_gold_excluded": True}
    atomic(receipt, result, True)
    return result


def mask_tokens(input_ids, tok, seed):
    import torch
    generator = torch.Generator().manual_seed(seed)
    ids = torch.tensor([input_ids], dtype=torch.long)
    eligible = torch.tensor([[v not in tok.all_special_ids for v in input_ids]])
    selected = (torch.rand(ids.shape, generator=generator) < .15) & eligible
    if not selected.any() and eligible.any():
        selected[0, int(torch.nonzero(eligible[0])[0])] = True
    labels = ids.clone()
    labels[~selected] = -100
    replacement = torch.rand(ids.shape, generator=generator)
    masked = ids.clone()
    masked[selected & (replacement < .8)] = tok.mask_token_id
    random_positions = selected & (replacement >= .8) & (replacement < .9)
    masked[random_positions] = torch.randint(len(tok), ids.shape, generator=generator)[random_positions]
    return masked, labels


def dapt(path=None):
    import torch
    from transformers import AutoModelForMaskedLM, AutoTokenizer
    manifest = tokenize_corpus(path)
    directory = home()/"dapt"
    complete = directory/"complete.json"
    if complete.exists():
        result = read(complete)
        if result["token_manifest_sha256"] != filehash(directory/"tokens-manifest.json"):
            raise ValueError("DAPT 완료 입력 계약 변경")
        return result
    seed_all(20261023)
    device, gpu = gpu_setup()
    tok = AutoTokenizer.from_pretrained(MODEL, revision=REVISION, use_fast=True, cache_dir=home()/"cache/huggingface")
    model = AutoModelForMaskedLM.from_pretrained(MODEL, revision=REVISION,
        cache_dir=home()/"cache/huggingface", attn_implementation="sdpa")
    model.resize_token_embeddings(len(tok), mean_resizing=False)
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.to(device).train()
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-5, weight_decay=.01)
    checkpoint_path = directory/"checkpoint.pt"
    done, total_loss = 0, 0.
    expected = digest(canonical([manifest, sources(), MODEL, REVISION]))
    if checkpoint_path.exists():
        value = torch.load(checkpoint_path, map_location=device, weights_only=True)
        if value["contract"] != expected:
            raise ValueError("DAPT 재개 계약 변경")
        model.load_state_dict(value["model"])
        optimizer.load_state_dict(value["optimizer"])
        done, total_loss = value["chunks"], value["total_loss"]
        if value.get("rng_cpu") is not None:
            torch.set_rng_state(value["rng_cpu"].cpu())
            torch.cuda.set_rng_state_all(value["rng_cuda"])
    ledger = Ledger()
    ledger.start("dapt-one-pass")
    processed = done
    try:
        optimizer.zero_grad(set_to_none=True)
        with (directory/"tokens.jsonl").open(encoding="utf8") as stream:
            for index, line in enumerate(stream):
                if index < done:
                    continue
                value = json.loads(line)
                ids, labels = mask_tokens(value["input_ids"], tok, 20261023+index)
                output = model(input_ids=ids.to(device), attention_mask=torch.ones_like(ids).to(device), labels=labels.to(device))
                if not torch.isfinite(output.loss):
                    raise FloatingPointError("DAPT 손실 NaN/Inf")
                (output.loss/16).backward()
                total_loss += float(output.loss.detach())
                processed = index+1
                if processed % 16 == 0 or processed == manifest["stats"]["chunks"]:
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.)
                    optimizer.step()
                    optimizer.zero_grad(set_to_none=True)
                if processed % 128 == 0 or processed == manifest["stats"]["chunks"]:
                    temporary = directory/"checkpoint.part.pt"
                    torch.save({"contract": expected, "model": model.state_dict(), "optimizer": optimizer.state_dict(),
                                "chunks": processed, "total_loss": total_loss, "rng_cpu": torch.get_rng_state(),
                                "rng_cuda": torch.cuda.get_rng_state_all()}, temporary)
                    temporary.replace(checkpoint_path)
                    atomic(directory/"progress.json", {"chunks": processed, "total": manifest["stats"]["chunks"],
                           "mean_loss": total_loss/processed})
                ledger.check()
        model.save_pretrained(directory/"encoder", safe_serialization=True)
        tok.save_pretrained(directory/"encoder")
        result = {"trained": True, "chunks": processed, "actual_tokens": manifest["actual_tokens"],
                  "epochs": 1, "mean_loss": total_loss/processed, "gpu_uuid": gpu["uuid"],
                  "token_manifest_sha256": filehash(directory/"tokens-manifest.json"),
                  "used_as_default_encoder": False, "requires_supervised_ablation_before_adoption": True,
                  "files": {str(p.relative_to(directory)): filehash(p) for p in (directory/"encoder").iterdir() if p.is_file()}}
        atomic(complete, result, True)
        return result
    finally:
        ledger.stop()
        del model, optimizer
        gc.collect()
        torch.cuda.empty_cache()


def clinical_rows(document):
    """Only literal official span labels; ICO descriptions never become span gold."""
    from ..clinical_v4 import TOKEN, token_features
    from ..text import sentence_spans
    spans = document.get("annotated_spans", [])
    annotated_labels = document.get("annotated_labels")
    if not isinstance(annotated_labels, list):
        raise ValueError("임상 범주별 주석 파일 존재 여부 annotated_labels 필요")
    for s in spans:
        surface = s.get("text", document["text"][s["start"]:s["end"]])
        if not exact(document["text"], {**s, "text": surface}):
            raise ValueError("임상 학습 주석 위치 불일치")
    for sentence in sentence_spans(document["text"], document.get("sections", [])):
        tokens = [{"text": m.group(), "start": sentence["start"]+m.start(), "end": sentence["start"]+m.end()}
                  for m in TOKEN.finditer(sentence["text"])]
        if not tokens:
            continue
        x = [token_features(tokens, i) for i in range(len(tokens))]
        labels = {}
        for role in ("participants", "interventions", "outcomes"):
            if role not in annotated_labels:
                continue
            tags, previous = [], None
            for t in tokens:
                covering = next(((s["start"], s["end"]) for s in spans if s["label"] == role
                                 and s["start"] <= t["start"] and t["end"] <= s["end"]), None)
                tags.append("O" if covering is None else "I" if previous == covering else "B")
                previous = covering
            labels[role] = tags
        yield {"pmid": str(document["pmid"]), "x": x, "labels": labels}


def clinical():
    """Independent CPU CRFs; full ICO/group/dose/time accuracy remains separate."""
    import joblib
    import sklearn_crfsuite
    path = home()/"data/clinical-train.json"
    audit_path = home()/"data/clinical-audit.json"
    if not path.exists() or not audit_path.exists():
        raise FileNotFoundError("공식 임상 학습 분할·누출 감사 입력 미준비")
    value, audit = read(path), read(audit_path)
    if (value.get("split") != "train" or not audit.get("passed") or audit.get("protected_overlap") != 0
            or audit.get("audited_training_sha256") != filehash(path)):
        raise ValueError("임상 학습 입력과 공식 train-only 감사 불일치")
    docs = [d for d in value["documents"] if d.get("annotated_spans") is not None]
    if not docs:
        raise FileNotFoundError("공식 구절 주석 없는 ICO 설명을 구절 정답으로 변환하지 않음")
    protected = read(home()/"data/protection.json")
    guard = CorpusGuard(protected)
    # CorpusGuard includes relation training, which is deliberately excluded here too.
    if any(guard.reason(d) for d in docs):
        raise ValueError("임상 학습·보호 문헌 중복")
    assignment = groups(docs, 5, "clinical-20261023")
    rows = [r for d in docs for r in clinical_rows(d)]
    results = {}
    output = home()/"clinical"
    output.mkdir(parents=True, exist_ok=True)
    for role in ("participants", "interventions", "outcomes"):
        role_rows = [r for r in rows if role in r["labels"]]
        if not role_rows:
            raise FileNotFoundError("임상 범주 주석 0건: "+role)
        candidates = []
        for c in (.1, 1., 10.):
            gold, predicted = set(), set()
            for k in range(5):
                checkpoint()
                train = [r for r in role_rows if assignment[r["pmid"]] != k]
                valid = [r for r in role_rows if assignment[r["pmid"]] == k]
                model = sklearn_crfsuite.CRF(algorithm="lbfgs", c1=.1, c2=1/c, max_iterations=50,
                                             all_possible_transitions=True)
                model.fit([r["x"] for r in train], [r["labels"][role] for r in train])
                labels = model.predict([r["x"] for r in valid])
                for i, (row, prediction) in enumerate(zip(valid, labels)):
                    for j, label in enumerate(row["labels"][role]):
                        if label != "O":
                            gold.add((k, i, j))
                    for j, label in enumerate(prediction):
                        if label != "O":
                            predicted.add((k, i, j))
            candidates.append({"C": c, **metric(gold, predicted)})
        selected = max(candidates, key=lambda v: (v["f1"], -v["C"]))
        model = sklearn_crfsuite.CRF(algorithm="lbfgs", c1=.1, c2=1/selected["C"], max_iterations=50,
                                     all_possible_transitions=True)
        model.fit([r["x"] for r in role_rows], [r["labels"][role] for r in role_rows])
        joblib.dump(model, output/(role+".joblib"))
        results[role] = {"selected": selected, "candidates": candidates,
                         "annotated_sequences": len(role_rows),
                         "model_sha256": filehash(output/(role+".joblib"))}
    result = {"trained": True, "models": results, "training_papers": len(docs), "sentences": len(rows),
              "selection": "5-fold public-train paper-grouped CRF; not nested final performance",
              "clinical_ICO_evidence_and_group_dose_time": "not_evaluated_by_span_model",
              "source_sha256": sources(), "training_sha256": filehash(path),
              "nutrition_accuracy": "unmeasured", "independent_final_performance": False}
    atomic(output/"manifest.json", result, True)
    return result


def clinical_role():
    """PubMed RCT native sentence role; role is never treated as observed truth."""
    import joblib
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import accuracy_score, f1_score
    from sklearn.pipeline import make_pipeline
    path, audit_path = home()/"data/rct-train.json", home()/"data/clinical-audit.json"
    if not path.exists() or not audit_path.exists():
        raise FileNotFoundError("공식 RCT 문장역할 학습 입력 미준비")
    data, audit = read(path), read(audit_path)
    if (data.get("split") != "train" or not audit.get("passed") or audit.get("protected_overlap") != 0
            or audit.get("audited_rct_training_sha256") != filehash(path)):
        raise ValueError("RCT 문장역할 입력과 공식 train-only 감사 불일치")
    docs = data["documents"]
    allowed = {"BACKGROUND", "OBJECTIVE", "METHODS", "RESULTS", "CONCLUSIONS"}
    rows = []
    guard = CorpusGuard(read(home()/"data/protection.json"))
    for d in docs:
        if guard.reason(d):
            raise ValueError("RCT 학습·보호 문헌 중복")
        for s in d["sentences"]:
            if s["label"] not in allowed or not exact(d["text"], s):
                raise ValueError("RCT native label 또는 원문 위치 불일치")
            rows.append({"pmid": str(d["pmid"]), "text": s["text"], "label": s["label"]})
    def model(c):
        return make_pipeline(TfidfVectorizer(ngram_range=(1, 2), min_df=2, max_features=150000, sublinear_tf=True),
            LogisticRegression(C=c, max_iter=300, random_state=20261023, class_weight="balanced"))
    def choose(ds, subset, salt):
        folds = groups(ds, 3, salt)
        options = []
        for c in (.1, 1., 10.):
            actual, predicted = [], []
            for k in range(3):
                checkpoint()
                fit_rows = [r for r in subset if folds[r["pmid"]] != k]
                held_rows = [r for r in subset if folds[r["pmid"]] == k]
                learner = model(c).fit([r["text"] for r in fit_rows], [r["label"] for r in fit_rows])
                predicted.extend(learner.predict([r["text"] for r in held_rows]).tolist())
                actual.extend(r["label"] for r in held_rows)
            options.append({"C": c, "macro_f1": f1_score(actual, predicted, average="macro", zero_division=0),
                            "accuracy": accuracy_score(actual, predicted)})
        return max(options, key=lambda v: (v["macro_f1"], v["accuracy"], -v["C"])), options
    outer = groups(docs, 5, "rct-native-role-outer")
    results = []
    for k in range(5):
        training_docs = [d for d in docs if outer[str(d["pmid"])] != k]
        train_rows = [r for r in rows if outer[r["pmid"]] != k]
        held_rows = [r for r in rows if outer[r["pmid"]] == k]
        choice, _ = choose(training_docs, train_rows, "rct-native-inner-"+str(k))
        learner = model(choice["C"]).fit([r["text"] for r in train_rows], [r["label"] for r in train_rows])
        predictions = learner.predict([r["text"] for r in held_rows])
        truth = [r["label"] for r in held_rows]
        results.append({"outer_fold": k, "C": choice["C"], "macro_f1": f1_score(truth, predictions, average="macro", zero_division=0),
                        "accuracy": accuracy_score(truth, predictions)})
    selected, candidates = choose(docs, rows, "rct-native-final")
    learner = model(selected["C"]).fit([r["text"] for r in rows], [r["label"] for r in rows])
    output = home()/"clinical-role"
    output.mkdir(parents=True, exist_ok=True)
    joblib.dump(learner, output/"model.joblib")
    result = {"trained": True, "task": "native_rct_sentence_role", "training_papers": len(docs),
              "sentences": len(rows), "selected": selected, "candidates": candidates, "outer_folds": results,
              "model_sha256": filehash(output/"model.joblib"), "training_sha256": filehash(path),
              "role_is_not_observation_confirmation": True, "independent_final_performance": False}
    atomic(output/"manifest.json", result, True)
    return result
