"""Training-only span alignment; entity, semantic and native-ID targets differ."""
from collections import defaultdict


def kind(value):
    return "component" if value in {"nutrient", "chemical", "component"} else value


def span_key(mention):
    return kind(mention["kind"]), mention["start"], mention["end"]


def alignment(document, mentions):
    gold = defaultdict(set)
    for entity in document.get("entities", []):
        gold[span_key(entity)].update(str(i) for i in entity.get("native_ids", []))
    complete = document.get("gene_annotation_complete", True)
    return [{"span": span_key(m), "entity_valid": span_key(m) in gold if complete else None,
             "aligned_native_ids": sorted(gold.get(span_key(m), ())),
             "predicted_id_correct": bool(native_ids(m) & gold.get(span_key(m), set()))}
            for m in mentions]


def native_ids(mention):
    payload = mention.get("payload", {})
    if payload.get("provided_native_ids"):
        return set(map(str, payload["provided_native_ids"]))
    value = mention.get("normalized_id")
    if not value:
        return set()
    if payload.get("native_id"):
        return {str(payload["native_id"])}
    return {value.split(":", 1)[-1]}


def bag_targets(document, endpoints):
    """Labels belong to a document bag, never each witness sentence.

    Wrong IDs do not turn a correctly located positive relation into negative.
    Mixed-identity clusters and incomplete annotation are masked, not negatives.
    """
    mapping = defaultdict(set)
    for entity in document.get("entities", []):
        mapping[span_key(entity)].update(map(str, entity.get("native_ids", [])))
    aligned = {role: set().union(*(mapping.get(span_key(p[role]), set()) for p in endpoints))
               for role in ("chemical", "gene")}
    observed = {role: set().union(*(native_ids(p[role]) for p in endpoints))
                for role in ("chemical", "gene")}
    labels, strict, direction = set(), set(), set()
    complete = document.get("relation_annotation_complete") is True
    entity_valid = all(all(span_key(p[r]) in mapping for r in aligned) for p in endpoints)
    groups = {r: [mapping[span_key(p[r])] for p in endpoints if mapping.get(span_key(p[r]))] for r in aligned}
    ambiguous = any(values and not set.intersection(*map(set, values)) for values in groups.values())
    for relation in document.get("relations", []):
        a, b = str(relation["entity1"]), str(relation["entity2"])
        match = ((a in aligned["chemical"] and b in aligned["gene"])
                 or (b in aligned["chemical"] and a in aligned["gene"]))
        if match:
            labels.add(relation["predicate"])
            if relation.get("subject"):
                direction.add("chemical_to_gene" if str(relation["subject"]) in aligned["chemical"] else "gene_to_chemical")
            elif relation.get("direction_annotated"):
                direction.add("undirected")
        if ((a in observed["chemical"] and b in observed["gene"])
                or (b in observed["chemical"] and a in observed["gene"])):
            strict.add(relation["predicate"])
    return {"semantic_labels": sorted(labels), "strict_native_labels": sorted(strict),
            "semantic_supervised": complete and entity_valid and not ambiguous,
            "entity_valid": entity_valid, "identity_cluster_conflict": ambiguous,
            "direction": next(iter(direction)) if len(direction) == 1 and not ambiguous else None,
            "labels_copied_to_sentences": False}


def gold_keys(document):
    return {(str(document["pmid"]), tuple(sorted((str(r["entity1"]), str(r["entity2"])))), r["predicate"])
            for r in document.get("relations", [])
            if r["predicate"] in {"Association", "Bind", "Negative_Correlation", "Positive_Correlation"}
            and any(e["kind"] == "gene" and str(r["entity1"]) in set(map(str, e.get("native_ids", [])))
                    or e["kind"] == "gene" and str(r["entity2"]) in set(map(str, e.get("native_ids", [])))
                    for e in document.get("entities", []))
            and any(kind(e["kind"]) == "component" and (str(r["entity1"]) in set(map(str, e.get("native_ids", [])))
                    or str(r["entity2"]) in set(map(str, e.get("native_ids", []))))
                    for e in document.get("entities", []))}


def predicted_keys(pmid, endpoints, labels):
    result = set()
    for pair in endpoints:
        for a in native_ids(pair["chemical"]):
            for b in native_ids(pair["gene"]):
                result.update((str(pmid), tuple(sorted((a, b))), t) for t in labels)
    return result
