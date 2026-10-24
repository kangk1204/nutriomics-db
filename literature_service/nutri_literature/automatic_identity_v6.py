"""Independent absolute gene-ID confidence, learned from automatic span errors.

Relation labels/scores are never inputs to ID selection. Unsupported/unknown
species and gene families are always retained for review, never filled in.
"""
from collections import defaultdict
from copy import deepcopy
from difflib import SequenceMatcher
import json
from pathlib import Path
import re

from .db import digest, dumps, now


def source_sha():
    return digest(Path(__file__).read_bytes())


def rows_for(mention, text, sentences, search):
    from .features_v5 import identity_features
    from .identity_v6 import definition_for, taxon_evidence
    from .extraction_v3 import abbreviations
    sentence = next((s for s in sentences if s["start"] <= mention["start"] < mention["end"] <= s["end"]), None)
    if sentence is None:
        return [], [], {"eligible": False, "reason": "sentence_boundary_unresolved"}
    definition = definition_for(abbreviations(text), mention["text"], mention["start"])
    surface = definition["long"] if definition else mention["text"]
    taxon, _, evidence = taxon_evidence(mention, sentence, sentences, text, {})
    eligible = taxon in {9606, 10090, 10116} and not re.search(r"\b(?:family|genes|proteins)\b", surface, re.I)
    records = search.retrieve(surface, taxon)
    features = []
    for i, record in enumerate(records):
        aliases = [record["symbol"], record["name"], *record["aliases"]]
        key = re.sub(r"\W", "", surface.casefold())
        similarities = [SequenceMatcher(None, key, re.sub(r"\W", "", alias.casefold())).ratio() for alias in aliases]
        row = {**identity_features(mention, record, sentence["text"], taxon, definition),
               "retrieval_exact": record["retrieval_exact"], "retrieval_similarity": record["retrieval_similarity"],
               "best_spelling_similarity": max(similarities or [0]), "candidate_rank": i,
               "span_priority": float(mention.get("payload", {}).get("detection_priority_score", 1)),
               "span_selected": float(mention.get("payload", {}).get("automatic_selected", True)),
               "surface_length": len(surface), "surface_upper": surface.isupper(),
               "candidate_count": len(records), "eligible": bool(eligible),
               "species_evidence_count": len(evidence)}
        features.append(row)
    return records, features, {"eligible": bool(eligible), "taxon_id": taxon, "source_taxon_evidence": evidence}


def choose(records, scores, eligibility, threshold, margin):
    if not eligibility["eligible"]:
        return None
    ranked = sorted([(r, s) for r, s in zip(records, scores) if r["taxon_id"] == eligibility["taxon_id"]], key=lambda x: (-x[1], x[0]["id"]))
    if not ranked or ranked[0][1] < threshold or (len(ranked) > 1 and ranked[0][1] - ranked[1][1] < margin):
        return None
    return ranked[0][0]


def apply(mentions, text, sentences, search, model, manifest):
    result, trace = deepcopy(mentions), []
    for mention in result:
        if mention["kind"] != "gene" or mention.get("normalized_id"):
            continue
        records, features, eligibility = rows_for(mention, text, sentences, search)
        scores = [v.get("1", 0) for v in model.probabilities(features)]
        record = choose(records, scores, eligibility, manifest["selection"]["threshold"], manifest["selection"]["margin"])
        trace.append({"start": mention["start"], "end": mention["end"], "selected": record["id"] if record else None,
                      "eligibility": eligibility, "candidates": [{"id": r["id"], "priority": s} for r, s in zip(records, scores)]})
        if record:
            mention["normalized_id"] = record["id"]
            mention["payload"].update(native_id=record["native_id"], previous_identity_reason=mention["payload"].get("identity_reason"),
                                      identity_reason=None, normalization_rule="independent_absolute_id_v6_r5",
                                      identity_selection_source="source-only; no relation prediction")
    return result, trace


def train(raw_data, audit, automatic, models, output):
    import joblib
    from .refinement_v6 import read_training
    from .candidates_v4 import GeneIndex
    from .identity_v6 import CandidateSearch
    from .models_v6 import LinearHead
    from .training_v6 import folds
    from .text import sentence_spans
    from .benchmarks import _metrics, _write_immutable
    data, _ = read_training(raw_data, audit)
    automatic, models, output = Path(automatic), Path(models), Path(output)
    pointer = json.loads((models / "gene-snapshot.json").read_bytes())
    search = CandidateSearch(GeneIndex.load(models / "genes" / pointer["filename"]))
    feature_manifest = json.loads((automatic / "manifest.json").read_bytes())
    examples = []
    for doc in data["documents"]:
        sentences = sentence_spans(doc["text"], doc["sections"])
        if doc.get("gene_annotation_complete") is False:
            continue
        gold = {(e["start"], e["end"]): set(map(str, e.get("native_ids", []))) for e in doc["entities"] if e["kind"] == "gene"}
        # Gold augmentation adds known positive spellings, while actual raw
        # candidates below expose false boundaries/common-word mistakes.
        sources = [(e, "gold") for e in doc["entities"] if e["kind"] == "gene"]
        path = automatic / ("paper-" + doc["pmid"] + ".json")
        if path.exists():
            raw = json.loads(path.read_bytes())
            assert raw["source_hash"] == digest(doc["text"])
            sources += [(e, "automatic") for e in raw["entities"] if e["kind"] == "gene" and not e.get("normalized_id")]
        for e, origin in sources:
            records, features, eligibility = rows_for(e, doc["text"], sentences, search)
            if not features:
                continue
            native = gold.get((e["start"], e["end"]), set())
            examples.append({"pmid": doc["pmid"], "start": e["start"], "end": e["end"], "origin": origin,
                             "records": [{k: r[k] for k in ("id", "native_id", "taxon_id")} for r in records],
                             "features": features, "eligibility": eligibility,
                             "labels": [int(r["native_id"] in native) for r in records], "native_ids": sorted(native)})
    assignment = folds([d["pmid"] for d in data["documents"]])
    rows, labels, owners = [], [], []
    for i, e in enumerate(examples):
        rows.extend(e["features"])
        labels.extend(e["labels"])
        owners.extend([i] * len(e["features"]))
    configurations, by_c = [], {}
    for c in (.1, 1.0, 10.0):
        scores = {}
        for fold in range(5):
            fit = [i for i, owner in enumerate(owners) if assignment[examples[owner]["pmid"]] != fold]
            held = [i for i, e in enumerate(examples) if e["origin"] == "automatic" and assignment[e["pmid"]] == fold]
            head = LinearHead().fit([rows[i] for i in fit], [labels[i] for i in fit], c)
            for index in held:
                scores[index] = [v.get("1", 0) for v in head.probabilities(examples[index]["features"])]
        by_c[c] = scores
        expected = {i for i, e in enumerate(examples) if e["origin"] == "automatic" and e["native_ids"]}
        for threshold in (.5, .6, .7, .8, .85, .9, .95):
            for margin in (.0, .1, .2):
                tp, fp, fn = 0, 0, 0
                for i, values in scores.items():
                    e = examples[i]
                    record = choose(e["records"], values, e["eligibility"], threshold, margin)
                    correct = record is not None and record["native_id"] in e["native_ids"]
                    tp += bool(correct)
                    fp += record is not None and not correct
                    fn += i in expected and not correct
                precision = tp / (tp + fp) if tp + fp else 0
                recall = tp / (tp + fn) if tp + fn else 0
                f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0
                configurations.append({"C": c, "threshold": threshold, "margin": margin, "tp": tp, "fp": fp, "fn": fn,
                                       "precision": precision, "recall": recall, "f1": f1})
        print(dumps({"stage": "absolute_id_oof", "C": c}), flush=True)
    eligible = [r for r in configurations if r["tp"] and r["precision"] >= .9]
    selection = max(eligible or configurations, key=lambda r: (r["f1"], r["recall"], -r["C"], r["threshold"]))
    head = LinearHead().fit(rows, labels, selection["C"])
    output.mkdir(parents=True, exist_ok=True)
    if (output / "manifest.json").exists():
        raise ValueError("Identity output must be new")
    joblib.dump(head, output / "identity.joblib")
    oof = []
    for index, values in by_c[selection["C"]].items():
        e = examples[index]
        record = choose(e["records"], values, e["eligibility"], selection["threshold"], selection["margin"])
        oof.append({"pmid": e["pmid"], "start": e["start"], "end": e["end"], "record": record})
    _write_immutable(output / "oof-identities.json", oof)
    manifest = {"source_sha256": source_sha(), "created_at": now(), "training_split": "train",
                "training_pmids": sorted(assignment), "test_used_for_selection": False,
                "training_sha256": digest(Path(raw_data).read_bytes()), "audit_sha256": digest(Path(audit).read_bytes()),
                "automatic_features_manifest_sha256": digest((automatic / "manifest.json").read_bytes()),
                "gene_dictionary_sha256": search.gene_index.manifest["records_sha256"],
                "selection": selection, "target_precision_met": bool(eligible), "configurations": configurations,
                "examples": len(examples), "candidate_rows": len(rows), "upstream_models_fixed_not_fold_refit": True,
                "relation_labels_used": False, "species_default": None,
                "files": {name: digest((output / name).read_bytes()) for name in ("identity.joblib", "oof-identities.json")}}
    _write_immutable(output / "manifest.json", manifest)
    return manifest


def load(path):
    import joblib
    path = Path(path)
    manifest = json.loads((path / "manifest.json").read_bytes())
    assert manifest["source_sha256"] == source_sha() and manifest["training_split"] == "train"
    for name, sha in manifest["files"].items():
        assert Path(name).name == name and digest((path / name).read_bytes()) == sha
    return joblib.load(path / "identity.joblib"), manifest
