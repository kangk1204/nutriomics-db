"""Native labels are preserved; supplied gold pairs are not automatic entities."""
from __future__ import annotations

import copy
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

from .common import POLICY, atomic, canonical, digest, filehash, home, read, sources, textkey


def parse_markers(text):
    tokens = ("[C]", "[/C]", "[G]", "[/G]")
    if any(text.count(t) != 1 for t in tokens):
        raise ValueError("native 후보는 참여자 표식 2개가 정확히 필요")
    matches = list(re.finditer(r"\[(?:/?C|/?G)\]", text))
    result, spans, cursor, open_span = [], {}, 0, {}
    length = 0
    for m in matches:
        segment = text[cursor:m.start()]
        result.append(segment)
        length += len(segment)
        tag = m.group()[1:-1]
        if tag.startswith("/"):
            kind = tag[1:]
            if kind not in open_span:
                raise ValueError("참여자 닫힘 표식 순서 오류")
            start = open_span.pop(kind)
            if start == length:
                raise ValueError("빈 참여자 구절")
            spans[kind] = (start, length)
        else:
            if open_span:
                raise ValueError("겹치거나 중첩된 참여자 구절")
            open_span[tag] = length
        cursor = m.end()
    result.append(text[cursor:])
    plain = "".join(result)
    return plain, [{"role": "arg1" if kind == "C" else "arg2", "start": a, "end": b, "text": plain[a:b]}
                   for kind, (a, b) in sorted(spans.items())]


def prepare(source, protection=None):
    source = Path(source)
    source = source/"train.json" if source.is_dir() else source
    manifest_path = source.with_name("manifest.json")
    value, prior = read(source), read(manifest_path)
    if (value.get("heldout_labels_read") is not False or prior.get("native_tasks_only") is not True
            or prior.get("data_sha256") != filehash(source) or prior.get("identity_overlap")):
        raise ValueError("기존 native 공식 train-only 감사 입력 불일치")
    guard = read(protection) if protection else {"pmids": [], "pmcids": [], "text_hashes": []}
    excluded_pmids = set(map(str, guard.get("pmids", [])))
    excluded_text = set(guard.get("normalized_text_hashes", guard.get("text_hashes", [])))
    pairs, counts = {}, Counter()
    for row in value["rows"]:
        task = row["task"]
        if task not in ("drugprot", "cg") or not row["label"].startswith(task+":"):
            raise ValueError("원래 자료별 native 레이블이 아닌 값")
        if row["label"] not in value["labels"] or value["labels"][row["target"]] != row["label"]:
            raise ValueError("native 레이블 색인 불일치")
        plain, arguments = parse_markers(row["text"])
        if str(row["pmid"]) in excluded_pmids or textkey(plain) in excluded_text:
            raise ValueError("native 입력과 명시적 보호 문헌 중복")
        for span in row["segments"]:
            if span["end"]-span["start"] != len(span["text"]):
                raise ValueError("native 저장 원문 구간 길이 불일치")
        key = digest(canonical([task, str(row["pmid"]), row["text"], row["segments"]]))[:32]
        if key not in pairs:
            pairs[key] = {"key": key, "task": task, "pmid": str(row["pmid"]), "text": row["text"],
                          "unmarked_text": plain, "arguments": arguments, "segments": row["segments"], "labels": []}
        if row["label"] not in pairs[key]["labels"]:
            pairs[key]["labels"].append(row["label"])
        counts[task+"_source_rows"] += 1
    rows = sorted(pairs.values(), key=lambda r: (r["task"], int(r["pmid"]), r["key"]))
    for r in rows:
        r["labels"].sort()
        if r["task"] == "cg" and any(label.endswith(":NONE") for label in r["labels"]):
            raise ValueError("CG의 미주석 후보에 NONE 정답을 만들 수 없음")
        if any(label.endswith(":NONE") for label in r["labels"]) and len(r["labels"]) != 1:
            raise ValueError("같은 native 후보의 NONE/양성 주석 충돌")
    docs = defaultdict(list)
    for r in rows:
        docs[r["pmid"]].append(r["unmarked_text"])
    doc_list = [{"pmid": p, "text": "\n".join(sorted(set(parts)))} for p, parts in sorted(docs.items())]
    counts.update({task+"_unique_pairs": sum(r["task"] == task for r in rows) for task in ("drugprot", "cg")})
    counts.update({task+"_papers": len({r["pmid"] for r in rows if r["task"] == task}) for task in ("drugprot", "cg")})
    directory = home()/"inputs"
    directory.mkdir(parents=True, exist_ok=True)
    for original, destination in ((source, directory/"native-train.json"), (manifest_path, directory/"prior-manifest.json")):
        if destination.exists() and filehash(destination) != filehash(original):
            raise ValueError("이전 native 실험 입력 덮어쓰기 금지")
        if not destination.exists():
            shutil.copyfile(original, destination)
    output = {"pairs": rows, "documents": doc_list, "labels": value["labels"], "split": "train",
              "entity_condition": "provided_gold_arguments", "automatic_entity_condition": "not_measured",
              "cg_scope": "classification of supplied annotated trigger-role candidates; no NONE negatives",
              "drugprot_scope": "native multilabel relation classification on supplied marked candidate pairs",
              "causal_direction_supervision": False, "full_event_extraction": False}
    atomic(home()/"data/train.json", output, True)
    result = {"counts": dict(counts), "input_sha256": filehash(source), "prior_manifest_sha256": filehash(manifest_path),
              "prepared_sha256": filehash(home()/"data/train.json"),
              "protection_sha256": filehash(protection) if protection else None,
              "scope": {k: output[k] for k in ("entity_condition", "automatic_entity_condition", "cg_scope", "drugprot_scope", "full_event_extraction")},
              "deduplication_scope": "PMID and exact available window union; full-text and PMCID unavailable",
              "cross_task_label_mapping": False, "source_sha256": sources(), "sealed_labels_read": False}
    atomic(home()/"policy.json", POLICY, True)
    atomic(home()/"audit.json", result, True)
    return result


def load():
    audit = read(home()/"audit.json")
    if (filehash(home()/"inputs/native-train.json") != audit["input_sha256"]
            or filehash(home()/"data/train.json") != audit["prepared_sha256"]):
        raise ValueError("native 학습 입력 변경")
    return read(home()/"data/train.json")
