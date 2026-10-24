"""Label-independent paper/PMCID/normalized-text groups, nested CV."""
from collections import defaultdict
import re
import unicodedata
from .contract_v7 import SEED, sha


def text_key(text):
    return sha(re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip().casefold())


def grouped_folds(documents, folds=5, salt="outer"):
    parent = {str(d["pmid"]): str(d["pmid"]) for d in documents}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    seen = {}
    for d in documents:
        p = str(d["pmid"])
        for key in ["text:" + text_key(d["text"]), *(["pmc:" + d["pmcid"]] if d.get("pmcid") else [])]:
            if key in seen:
                a, b = find(p), find(seen[key])
                parent[max(a, b)] = min(a, b)
            seen[key] = p
    groups = defaultdict(list)
    for key in parent:
        groups[find(key)].append(key)
    ordered = sorted(groups, key=lambda x: sha(str(SEED) + ":" + salt + ":" + x))
    return {pmid: i % folds for i, group in enumerate(ordered) for pmid in groups[group]}


def audit_overlap(training, protected):
    ids = {str(d["pmid"]) for d in protected}
    pmcs = {d["pmcid"] for d in protected if d.get("pmcid")}
    hashes = {text_key(d["text"]) for d in protected}
    kept, excluded = [], []
    for doc in training:
        reasons = [name for name, value in (("pmid", str(doc["pmid"]) in ids),
                   ("pmcid", bool(doc.get("pmcid") and doc["pmcid"] in pmcs)),
                   ("normalized_text", text_key(doc["text"]) in hashes)) if value]
        (excluded if reasons else kept).append({"pmid": str(doc["pmid"]), "reasons": reasons} if reasons else doc)
    return kept, excluded
