"""CPU clinical role CRFs enriched with fixed MeSH chemical spans and context.

The dictionary supplies lexical features, not treatment truth or gold IDs. Weak
literal prompt supervision is explicitly separated from expert annotation.
"""
from collections import Counter, defaultdict
import json
from pathlib import Path
import re

from .db import digest, dumps, now
from .refinement_v6 import tokens, words, literal_matches, windows, read_training, ROLES


def source_sha():
    return digest(Path(__file__).read_bytes())


def mesh_terms(models):
    from .chemical_v6 import load_snapshot
    models = Path(models)
    pointer = json.loads((models / "mesh-snapshot.json").read_bytes())
    dictionary = load_snapshot(models / "mesh" / pointer["filename"], pointer["sha256"])
    trie = {}
    for alias in dictionary["index"]:
        term = words(alias)
        if not term or len(term) > 12 or (len(term) == 1 and len(term[0]) < 3):
            continue
        node = trie
        for word in term:
            node = node.setdefault(word, {})
        node[""] = True
    return trie, dictionary["manifest"]["snapshot_sha256"]


def matches(sequence, trie):
    found = []
    for start in range(len(sequence)):
        node, end = trie, start
        longest = None
        while end < len(sequence) and sequence[end]["text"].casefold() in node:
            node = node[sequence[end]["text"].casefold()]
            end += 1
            if "" in node:
                longest = (start, end)
        if longest:
            found.append(longest)
    return found


def features(text, sequence, trie, offset=0):
    terms = matches(sequence, trie)
    chemical = {i: (a, b) for a, b in terms for i in range(a, b)}
    rows = []
    units = {"mg", "kg", "g", "mcg", "ug", "μg", "ml", "iu", "daily", "weekly", "intravenously", "orally", "intramuscularly"}
    cues = {"randomized", "randomised", "assigned", "received", "administered", "given", "versus", "compared", "placebo", "sham", "control", "group", "groups", "weeks", "months", "days", "baseline"}
    for i, token in enumerate(sequence):
        word = token["text"]
        lower = word.casefold()
        row = {"bias": 1., "lower": lower, "upper": word.isupper(), "title": word.istitle(),
               "digit": word.isdigit(), "mixed_digit": any(c.isdigit() for c in word),
               "length": min(len(word), 20), "chemical": i in chemical,
               "chemical_start": chemical.get(i, (-1, -1))[0] == i,
               "chemical_end": chemical.get(i, (-1, -1))[1] == i + 1,
               "dose_unit": lower in units,
               "prior_gap": text[sequence[i - 1]["end"]:token["start"]] if i else "BOS",
               "after_gap": text[token["end"]:sequence[i + 1]["start"]] if i + 1 < len(sequence) else "EOS"}
        for size in (2, 3, 4, 5):
            row["suffix" + str(size)] = lower[-size:]
            row["prefix" + str(size)] = lower[:size]
        for step in (-3, -2, -1, 1, 2, 3):
            j = i + step
            if 0 <= j < len(sequence):
                other = sequence[j]["text"].casefold()
                row[str(step) + ":word"] = other
                row[str(step) + ":chemical"] = j in chemical
                row[str(step) + ":unit"] = other in units
            else:
                row[str(step) + ":boundary"] = True
        for cue in {t["text"].casefold() for t in sequence[max(0, i - 12):min(len(sequence), i + 13)]} & cues:
            row["near:" + cue] = True
        rows.append(row)
    return rows


def train(data_path, audit_path, models, output):
    import joblib
    import sklearn_crfsuite
    from .benchmarks import _write_immutable
    data, _ = read_training(data_path, audit_path)
    trie, mesh_sha = mesh_terms(models)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "manifest.json").exists():
        raise ValueError("Clinical role artifact must be new")
    receipt = {"source_sha256": source_sha(), "training_split": "train", "test_used_for_selection": False,
               "training_sha256": digest(Path(data_path).read_bytes()), "audit_sha256": digest(Path(audit_path).read_bytes()),
               "training_pmids": sorted(d["pmid"] for d in data["documents"]), "dictionary_sha256": mesh_sha,
               "parameters": {"c1": .1, "c2": .1, "max_iterations": 50, "window": 96},
               "supervision": "weak exact official-train prompt/source matches; unmatched text is not expert negative annotation",
               "roles": {}, "files": {}, "created_at": now()}
    # Materialize only sampled windows of one role at a time; full-paper
    # feature dictionaries for all articles exceed the shared-server budget.
    for role in ROLES:
        x, y = [], []
        aligned = descriptions = 0
        for doc in data["documents"]:
            sequence = tokens(doc["text"])
            names = {c[role + "_description"] for c in doc.get("comparisons", [])}
            descriptions += len(names)
            spans = literal_matches(sequence, names)
            aligned += sum(bool(literal_matches(sequence, [name])) for name in names)
            labels = ["O"] * len(sequence)
            for a, b in sorted(spans, key=lambda v: (-(v[1] - v[0]), v[0])):
                if any(v != "O" for v in labels[a:b]):
                    continue
                labels[a:b] = ["B"] + ["I"] * (b - a - 1)
            negative = 0
            for offset, chunk in windows(sequence):
                target = labels[offset:offset + len(chunk)]
                if not any(t != "O" for t in target):
                    if negative >= 3 or int(digest(doc["pmid"] + ":" + str(offset))[:8], 16) % 7:
                        continue
                    negative += 1
                x.append(features(doc["text"], chunk, trie, offset))
                y.append(target)
        model = sklearn_crfsuite.CRF(algorithm="lbfgs", c1=.1, c2=.1, max_iterations=50, all_possible_transitions=True).fit(x, y)
        path = output / (role + ".joblib")
        joblib.dump(model, path)
        receipt["files"][path.name] = digest(path.read_bytes())
        receipt["roles"][role] = {"sequences": len(x), "literal_descriptions": aligned, "all_descriptions": descriptions}
        print(dumps({"stage": "trial_role_training", "role": role, **receipt["roles"][role]}), flush=True)
    _write_immutable(output / "manifest.json", receipt)
    return receipt


def load(path, models):
    import joblib
    path = Path(path)
    manifest = json.loads((path / "manifest.json").read_bytes())
    trie, mesh_sha = mesh_terms(models)
    assert manifest["source_sha256"] == source_sha() and manifest["dictionary_sha256"] == mesh_sha
    assert manifest["training_split"] == "train" and manifest["test_used_for_selection"] is False
    heads = {}
    for name, sha in manifest["files"].items():
        assert Path(name).name == name and digest((path / name).read_bytes()) == sha
        heads[name.removesuffix(".joblib")] = joblib.load(path / name)
    return heads, trie, manifest


def predict(document, heads, trie):
    text = document["text"]
    sequence = tokens(text)
    found = {role: [] for role in ROLES}
    for role, head in heads.items():
        for offset, chunk in windows(sequence):
            rows = features(text, chunk, trie, offset)
            labels, marginals = head.predict_single(rows), head.predict_marginals_single(rows)
            start = None
            for i in range(len(labels) + 1):
                tag = labels[i] if i < len(labels) else "O"
                if start is not None and (tag != "I"):
                    score = sum(marginals[j].get(labels[j], 0) for j in range(start, i)) / (i - start)
                    a, b = chunk[start]["start"], chunk[i - 1]["end"]
                    found[role].append({"start": a, "end": b, "text": text[a:b], "score": score, "origin": "mesh_context_role_crf_r5"})
                    start = None
                if tag == "B" or (tag == "I" and start is None):
                    start = i
    chemical = [{"start": sequence[a]["start"], "end": sequence[b - 1]["end"],
                 "text": text[sequence[a]["start"]:sequence[b - 1]["end"]]} for a, b in matches(sequence, trie)]
    return found, chemical
