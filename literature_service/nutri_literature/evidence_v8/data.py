"""Read-only official adapters and explicit source/label separation."""
from __future__ import annotations

import copy
import re
import shutil
from collections import Counter
from pathlib import Path

from .common import (POLICY, atomic, canonical, digest, exact, filehash, groups,
                     home, read, sources, textkey)


def documents(name):
    value = read(home()/"data"/(name+".json"))
    return value["documents"]


def import_r2(source):
    """Copy immutable allowed snapshots; never launch or modify an r2 adapter."""
    source = Path(source).resolve()
    if source == home().resolve():
        raise ValueError("v8 출력과 과거 실험 입력 경로가 같음")
    paths = ["data/relations-train.json", "data/identity-train.json",
             "data/development-sources.json", "data/protection.json", "aliases.sqlite3",
             "knowledge/overlay.sqlite3", "knowledge/manifest.json", "knowledge/chemical-categories.json"]
    optional = ["audit.json", "prepared/manifest.json"]
    for relative in paths:
        if not (source/relative).is_file():
            raise FileNotFoundError("읽기 전용 원본 입력 미준비: " + relative)
    paths += [p for p in optional if (source/p).is_file()]
    paths += [str(p.relative_to(source)).replace("\\", "/") for split in ("train", "normalization", "development")
              for p in sorted((source/"prepared"/split).glob("paper-*.json"))]
    receipts = {}
    for relative in paths:
        original, destination = source/relative, home()/relative
        if relative == "audit.json":
            destination = home()/"inputs/r2-audit.json"
        before = filehash(original)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if filehash(destination) != before:
                raise ValueError("새 실험 버전 필요; 이전 입력 덮어쓰기 금지: " + relative)
        else:
            temporary = destination.with_suffix(destination.suffix+".part")
            shutil.copyfile(original, temporary)
            if filehash(temporary) != before or filehash(original) != before:
                temporary.unlink(missing_ok=True)
                raise ValueError("원본 복사 중 입력 변경")
            temporary.replace(destination)
        receipts[str(destination.relative_to(home())).replace("\\", "/")] = before
    result = {"files": receipts, "source_engine": "relation-repair-v6-gpu-r2",
              "source_modified": False, "development_labels_imported": False,
              "automatic_labels_are_gold": False, "source_code": sources()}
    atomic(home()/"inputs/import.json", result, True)
    atomic(home()/"policy.json", POLICY, True)
    return result


def source_only(document):
    if any(k in document for k in ("entities", "relations", "gold", "targets", "labels")):
        raise ValueError("예측 입력에 정답 포함")
    return {"pmid": str(document["pmid"]), "text": document["text"],
            **{k: document[k] for k in ("pmcid", "study_family") if document.get(k)}}


def audit():
    contract = read(home()/"inputs/import.json")
    for relative, expected in contract["files"].items():
        if filehash(home()/relative) != expected:
            raise ValueError("수입 입력 해시 불일치: " + relative)
    from .dictionary_mentions import asset_contract
    dictionary_contract, _ = asset_contract()
    atomic(home()/"dictionary-assets.json", dictionary_contract, True)
    train, external, development = (documents(v) for v in
                                    ("relations-train", "identity-train", "development-sources"))
    from .relations import native_truth
    if len(train) != 384 or sum(len(native_truth(d)) for d in train) != 549:
        raise ValueError("공식 관계 학습 계약 384편·549관계 불일치")
    if any(read(home()/"data"/(name+".json")).get("split") != "train" for name in ("relations-train", "identity-train")):
        raise ValueError("공식 train 분할만 수입 가능")
    if len(development) != 100:
        raise ValueError("동일 개발 평가 코호트 100편 필요")
    for d in development:
        source_only(d)
    protections = read(home()/"data/protection.json")
    protected_ids = set(map(str, protections["pmids"]))
    protected_text = set(protections.get("text_hashes", []))
    dev_ids = {str(d["pmid"]) for d in development}
    dev_text = {textkey(d["text"]) for d in development}
    dev_pmc = {str(d["pmcid"]) for d in development if d.get("pmcid")}
    train_ids = {str(d["pmid"]) for d in train}
    train_text = {textkey(d["text"]) for d in train}
    # r2 protection includes relation training to keep upstream NER and DAPT separate.
    for split, ds in (("relation", train), ("normalization", external)):
        for d in ds:
            if str(d["pmid"]) in dev_ids or textkey(d["text"]) in dev_text or (d.get("pmcid") and str(d["pmcid"]) in dev_pmc):
                raise ValueError("학습·개발 중복: " + split)
            if split == "normalization" and (str(d["pmid"]) in protected_ids | train_ids or textkey(d["text"]) in protected_text | train_text):
                raise ValueError("상류 개체·정규화 학습 자료 중복")
            for span in d.get("entities", []):
                if not exact(d["text"], span):
                    raise ValueError("공식 학습 개체 위치 불일치")
    rows = []
    for split, ds in (("train", train), ("normalization", external), ("development", development)):
        found = 0
        for d in ds:
            p = home()/"prepared"/split/("paper-"+str(d["pmid"])+".json")
            if not p.exists():
                continue
            v = read(p)
            if v.get("source_sha256") != digest(d["text"]):
                raise ValueError("자동 개체와 원문 해시 불일치")
            for m in v["mentions"]:
                if not exact(d["text"], m) or m.get("retrieval_uses_gold"):
                    raise ValueError("자동 개체 위치 또는 정답 주입 위반")
            found += 1
        rows.append({"split": split, "papers": len(ds), "automatic_papers": found})
    result = {"passed": True, "prepared": rows, "source_only_development": True,
              "dictionary_assets_sha256": filehash(home()/"dictionary-assets.json"),
              "automatic_profile_source": "frozen_r2_source_only_predictions",
              "upstream_ner_crossfit_matches_v8_groups": False,
              "full_pipeline_leak_free_claim_allowed": False,
              "limitation": "ID fold validation is conditional on imported r2 NER; new v8 upstream NER cross-fitting is required",
              "pretrained_model_overlap": "unknown", "pmcid_missing": sum(not d.get("pmcid") for d in train+external),
              "import_sha256": filehash(home()/"inputs/import.json"), "policy_sha256": filehash(home()/"policy.json")}
    atomic(home()/"audit.json", result, True)
    return result


def span_truth(document, mention):
    kind = mention["kind"].replace("component", "chemical")
    matching = [e for e in document.get("entities", []) if e["kind"].replace("component", "chemical") == kind
                and (e["start"], e["end"]) == (mention["start"], mention["end"])]
    ids = {str(v) for e in matching for v in e.get("native_ids", []) if v}
    complete = True
    if document.get("relation_annotation_complete") is False:
        annotated = "chemical" if document.get("gene_annotation_complete") is False else "gene"
        complete = kind == annotated
    return ids, bool(matching), complete


def id_supervision(document, mention, menu):
    """Real entity + missing gold ID has zero ranking loss, NOT a NIL target."""
    ids, valid, complete = span_truth(document, mention)
    positives = [i for i, identifier in enumerate(menu) if identifier in ids]
    retrieval_miss = valid and bool(ids) and not positives
    rank_weight = float(complete and not retrieval_miss and (bool(ids) or not valid))
    target = [float(i in positives)/len(positives) if positives else float(i == 0) for i in range(len(menu))]
    return {"targets": target, "rank_weight": rank_weight, "valid_target": float(valid),
            "valid_weight": float(complete), "retrieval_miss": retrieval_miss,
            "gold_ids": sorted(ids), "valid_span": valid}


def id_rows(split, retriever=None):
    name = "identity-train" if split == "normalization" else "relations-train" if split == "train" else "development-sources"
    result = []
    for d in documents(name):
        if split == "development":
            source_only(d)
        p = home()/"prepared"/split/("paper-"+str(d["pmid"])+".json")
        if not p.exists():
            raise FileNotFoundError("자동 개체/위치 입력 미준비: " + str(p))
        for mention in read(p)["mentions"]:
            m = copy.deepcopy(mention)
            if retriever is not None:
                m = retriever.enrich(d, m)
            from .retrieval import candidate_menu
            menu = candidate_menu(m)
            row = {"pmid": str(d["pmid"]), "text": d["text"], "mention": m, "menu": menu,
                   "source_sha256": digest(d["text"])}
            if retriever is not None:
                row["menu_taxa"] = [(retriever.kb.by_id(m["kind"], identifier) or {}).get("taxon")
                                    if identifier else None for identifier in menu]
            if split != "development":
                row.update(id_supervision(d, m, menu))
            result.append(row)
    return result


class CorpusGuard:
    """DAPT excludes every known public-gold paper, including official training."""
    def __init__(self, protection, gold_documents=()):
        self.pmids = set(map(str, protection.get("pmids", [])))
        self.pmcids = {str(v).upper() for v in protection.get("pmcids", [])}
        self.texts = set(protection.get("normalized_text_hashes", protection.get("text_hashes", [])))
        for d in gold_documents:
            self.pmids.add(str(d["pmid"]))
            if d.get("pmcid"):
                self.pmcids.add(str(d["pmcid"]).upper())
            self.texts.add(textkey(d["text"]))

    def reason(self, d):
        if str(d.get("pmid")) in self.pmids:
            return "protected_pmid"
        if d.get("pmcid") and str(d["pmcid"]).upper() in self.pmcids:
            return "protected_pmcid"
        if textkey(d["text"]) in self.texts:
            return "protected_text"
        return None


def prepare():
    audit()
    from .retrieval import build_index
    result = {"index": build_index(), "automatic_profiles": "created by train-ner and fold-specific provider",
              "old_r2_mentions_used_for_new_training": False}
    atomic(home()/"prepared-v8.json", result, True)
    return result
