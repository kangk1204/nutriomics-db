"""Paper-separated train-only learning, shared mention bags and audited inputs."""

from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
import re

from .benchmarks import _metrics, _write_immutable
from .candidates_v4 import GeneIndex
from .db import digest, dumps, now
from .embeddings_v6 import FrozenEncoder, pooled_pair_features, span_context_features
from .extraction_v3 import abbreviations
from .features_v5 import pair_candidates, identity_features
from .identity_v6 import CandidateSearch, cluster_mentions, taxon_evidence, definition_for
from .models_v6 import C_GRID, FEATURE_SHA, FEATURE_SPEC, ID_MARGINS, LinearHead, SEED, THRESHOLDS
from .nlp_v2 import parser_for, validate_tokens
from .text import sentence_spans
from .features_v6 import source_hashes


def prepare_features(data, model_path, cache, output, audit_path):
    value = json.loads(Path(data).read_bytes())
    audit = json.loads(Path(audit_path).read_bytes())
    if (
        value.get("split") != "train"
        or value.get("protected_overlap", 0)
        or not audit.get("passed")
        or audit.get("audited_training_sha256") != digest(Path(data).read_bytes())
    ):
        raise ValueError("V6 features require audited official training articles")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    model_path = Path(model_path)
    pointer = json.loads((model_path / "gene-snapshot.json").read_bytes())
    search = CandidateSearch(GeneIndex.load(model_path / "genes" / pointer["filename"]))
    from .chemical_v6 import load_snapshot

    chemical_pointer = json.loads((model_path / "mesh-snapshot.json").read_bytes())
    chemical = load_snapshot(model_path / "mesh" / chemical_pointer["filename"], chemical_pointer["sha256"])
    encoder = FrozenEncoder(model_path / "gene" / "selected_model", cache)
    encoder.load()
    from .provider_v6 import SharedCandidates

    provider = SharedCandidates(model_path, encoder, "union")
    provider.load()
    parser = parser_for({})
    files = []
    implementation = source_hashes()
    for i, doc in enumerate(value["documents"]):
        filename = digest(str(doc["pmid"]) + dumps(implementation))[:20] + ".json"
        path = output / filename
        identity = {
            "source_hash": digest(doc["text"]),
            "feature_sha256": FEATURE_SHA,
            "feature_source_hashes": implementation,
            "encoder": encoder.manifest,
            "gene_dictionary": search.gene_index.manifest,
            "chemical_dictionary": chemical["manifest"],
            "candidate_provider": provider.manifest,
        }
        if path.exists():
            existing = json.loads(path.read_bytes())
            if existing.get("identity") != identity:
                raise ValueError("Training feature cache identity changed")
        else:
            sentences = sentence_spans(doc["text"], doc["sections"])
            definitions = abbreviations(doc["text"])
            entities = deepcopy(doc["entities"])
            id_examples = []
            for entity in entities:
                sentence = next(
                    (s for s in sentences if s["start"] <= entity["start"] < entity["end"] <= s["end"]), None
                )
                entity["payload"] = {}
                if not sentence or entity["kind"] != "gene":
                    continue
                taxon, model, evidence = taxon_evidence(entity, sentence, sentences, doc["text"], {})
                entity["payload"].update(taxon_id=taxon, study_model=model, taxon_evidence=evidence)
                definition = definition_for(definitions, entity["text"], entity["start"])
                records = search.retrieve(definition["long"] if definition else entity["text"], taxon)
                features = [
                    {
                        **identity_features(entity, r, sentence["text"], taxon, definition),
                        "retrieval_exact": r["retrieval_exact"],
                        "retrieval_similarity": r["retrieval_similarity"],
                    }
                    for r in records
                ]
                id_examples.append(
                    {
                        "features": features,
                        "labels": [int(r["native_id"] in entity.get("native_ids", [])) for r in records],
                        "eligible": [r["taxon_id"] == taxon for r in records],
                        "gene_group": bool(
                            re.search(
                                r"\b(?:family|genes|proteins)\b",
                                definition["long"] if definition else entity["text"],
                                re.I,
                            )
                        ),
                        "supported": taxon in {9606, 10090, 10116},
                        "gold_available": bool(entity.get("native_ids")),
                        "retrieved": any(r["native_id"] in entity.get("native_ids", []) for r in records),
                    }
                )
            clusters = cluster_mentions(entities, definitions, search, chemical)
            spans = []
            if doc.get("gene_annotation_complete", True):
                gold = {(e["start"], e["end"]) for e in entities if e["kind"] == "gene"}
                candidates = {
                    (e["start"], e["end"]): e for e in provider.predict(doc["text"]) if e["kind"] == "gene"
                }
                for candidate in candidates.values():
                    sentence = next(
                        (
                            s
                            for s in sentences
                            if s["start"] <= candidate["start"] < candidate["end"] <= s["end"]
                        ),
                        None,
                    )
                    if sentence:
                        spans.append(
                            {
                                "features": span_context_features(candidate, doc["text"], sentence, encoder),
                                "label": int((candidate["start"], candidate["end"]) in gold),
                            }
                        )
            pairs = pair_candidates(sentences, entities, lambda s: validate_tokens(parser.parse(s), s))
            bags = defaultdict(list)
            for pair in pairs:
                bags[
                    (
                        pair["chemical"]["payload"]["mention_cluster_id"],
                        pair["gene"]["payload"]["mention_cluster_id"],
                    )
                ].append(pair)
            rows = []
            for key, bag in bags.items():
                if doc.get("relation_annotation_complete") is False:
                    # Gene/Chem normalization corpora have no complete relation
                    # annotation; unlabelled co-occurrences are not negatives.
                    continue
                chemicals = set().union(*(set(p["chemical"].get("native_ids", [])) for p in bag))
                genes = set().union(*(set(p["gene"].get("native_ids", [])) for p in bag))
                labels, directions = set(), set()
                for relation in doc.get("relations", []):
                    if (relation["entity1"] in chemicals and relation["entity2"] in genes) or (
                        relation["entity2"] in chemicals and relation["entity1"] in genes
                    ):
                        labels.add(relation["predicate"])
                        if relation.get("subject"):
                            directions.add(
                                "chemical_to_gene" if relation["subject"] in chemicals else "gene_to_chemical"
                            )
                        elif "direction_annotated" in relation and relation["direction_annotated"]:
                            directions.add("undirected")
                rows.append(
                    {
                        "features": pooled_pair_features(bag, doc["text"], encoder),
                        "labels": sorted(labels),
                        "direction": next(iter(directions)) if len(directions) == 1 else None,
                        "witnesses": len(bag),
                        "group": list(key),
                    }
                )
            _write_immutable(
                path,
                {
                    "pmid": doc["pmid"],
                    "identity": identity,
                    "bags": rows,
                    "identities": id_examples,
                    "spans": spans,
                    "mention_clusters": len(clusters),
                    "labels_copied_to_sentences": False,
                },
            )
        files.append({"path": filename, "sha256": digest(path.read_bytes())})
        if (i + 1) % 10 == 0:
            print(dumps({"prepared": i + 1, "total": len(value["documents"])}), flush=True)
    manifest = {
        "feature_sha256": FEATURE_SHA,
        "feature_source_hashes": implementation,
        "files": files,
        "encoder": encoder.manifest,
        "candidate_provider": provider.manifest,
        "audit_sha256": digest(Path(audit_path).read_bytes()),
        "training_sha256": digest(Path(data).read_bytes()),
        "split": "train",
        "papers": len(files),
    }
    _write_immutable(output / "manifest.json", manifest)
    return manifest


def folds(pmids):
    ordered = sorted(set(pmids), key=lambda p: digest(str(SEED) + ":" + p))
    return {pmid: i % 5 for i, pmid in enumerate(ordered)}


def threshold(labels, scores, target=0.85):
    rows = []
    for value in THRESHOLDS:
        expected = {i for i, label in enumerate(labels) if label}
        predicted = {i for i, score in enumerate(scores) if score >= value}
        metric = _metrics(expected, predicted)
        rows.append({"threshold": value, **metric})
    eligible = [
        row for row in rows if row["precision"] is not None and row["precision"] >= target and row["tp"] > 0
    ]
    chosen = (
        max(eligible, key=lambda r: (r["recall"], r["f1"], r["threshold"]))
        if eligible
        else max(rows, key=lambda r: ((r["f1"] or 0), r["threshold"]))
    )
    return {**chosen, "target_met": bool(eligible), "source": "paper-separated-train-OOF", "grid": rows}


def pairwise_examples(rows):
    differences, labels = [], []
    for row in rows:
        positives = [f for f, label in zip(row["features"], row["labels"]) if label]
        negatives = [f for f, label in zip(row["features"], row["labels"]) if not label]
        for a in positives:
            for b in negatives:
                for first, second, label in ((a, b, 1), (b, a, 0)):
                    differences.append(
                        {
                            k: float(first.get(k, 0)) - float(second.get(k, 0))
                            for k in set(first) | set(second)
                        }
                    )
                    labels.append(label)
    return differences, labels


def select_identity(papers, assignments):
    """Fit relative rankers and select the abstention margin on train papers only."""
    examples = [(p["pmid"], row) for p in papers for row in p["identities"]]
    configurations = []
    for c in C_GRID:
        held_rows = []
        for fold in range(5):
            fitting = [r for p, r in examples if assignments[p] != fold]
            held = [r for p, r in examples if assignments[p] == fold]
            x, y = pairwise_examples(fitting)
            if not x:
                raise ValueError("Every ID training fold needs positive and negative retrieval candidates")
            head = LinearHead().fit(x, y, c)
            for row in held:
                eligible = row.get("eligible", [bool(f.get("taxon_match")) for f in row["features"]])
                scores = head.rank(row["features"]) if row["features"] else []
                order = sorted((i for i, ok in enumerate(eligible) if ok), key=lambda i: (-scores[i], i))
                best = order[0] if order else None
                gap = scores[best] - scores[order[1]] if len(order) > 1 else float("inf")
                allowed = (
                    best is not None
                    and row.get("supported", False)
                    and not row.get("gene_group", False)
                    and bool(row["features"][best].get("retrieval_exact"))
                )
                held_rows.append((row, best, gap, allowed))
        for margin in ID_MARGINS:
            tp = fp = fn = 0
            for row, best, gap, allowed in held_rows:
                if not row.get("gold_available", True):
                    continue
                selected = allowed and gap >= margin
                correct = selected and bool(row["labels"][best])
                tp += int(correct)
                fp += int(selected and not correct)
                fn += int(not correct)
            precision = tp / (tp + fp) if tp + fp else None
            recall = tp / (tp + fn) if tp + fn else None
            f1 = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None
            configurations.append(
                {
                    "C": c,
                    "margin": margin,
                    "tp": tp,
                    "fp": fp,
                    "fn": fn,
                    "precision": precision,
                    "recall": recall,
                    "f1": f1,
                    "target_met": precision is not None and precision >= 0.9 and tp > 0,
                }
            )
    eligible = [r for r in configurations if r["target_met"]]
    selected = max(
        eligible or configurations,
        key=lambda r: (
            (r["recall"] or 0) if eligible else (r["f1"] or 0),
            r["f1"] or 0,
            -r["C"],
            r["margin"],
        ),
    )
    x, y = pairwise_examples([r for _, r in examples])
    return LinearHead().fit(x, y, selected["C"]), {
        **selected,
        "source": "train-paper-OOF-only",
        "configurations": configurations,
    }


def train(features, output, audit_path):
    import joblib

    audit = json.loads(Path(audit_path).read_bytes())
    if not audit.get("passed") or audit.get("protected_overlap", 0):
        raise ValueError("Training/test source audit must pass first")
    features, output = Path(features), Path(output)
    manifest = json.loads((features / "manifest.json").read_bytes())
    if (
        manifest.get("split") != "train"
        or manifest["feature_sha256"] != FEATURE_SHA
        or manifest.get("audit_sha256") != digest(Path(audit_path).read_bytes())
        or manifest.get("training_sha256") != audit.get("audited_training_sha256")
        or manifest.get("feature_source_hashes") != source_hashes()
    ):
        raise ValueError("Wrong training features")
    papers = []
    for record in manifest["files"]:
        path = features / record["path"]
        if digest(path.read_bytes()) != record["sha256"]:
            raise ValueError("Training feature file changed")
        papers.append(json.loads(path.read_bytes()))
    assignments = folds([p["pmid"] for p in papers])
    if len(assignments) < 5 or set(assignments) != set(audit["training_pmids"]):
        raise ValueError("Training features must match the audited paper cohort with at least five papers")
    examples = [(p["pmid"], row) for p in papers for row in p["bags"]]
    if not examples:
        raise ValueError("No completely annotated training relation bags")
    types = sorted(set().union(*(set(row["labels"]) for _, row in examples)))
    heads, selections = {}, {}
    tasks = {"existence": lambda row: int(bool(row["labels"]))}
    tasks.update({"type_" + label: (lambda row, label=label: int(label in row["labels"])) for label in types})
    span_examples = [(p["pmid"], row) for p in papers for row in p.get("spans", [])]
    if span_examples:
        tasks["span"] = lambda row: row["label"]
    for name, labeler in tasks.items():
        task_examples = span_examples if name == "span" else examples
        configurations = []
        for c in C_GRID:
            scores, labels = [], []
            for fold in range(5):
                training = [row for pmid, row in task_examples if assignments[pmid] != fold]
                heldout = [row for pmid, row in task_examples if assignments[pmid] == fold]
                if not training or not heldout:
                    continue
                head = LinearHead().fit([r["features"] for r in training], [labeler(r) for r in training], c)
                scores.extend(r.get("1", 0) for r in head.probabilities([r["features"] for r in heldout]))
                labels.extend(labeler(r) for r in heldout)
            configurations.append({"C": c, **threshold(labels, scores, 0.9 if name == "span" else 0.85)})
        eligible = [r for r in configurations if r["target_met"]]
        selected = max(
            eligible or configurations,
            key=lambda r: ((r["recall"] or 0) if eligible else (r["f1"] or 0), (r["f1"] or 0), -r["C"]),
        )
        heads[name] = LinearHead().fit(
            [r["features"] for _, r in task_examples], [labeler(r) for _, r in task_examples], selected["C"]
        )
        selections[name] = {**selected, "configurations": configurations}
        print(
            dumps(
                {
                    "head": name,
                    "precision": selected["precision"],
                    "recall": selected["recall"],
                    "threshold": selected["threshold"],
                }
            ),
            flush=True,
        )
    directed = [row for _, row in examples if row["direction"] is not None]
    if not directed:
        raise ValueError("No officially annotated direction training cases")
    direction_configs = []
    for c in C_GRID:
        correct, total = 0, 0
        for fold in range(5):
            fitting = [r for p, r in examples if assignments[p] != fold and r["direction"] is not None]
            checking = [r for p, r in examples if assignments[p] == fold and r["direction"] is not None]
            if not fitting or not checking:
                continue
            head = LinearHead().fit([r["features"] for r in fitting], [r["direction"] for r in fitting], c)
            scores = head.probabilities([r["features"] for r in checking])
            correct += sum(max(score, key=score.get) == r["direction"] for score, r in zip(scores, checking))
            total += len(checking)
        direction_configs.append(
            {"C": c, "correct": correct, "total": total, "accuracy": correct / max(1, total)}
        )
    direction_choice = max(direction_configs, key=lambda row: (row["accuracy"], -row["C"]))
    heads["direction"] = LinearHead().fit(
        [r["features"] for r in directed], [r["direction"] for r in directed], direction_choice["C"]
    )
    heads["identity"], identity_selection = select_identity(papers, assignments)
    output.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, head in heads.items():
        path = output / (name + ".joblib")
        if path.exists():
            raise ValueError("Model output directory must be new")
        joblib.dump(head, path)
        files[path.name] = digest(path.read_bytes())
    result = {
        "schema": "lightweight-v6-1",
        "feature_sha256": FEATURE_SHA,
        "feature_spec": FEATURE_SPEC,
        "feature_source_hashes": manifest["feature_source_hashes"],
        "files": files,
        "training_split": "train",
        "test_used_for_selection": False,
        "training_pmids": sorted(assignments),
        "fold_assignments": assignments,
        "encoder": manifest["encoder"],
        "candidate_provider": manifest.get("candidate_provider"),
        "training_features_sha256": digest((features / "manifest.json").read_bytes()),
        "audit_sha256": digest(Path(audit_path).read_bytes()),
        "selections": selections,
        "identity_margin": identity_selection["margin"],
        "identity_selection": identity_selection,
        "identity_margin_policy": "C and margin selected by paper-separated train OOF only",
        "direction_selection": {
            **direction_choice,
            "configurations": direction_configs,
            "source": "train-paper-OOF",
        },
        "created_at": now(),
    }
    _write_immutable(output / "manifest.json", result)
    return result
