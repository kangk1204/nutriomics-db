"""R4: deployment candidates, endpoint errors and relation bags use one path.

Annotations enter only after source-only candidates/features have been computed.
Normalization-only corpora never supply relation negatives.
"""

from collections import defaultdict
from copy import deepcopy
from pathlib import Path

from .db import digest


def source_sha():
    return digest(Path(__file__).read_bytes())


def endpoint_features(bag):
    """Quality is observable at deployment; neither IDs nor gold labels are features."""
    result = {"quality:witnesses": len(bag)}
    for role in ("chemical", "gene"):
        mentions = [p[role] for p in bag]
        payloads = [m.get("payload", {}) for m in mentions]
        result["quality:" + role + ":resolved"] = sum(bool(m.get("normalized_id")) for m in mentions) / len(mentions)
        result["quality:" + role + ":span_score"] = max(float(p.get("detection_priority_score", 1)) for p in payloads)
        result["quality:" + role + ":selected"] = max(float(p.get("automatic_selected", True)) for p in payloads)
        for p in payloads:
            result["quality:" + role + ":reason:" + str(p.get("identity_reason"))] = 1.0
        result["quality:" + role + ":species_known"] = float(any(p.get("taxon_id") for p in payloads))
    return result


def bags_for(document, mentions, sentences, values):
    from .features_v5 import pair_candidates
    from .nlp_v2 import parser_for, validate_tokens
    from .embeddings_v6 import pooled_pair_features

    parser = parser_for(values)
    pairs = pair_candidates(sentences, mentions, lambda s: validate_tokens(parser.parse(s), s))
    grouped = defaultdict(list)
    for pair in pairs:
        grouped[tuple(pair[r]["payload"]["mention_cluster_id"] for r in ("chemical", "gene"))].append(pair)
    return [
        {"pairs": bag, "features": {**pooled_pair_features(bag, document["text"], values["_encoder"]), **endpoint_features(bag)}}
        for bag in grouped.values()
    ]


def native_id(mention):
    payload = mention.get("payload", {})
    if payload.get("provided_native_ids"):
        return set(map(str, payload["provided_native_ids"]))
    identifier = mention.get("normalized_id")
    native = payload.get("native_id")
    if identifier and native:
        return {str(native)}
    if identifier and identifier.startswith(("NCBIGene:", "MESH:")):
        return {identifier.split(":", 1)[1]}
    return set()


def training_labels(document, bag):
    """Wrong/unresolved deployment endpoints are explicit errors, never oracle IDs."""
    chemical = set().union(*(native_id(p["chemical"]) for p in bag))
    gene = set().union(*(native_id(p["gene"]) for p in bag))
    labels = set()
    for relation in document.get("relations", []):
        a, b = str(relation["entity1"]), str(relation["entity2"])
        if (a in chemical and b in gene) or (b in chemical and a in gene):
            labels.add(relation["predicate"])
    return sorted(labels)


def prepare(document, dictionaries, values):
    from .candidates_v6 import collect
    from .text import sentence_spans

    # Deliberately strip gold entities/relations and corpus tags at this boundary.
    source = {k: document[k] for k in ("pmid", "text", "sections")}
    sentences = sentence_spans(source["text"], source["sections"])
    mentions, _, hypotheses, clusters = collect(source["text"], sentences, dictionaries, values)
    sentences = values["_clinical_models"].predict(sentences)
    bags = bags_for(source, mentions, sentences, values)
    return source, mentions, hypotheses, clusters, bags


def labelled_rows(document, bags):
    if document.get("relation_annotation_complete") is False:
        raise ValueError("Incomplete relation annotation cannot provide negatives")
    rows = []
    for bag in bags:
        pairs = bag["pairs"]
        rows.append({
            "features": bag["features"],
            "labels": training_labels(document, pairs),
            "witnesses": len(pairs),
            "endpoints": [
                {role: deepcopy(pair[role]) for role in ("chemical", "gene")} for pair in pairs
            ],
        })
    return rows
