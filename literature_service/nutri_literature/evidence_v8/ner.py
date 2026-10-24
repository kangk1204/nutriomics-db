"""v8-owned upstream NER, cross-fitted inside every ID training partition."""
from __future__ import annotations

import gc
from collections import Counter
from pathlib import Path

from .common import (Ledger, atomic, canonical, digest, exact, filehash, gpu_setup,
                     groups, home, metric, read, sources, textkey)
from .data import documents, id_supervision


def annotation_scope(document):
    # In the NLM input, false gene completeness identifies the chemical-only corpus.
    gene = document.get("gene_annotation_complete") is not False
    chemical = document.get("chemical_annotation_complete", document.get("gene_annotation_complete") is False)
    return [gene, bool(chemical)]


def training_rows(ds):
    from .networks import tokenizer
    tok = tokenizer()
    result = []
    for d in ds:
        scope = annotation_scope(d)
        encoded = tok(d["text"], max_length=512, stride=64, truncation=True,
                      return_overflowing_tokens=True, return_offsets_mapping=True)
        for n, offsets in enumerate(encoded["offset_mapping"]):
            targets = [[-100, -100] if a == b else [0 if scope[k] else -100 for k in range(2)] for a, b in offsets]
            for entity in d.get("entities", []):
                kind = entity["kind"].replace("component", "chemical")
                head = 0 if kind == "gene" else 1 if kind == "chemical" else None
                if head is None or not scope[head]:
                    continue
                if not exact(d["text"], entity):
                    raise ValueError("NER 공식 개체 위치 불일치")
                overlapping = [i for i, (a, b) in enumerate(offsets) if a < b and a < entity["end"] and entity["start"] < b]
                if not overlapping:
                    continue
                complete = min(offsets[i][0] for i in overlapping) == entity["start"] and max(offsets[i][1] for i in overlapping) == entity["end"]
                if not complete:
                    for i in overlapping:
                        targets[i][head] = -100
                    continue
                for j, i in enumerate(overlapping):
                    targets[i][head] = 1 if j == 0 else 2
            result.append({**{k: encoded[k][n] for k in ("input_ids", "attention_mask", "token_type_ids") if k in encoded},
                           "labels": targets, "pmid": str(d["pmid"])})
    return result


def fit(ds):
    from .learning import fit_neural
    if not ds:
        raise ValueError("상류 NER 학습 문헌 0건")
    from .dictionary_mentions import asset_contract
    assets, _ = asset_contract()
    key = digest(canonical([[(str(d["pmid"]), digest(d["text"]), d.get("entities", [])) for d in ds], sources(), assets]))[:24]
    path = home()/"ner/models"/key
    # Fixed upstream learner: nested ID/relationship policy still excludes all held groups.
    config = {"family": "two-bio-heads", "learning_rate": 2e-5, "epochs": 3, "patience": 2,
              "accumulation": 16, "fixed_upstream_configuration": True}
    fit_neural("ner", training_rows(ds), [], path, config, 20261023)
    atomic(path/"training-provenance.json", {"pmids": sorted(str(d["pmid"]) for d in ds),
           "text_hashes": sorted(digest(d["text"]) for d in ds), "fixed_configuration": config}, True)
    return path


def predict_sources(ds, model_path):
    import torch
    from safetensors.torch import load_file
    from .networks import Network, tokenizer
    key = digest(canonical([filehash(model_path/"model.safetensors"),
                           [(str(d["pmid"]), digest(d["text"])) for d in ds], sources()]))
    path = home()/"ner/predictions"/(key+".json")
    if path.exists():
        return read(path)
    tok = tokenizer()
    device, gpu = gpu_setup()
    model = Network("ner", len(tok)).to(device)
    model.load_state_dict(load_file(str(model_path/"model.safetensors"), device="cpu"))
    model.eval()
    ledger = Ledger()
    ledger.start("v8-NER-predict")
    output = {}
    try:
        with torch.inference_mode():
            for d in ds:
                encoded = tok(d["text"], max_length=512, stride=64, truncation=True,
                              return_overflowing_tokens=True, return_offsets_mapping=True)
                found = {}
                for n, offsets in enumerate(encoded["offset_mapping"]):
                    inputs = {k: torch.tensor([encoded[k][n]], device=device)
                              for k in ("input_ids", "attention_mask", "token_type_ids") if k in encoded}
                    probabilities = model(**inputs).float().softmax(-1)[0].cpu()
                    for kind, head in (("gene", 0), ("chemical", 1)):
                        current = None
                        def emit():
                            if current is None:
                                return
                            start, end, values = current
                            candidate = {"kind": kind, "start": start, "end": end, "text": d["text"][start:end],
                                         "ner_score": sum(values)/len(values), "source": "v8-crossfit-NER"}
                            k = (kind, start, end)
                            if k not in found or found[k]["ner_score"] < candidate["ner_score"]:
                                found[k] = candidate
                        for i, (a, b) in enumerate(offsets):
                            label = int(probabilities[i, head].argmax())
                            if a == b or label == 0:
                                emit()
                                current = None
                            elif label == 1 or current is None or a > current[1]+1:
                                emit()
                                current = [a, b, [float(probabilities[i, head, label])]]
                            else:
                                current[1] = b
                                current[2].append(float(probabilities[i, head, label]))
                        emit()
                    ledger.check()
                output[str(d["pmid"])] = sorted(found.values(), key=lambda m: (m["start"], m["end"], m["kind"]))
        result = {"predictions": output, "model_sha256": filehash(model_path/"model.safetensors"),
                  "training_provenance_sha256": filehash(model_path/"training-provenance.json")}
        atomic(path, result, True)
        return result
    finally:
        ledger.stop()
        del model
        gc.collect()
        torch.cuda.empty_cache()


def automatic_rows(document, raw_mentions, retriever, categories):
    """CPU-testable post-NER route with explicit text, KB and category inputs."""
    from .dictionary_mentions import extract
    from .retrieval import candidate_menu
    from ..repair_v6_gpu_r2.text import normal, species
    text, pmid = document["text"], str(document["pmid"])
    if Path(retriever.kb.home).resolve() != home().resolve():
        raise ValueError("자동 언급 KB와 v8 입력 HOME 불일치")
    document_cells = retriever.kb.cell_species(text)
    document_species = species(text)
    mentions = list(raw_mentions)
    if not all(exact(text, m) for m in mentions):
        raise ValueError("NER 자동 언급 원문 위치 불일치")
    seen = {(m["kind"], m["start"], m["end"]) for m in mentions}
    mentions.extend(m for m in extract(text, retriever.kb, categories)
                    if (m["kind"], m["start"], m["end"]) not in seen)
    rows = []
    for m in mentions:
        local = species(text[max(0, m["start"]-180):m["end"]+180])
        cells = [c for c in document_cells if abs(c["start"]-m["start"]) <= 500]
        supported = local or sorted({c["taxon"] for c in cells}) or document_species
        m = {**m, "key": digest(canonical([m["kind"], m["start"], m["end"]]))[:20],
             "supported_taxa": supported, "local_taxa": local, "cell_evidence": cells}
        m = retriever.enrich(document, m)
        if not m.get("previous_id"):
            exact_ids = [identifier for identifier in m["candidate_ids"]
                if (r := retriever.kb.by_id(m["kind"], identifier))
                and any(normal(m["text"]) == normal(n) for n in r["names"])
                and (m["kind"] != "gene" or (len(supported) == 1 and r.get("taxon") == supported[0]))]
            m["previous_id"] = exact_ids[0] if len(exact_ids) == 1 else None
        menu = candidate_menu(m)
        row = {"pmid": pmid, "text": text, "mention": m, "menu": menu,
               "source_sha256": digest(text), "menu_taxa": [(retriever.kb.by_id(m["kind"], identifier) or {}).get("taxon")
                    if identifier else None for identifier in menu]}
        if "entities" in document:
            row.update(id_supervision(document, m, menu))
        rows.append(row)
    return rows


def infer(training_docs, requested_docs):
    """Whole held groups are excluded from the upstream model before prediction."""
    from .retrieval import Retriever
    from .dictionary_mentions import asset_contract
    train_ids = {str(d["pmid"]) for d in training_docs}
    held_ids = {str(d["pmid"]) for d in requested_docs}
    if train_ids & held_ids:
        raise ValueError("상류 NER 학습·예측 문헌 중복")
    train_keys = {("text", textkey(d["text"])) for d in training_docs}
    train_keys |= {(kind, str(d[kind]).upper()) for d in training_docs for kind in ("pmcid", "study_family") if d.get(kind)}
    for d in requested_docs:
        keys = {("text", textkey(d["text"]))} | {(kind, str(d[kind]).upper()) for kind in ("pmcid", "study_family") if d.get(kind)}
        if keys & train_keys:
            raise ValueError("상류 NER 학습·예측 원문/연구가족 중복")
    assets, categories = asset_contract()
    key = digest(canonical([[(str(d["pmid"]), digest(d["text"])) for d in training_docs],
                           [(str(d["pmid"]), digest(d["text"])) for d in requested_docs], sources(), assets]))[:24]
    path = home()/"ner/profiles"/(key+".json")
    if path.exists():
        value = read(path)
        if set(value["training_pmids"]) & set(value["predicted_pmids"]):
            raise ValueError("상류 프로필 학습·예측 중복")
        return value["rows"]
    model_path = fit(training_docs)
    raw = predict_sources(requested_docs, model_path)
    rows = []
    with Retriever() as retriever:
        for d in requested_docs:
            rows.extend(automatic_rows(d, raw["predictions"][str(d["pmid"])], retriever, categories))
    atomic(path, {"rows": rows, "training_pmids": sorted(train_ids), "predicted_pmids": sorted(held_ids),
                 "dictionary_assets": assets,
                 "model_provenance_sha256": filehash(model_path/"training-provenance.json"),
                 "source_only_mentions": True, "held_group_excluded": True}, True)
    return rows


def crossfit(training_docs):
    """ID training errors are also out-of-fold; own training labels never drive its NER."""
    fold = groups(training_docs, 5, "v8-upstream-automatic-errors")
    result = []
    for k in range(5):
        result.extend(infer([d for d in training_docs if fold[str(d["pmid"])] != k],
                            [d for d in training_docs if fold[str(d["pmid"])] == k]))
    return result


def train_ner():
    docs = documents("identity-train")
    rows = crossfit(docs)
    atomic(home()/"examples/id-normalization.json", {"rows": rows, "gold_injected": False,
           "upstream_crossfit": True, "retrieval_limit": 200, "menu_limit": 50}, True)
    path = fit(docs)
    metrics = {}
    for kind in ("gene", "chemical"):
        truth = {(str(d["pmid"]), e["start"], e["end"]) for d in docs for e in d.get("entities", [])
                 if e["kind"].replace("component", "chemical") == kind}
        for arm in ("ner_only", "ner_and_dictionary"):
            predicted = {(r["pmid"], r["mention"]["start"], r["mention"]["end"]) for r in rows
                         if r["mention"]["kind"] == kind and r.get("valid_weight")
                         and (arm == "ner_and_dictionary" or r["mention"].get("source") == "v8-crossfit-NER")}
            metrics[kind+"_"+arm] = metric(truth, predicted)
    result = {"training_papers": len(docs), "automatic_oof_mentions": len(rows),
              "model": str(path.relative_to(home())).replace("\\", "/"),
              "weights_sha256": filehash(path/"model.safetensors"),
              "boundary_metrics_on_public_train_OOF": metrics,
              "independent_final_performance": False,
              "official_training_only": True, "r2_models_modified": False,
              "configuration": "fixed BiomedBERT 2BIO heads, 3 epochs, LR2e-5; not tuned on development"}
    atomic(home()/"ner/manifest.json", result, True)
    atomic(home()/"examples/manifest.json", {"files": {"id-normalization.json": filehash(home()/"examples/id-normalization.json")},
           "source_sha256": sources(), "sapbert_sha256": filehash(home()/"retrieval/manifest.json")}, True)
    return result
