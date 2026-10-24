"""Train-only import and label-free held-out source audit for v5."""

import json
from pathlib import Path
import re
import zipfile

from .benchmarks import _write_immutable
from .db import digest
from .public_corpora import parse_pubtator
from .training_v4 import text_key

RELEASE_SHA = "29aa0b2060f9d3dc66d9452412e741158f69fb06021c7e326174cb3f1a1c4a85"
TRAIN_MEMBER = "bioredirect/bioredirect_train.pubtator"
FINAL_MEMBER = "bioredirect/bioredirect_bc8_test.pubtator"


def source_only(raw):
    documents, parts = [], {}
    for line in raw.splitlines():
        match = re.match(r"^(\S+)\|([ta])\|(.*)$", line)
        if match:
            parts.setdefault(match[1], []).append((match[2], match[3]))
    for pmid, values in parts.items():
        text = " ".join(v for _, v in values)
        sections, offset = [], 0
        for tag, value in values:
            sections.append(
                {"start": offset, "end": offset + len(value), "label": "Title" if tag == "t" else "Abstract"}
            )
            offset += len(value) + 1
        documents.append(
            {
                "pmid": pmid,
                "text": text,
                "sections": sections,
                "source_hash": digest(text),
                "source_level": "abstract",
                "group_id": pmid,
            }
        )
    return documents


def prepare(runtime, output, clinical_manifest):
    runtime, output = Path(runtime), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    archive = runtime / "public_benchmarks/bioredirect/404a04bf9fb316caa061e9ab/source.release"
    if digest(archive.read_bytes()) != RELEASE_SHA:
        raise ValueError("Pinned BioREDirect release differs")
    with zipfile.ZipFile(archive) as release:
        # Only train labels are parsed. BC8 source lines are extracted without interpreting annotations.
        train_raw = release.read(TRAIN_MEMBER).decode()
        train = parse_pubtator(train_raw)
        final_raw = release.read(FINAL_MEMBER).decode()
        final = source_only(final_raw)
    if len(train) != 400 or len(final) != 400:
        raise ValueError("Official article counts differ")
    held = {d["pmid"] for d in final}
    held_text = {text_key(d["text"]) for d in final}
    for path in sorted((runtime / "public_benchmarks").rglob("inputs.json")):
        value = json.loads(path.read_bytes())
        if value.get("split") == "test":
            for doc in value.get("documents", []):
                held.add(str(doc["pmid"]))
                held_text.add(text_key(doc["text"]))
    for path in sorted(
        (runtime.parent.parent / "nutriomics-literature-v4-development/runtime/holdouts").glob(
            "*/inputs.json"
        )
    ):
        value = json.loads(path.read_bytes())
        for doc in value.get("documents", []):
            held.add(str(doc["pmid"]))
            held_text.add(text_key(doc["text"]))
    leaks = [d["pmid"] for d in train if d["pmid"] in held or text_key(d["text"]) in held_text]
    train = [d for d in train if d["pmid"] not in leaks]
    clinical = json.loads(Path(clinical_manifest).read_bytes())
    overlap = {name: sorted(set(ids) & held) for name, ids in clinical["training_pmids"].items()}
    train_ids = {d["pmid"] for d in train}
    record = {
        "schema": "v5-evaluation-preregistration",
        "release_sha256": RELEASE_SHA,
        "train_member": TRAIN_MEMBER,
        "train_member_sha256": digest(train_raw),
        "final_member": FINAL_MEMBER,
        "final_member_sha256": digest(final_raw),
        "official_train_articles": 400,
        "train_articles": len(train),
        "final_articles": 400,
        "train_pmids": sorted(train_ids),
        "protected_pmids": sorted(held),
        "protected_text_hashes": sorted(held_text),
        "train_heldout_overlap": [],
        "excluded_relation_train_pmids": leaks,
        "clinical_train_exclusions": overlap,
        "seed": 20261009,
        "C": [0.1, 1, 10],
        "folds": 5,
        "split_unit": "paper",
        "bootstrap": 2000,
        "bc8_annotation_labels_read": False,
        "pmcid_audit": "not_available_in_pubtator_release",
        "pretraining_overlap": "unknown",
        "nutrition_accuracy": "unmeasured",
    }
    _write_immutable(output / "relation-train.json", {"split": "train", "documents": train})
    _write_immutable(output / "bc8-inputs.json", {"split": "test", "corpus": "bc8", "documents": final})
    _write_immutable(output / "audit.json", record)
    (output / "bc8-heldout-ids.txt").write_text("\n".join(sorted(held)) + "\n", encoding="utf-8")
    return record
