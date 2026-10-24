"""Frozen, paper-grouped public evaluation. Public gold is never nutrition gold.

Native corpus relation labels are kept in the denominator, including unsupported
types and document-level/cross-sentence relations. Oracle relations and end-to-end
results are distinct runs. No public score implies nutritional clinical validity.
"""

import argparse
import csv
import io
import json
import random
import re
import tarfile
import zipfile
from collections import Counter
from pathlib import Path

from .db import connect, digest, dumps, now, uid
from .evaluation import _metrics
from .public_corpora import CATALOG, prepare_public, validate_span
from .text import sentence_spans

PROJECTION = {
    "version": "biored-projection-v1",
    "predicates": {"increases": "Positive_Correlation", "decreases": "Negative_Correlation",
                   "associated_with": "Association", "binds": "Bind"},
    "association_direction": {"positive": "Positive_Correlation", "negative": "Negative_Correlation"},
    "note": "Frozen operational label projection, not biological synonym equivalence. Generic affects is unsupported.",
    "negative_claims": "negated/null/speculative/background/abstained claims are not projected as positive findings",
}

# Applicability comes from the pinned native-corpus manifest, never from whether
# a paper happens to contain positive annotations. Empty gold within an annotated
# task is a true negative; absence of that task's annotations is unmeasured.
TASK_ENDPOINTS = {
    "gene_detection": {"gene_detection"}, "normalization": {"normalization"},
    "chemical_gene_relation": {"relation"}, "direction": {"direction"},
    "negation_scope": {"scopes", "scopes_exact", "scopes:negation"},
    "speculation_scope": {"scopes", "scopes_exact", "scopes:speculation"},
    "participants": {"annotated_spans", "annotated_spans:participants"},
    "interventions": {"annotated_spans", "annotated_spans:interventions"},
    "outcomes": {"annotated_spans", "annotated_spans:outcomes"},
}


def _annotation_applicability(receipt):
    tasks = receipt.get("tasks")
    if (not isinstance(tasks, list) or any(task not in TASK_ENDPOINTS for task in tasks)
            or len(set(tasks)) != len(tasks)
            or set(tasks) != set(CATALOG[receipt["corpus"]]["tasks"])):
        raise ValueError("Pinned corpus annotation task coverage is missing or inconsistent")
    endpoints = set().union(*(TASK_ENDPOINTS[task] for task in tasks))
    return {"tasks": sorted(tasks), "evaluated_endpoints": sorted(endpoints),
            "not_evaluated_endpoints": sorted(set().union(*TASK_ENDPOINTS.values()) - endpoints),
            "basis": "Pinned native corpus tasks; empty gold is negative only within annotated tasks"}


def _paper_annotation_tasks(gold, receipt, gold_path):
    declared = set(receipt["tasks"])
    if receipt["corpus"] != "ebmnlp" or receipt["source_member"] != "aggregated starting spans":
        return {paper["pmid"]: declared for paper in gold["documents"]}, "pinned corpus-wide annotation protocol"
    # EBM-NLP has papers lacking a label file for an individual PICO task. Read
    # archive filenames only, never annotation values or text, to distinguish a
    # missing file (unannotated) from a present all-zero file (true negative).
    raw = (Path(gold_path).parent / "source.release").read_bytes()
    if digest(raw) != receipt["source_sha256"]:
        raise ValueError("Native task coverage source differs from pinned release")
    if zipfile.is_zipfile(io.BytesIO(raw)):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
    else:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as archive:
            names = archive.getnames()
    partition = "test" if gold["split"] == "test" else "train"
    coverage = {paper["pmid"]: set() for paper in gold["documents"]}
    for name in names:
        match = re.search(r"aggregated/starting_spans/(participants|interventions|outcomes)/"
                          r"(train|test)/([^/]+)\.ann$", name)
        if match and match[2] == partition and match[3].removesuffix(".AGGREGATED") in coverage:
            coverage[match[3].removesuffix(".AGGREGATED")].add(match[1])
    if any(not tasks or tasks - declared for tasks in coverage.values()):
        raise ValueError("Native paper annotation task coverage is missing or inconsistent")
    return coverage, "SHA-verified native archive annotation filenames only; no annotation values read"


def evaluator_source_hashes():
    root = Path(__file__).resolve().parents[1]
    files = ["nutri_literature/benchmarks.py", "nutri_literature/public_corpora.py",
             "nutri_literature/evaluation.py", "nutri_literature/promotion.py",
             "config/benchmark-acceptance-v2.json"]
    return {name: digest((root / name).read_bytes()) for name in files}


def _receipt(settings, kind, identifier, value):
    serialized = dumps(value)
    with connect(settings, durable=True) as con:
        con.execute("INSERT INTO audit VALUES(?,?,?,?,?)", (
            uid(), kind, None, dumps({"id": identifier, "sha256": digest(serialized)}), now(),
        ))
    return digest(serialized)


def verify_receipt(settings, kind, value):
    if settings is None or not value.get("id"):
        return False
    wanted = digest(dumps(value))
    with connect(settings) as con:
        records = [json.loads(row[0]) for row in con.execute("SELECT detail FROM audit WHERE kind=?", (kind,))]
    return any(record.get("id") == value["id"] and record.get("sha256") == wanted for record in records)


def _write_immutable(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(dumps(value))
    return str(path)


def _validate_release_receipt(settings, path, receipt):
    identity = Path(path).parent.name
    with connect(settings) as con:
        records = [json.loads(row[0]) for row in con.execute(
            "SELECT detail FROM audit WHERE kind='public_corpus_prepared'",
        )]
    if not any(record.get("id") == identity and record.get("sha256") == digest(dumps(receipt))
               for record in records):
        raise ValueError("Public release receipt differs from durable imported-source hash")


def _load_inputs(settings, path):
    path = Path(path)
    raw, manifest_raw = path.read_bytes(), (path.parent / "manifest.json").read_bytes()
    inputs, receipt = json.loads(raw), json.loads(manifest_raw)
    _validate_release_receipt(settings, path, receipt)
    if inputs.get("schema_version") != "literature-public-inputs-v1":
        raise ValueError("Expected pinned literature-public-inputs-v1")
    if digest(raw) != receipt.get("inputs_sha256"):
        raise ValueError("Public extractor inputs differ from release receipt")
    seen = set()
    for document in inputs["documents"]:
        if document["pmid"] in seen or digest(document["text"]) != document["source_hash"]:
            raise ValueError("Duplicate public PMID or changed source")
        seen.add(document["pmid"])
        if any(key in document for key in ["entities", "relations", "scopes", "cues", "annotated_spans"]):
            raise ValueError("Extractor input manifest must not contain gold labels")
    return inputs, receipt, digest(raw)


def _load_gold(settings, path):
    path = Path(path)
    raw = path.read_bytes()
    receipt = json.loads((path.parent / "manifest.json").read_bytes())
    _validate_release_receipt(settings, path, receipt)
    gold = json.loads(raw)
    if (gold.get("schema_version") != "literature-public-gold-v1"
            or digest(raw) != receipt.get("gold_sha256")
            or gold.get("manifest", {}).get("annotation_status") != "published_corpus_annotations"):
        raise ValueError("Expected unchanged published-corpus gold; predictions/manual-pending cannot be labels")
    return gold, receipt, digest(raw)


def _crosswalk(settings, dictionaries, path=None):
    rows, sources = [], []
    manifest = dictionaries.manifest
    if not manifest.get("fallback") and manifest.get("path"):
        snapshot = settings.dictionaries / manifest["path"]
        raw = snapshot.read_bytes()
        if digest(raw) != manifest["sha256"]:
            raise ValueError("HGNC crosswalk snapshot hash differs")
        records = csv.DictReader(raw.decode("utf-8").splitlines(), delimiter="\t")
        rows.extend({"kind": "gene", "system_id": record["hgnc_id"], "native_id": record["entrez_id"]}
                    for record in records if record.get("hgnc_id") and record.get("entrez_id"))
        sources.append({"source": manifest.get("source"), "sha256": digest(raw), "mapping": "HGNC->NCBI Gene"})
    if path is not None:
        raw = Path(path).read_bytes()
        external = json.loads(raw)
        if (external.get("schema_version") != "literature-id-crosswalk-v1"
                or not external.get("source_references")):
            raise ValueError("Crosswalk requires official source references and explicit schema")
        for row in external["rows"]:
            if set(row) != {"kind", "system_id", "native_id"} or row["kind"] not in {"gene", "component"}:
                raise ValueError("Invalid native ID crosswalk row")
        rows.extend(external["rows"])
        sources.append({"source_references": external["source_references"], "sha256": digest(raw)})
    mapping = {}
    for row in rows:
        key = (row["kind"], row["system_id"])
        mapping.setdefault(key, set()).add(row["native_id"])
    return mapping, {"sha256": digest(dumps({"rows": rows, "sources": sources})), "sources": sources,
                     "gene_rows": sum(row["kind"] == "gene" for row in rows),
                     "component_rows": sum(row["kind"] == "component" for row in rows)}


def _provenance(settings, engine, crosswalk_path=None):
    from .dictionaries import load_dictionaries
    from .engines import engine_manifest_validation, engine_rules_version, extract_engine

    dictionaries = load_dictionaries(settings, engine=engine)
    probe = extract_engine("Vitamin D decreases VDR expression in human cells.", [], dictionaries,
                           metadata={}, engine=engine)
    errors = engine_manifest_validation(probe, engine)
    if errors:
        raise ValueError("Cannot freeze unavailable/nonproduction engine: " + ",".join(errors))
    _, crosswalk_manifest = _crosswalk(settings, dictionaries, crosswalk_path)
    parser = probe.get("manifest", {}).get("parser", {})
    return dictionaries, {"engine_version": engine, "rules_version": engine_rules_version(engine),
                          "dictionary_version": dictionaries.version, "dictionary_manifest": dictionaries.manifest,
                          "parser": parser, "crosswalk": crosswalk_manifest,
                          "projection_sha256": digest(dumps(PROJECTION)),
                          "evaluator_source_hashes": evaluator_source_hashes()}


def freeze_benchmark(settings, input_path, engine="heuristic-v2", crosswalk_path=None):
    inputs, receipt, input_sha = _load_inputs(settings, input_path)
    _, provenance = _provenance(settings, engine, crosswalk_path)
    frozen = {"schema_version": "literature-public-freeze-v1", "id": uid(), "frozen_at": now(),
              "corpus": inputs["corpus"], "release_sha256": receipt["source_sha256"],
              "development_input_sha256": input_sha, "provenance": provenance, "projection": PROJECTION,
              "test_policy": "one attempt per release/frozen provenance/mode, including failed attempts",
              "pretraining_overlap": receipt.get("pretraining_overlap", "unknown")}
    path = Path(input_path).parent / ("freeze-" + frozen["id"] + ".json")
    _write_immutable(path, frozen)
    _receipt(settings, "public_benchmark_freeze", frozen["id"], frozen)
    return {**frozen, "freeze_path": str(path)}


def _load_freeze(settings, freeze_path, inputs, provenance):
    if freeze_path is None:
        if inputs["split"] == "test":
            raise ValueError("Official test requires a frozen engine before prediction")
        return None
    frozen = json.loads(Path(freeze_path).read_bytes())
    if not verify_receipt(settings, "public_benchmark_freeze", frozen):
        raise ValueError("Freeze receipt differs from durable recorded hash")
    if (frozen["corpus"] != inputs["corpus"]
            or frozen["release_sha256"] != inputs["manifest"]["source_sha256"]
            or frozen["provenance"] != provenance):
        raise ValueError("Engine, dictionary, parser, ontology/mapping, or corpus changed after freeze")
    return frozen


def _claim_fields(event):
    payload = event.get("payload", {})
    if isinstance(payload, str):
        payload = json.loads(payload)
    nested = payload.get("event", {}) if isinstance(payload, dict) else {}
    return {**payload, **nested, **event}


def _public_kind(kind):
    return "component" if kind in {"nutrient", "component", "chemical"} else kind


def _project_event(event, document, mapping, oracle=False):
    fields = _claim_fields(event)
    source_predicate = fields.get("predicate")
    if source_predicate is None:
        source_predicate = {"increase": "increases", "decrease": "decreases", "association": "associated_with"}.get(
            fields.get("direction"), "binds" if fields.get("relation_type") == "binding" else None,
        )
    predicate = PROJECTION["predicates"].get(source_predicate)
    if source_predicate == "associated_with":
        predicate = PROJECTION["association_direction"].get(fields.get("association_direction"), predicate)
    if (predicate is None or fields.get("status", "candidate") in {"abstained", "background"}
            or fields.get("polarity", "positive") != "positive"
            or fields.get("negation") or fields.get("speculative", fields.get("speculation", False))):
        return None
    endpoints = []
    for role in ["subject", "object"]:
        mention = fields.get(role + "_mention", fields.get("controller" if role == "subject" else "theme", {}))
        if not mention and isinstance(fields.get(role), dict):
            mention = fields[role]
        kind = _public_kind(mention.get("kind"))
        endpoint = fields.get(role)
        if isinstance(endpoint, dict):
            endpoint = endpoint.get("normalized_id")
        if not kind:
            if endpoint in {value for e in document.get("entities", []) for value in e.get("native_ids", [])
                            if e["kind"] == "gene"} or str(endpoint).startswith("HGNC:"):
                kind = "gene"
            elif str(endpoint).startswith(("CHEBI:", "local:")):
                kind = "component"
            elif oracle:
                kind = next((e["kind"] for e in document["entities"]
                             if endpoint in e.get("native_ids", [])), None)
        if kind not in {"gene", "component"} or not endpoint:
            return None
        native = {str(endpoint)} if oracle else mapping.get((kind, endpoint), set())
        # Ambiguous crosswalk is abstention, not best-gold matching.
        if len(native) != 1:
            return None
        endpoints.append((kind, next(iter(native))))
    if {kind for kind, _ in endpoints} != {"gene", "component"}:
        return None
    return {"entity1": endpoints[0][1], "entity2": endpoints[1][1], "predicate": predicate,
            "subject": endpoints[0][1], "source_event": event}


def predict_public(settings, input_path, engine="heuristic-v2", freeze_path=None,
                   mode="end_to_end", crosswalk_path=None, ablation_stage=None):
    from .engines import extract_engine, validate_source_spans

    if mode not in {"end_to_end", "gold_entities"}:
        raise ValueError("Public prediction mode must distinguish detector and oracle")
    inputs, receipt, input_sha = _load_inputs(settings, input_path)
    if ablation_stage not in {None, "entities_v2_relations_v1", "syntax_without_context"}:
        raise ValueError("Unknown development ablation stage")
    if ablation_stage and (inputs["split"] == "test" or engine != "heuristic-v2" or freeze_path is not None):
        raise ValueError("Ablations are development-only and cannot use protected frozen test")
    dictionaries, provenance = _provenance(settings, engine, crosswalk_path)
    frozen = _load_freeze(settings, freeze_path, inputs, provenance)
    if ablation_stage:
        provenance = {**provenance, "ablation_stage": ablation_stage}
    mapping, _ = _crosswalk(settings, dictionaries, crosswalk_path)
    # Only gold mention inputs are used for the oracle. Relation/scope labels do
    # not reach the extractor; detector mode never opens gold.json at all.
    oracle_documents = {}
    if mode == "gold_entities":
        raw = (Path(input_path).parent / "oracle_entities.json").read_bytes()
        oracle = json.loads(raw)
        if (digest(raw) != receipt.get("oracle_entities_sha256")
                or oracle.get("schema_version") != "literature-public-oracle-entities-v1"):
            raise ValueError("Gold entity oracle input differs from imported corpus receipt")
        if any(set(document) != {"pmid", "source_hash", "entities"} for document in oracle["documents"]):
            raise ValueError("Oracle input must not contain relation/scope/result gold labels")
        oracle_documents = {d["pmid"]: d for d in oracle["documents"]}
        if set(oracle_documents) != {d["pmid"] for d in inputs["documents"]}:
            raise ValueError("Oracle/input paper partitions differ")
        for document in inputs["documents"]:
            if oracle_documents[document["pmid"]]["source_hash"] != document["source_hash"]:
                raise ValueError("Oracle entity source hash differs")
    key = digest(dumps({"release": receipt["source_sha256"], "corpus": inputs["corpus"],
                        "engine": engine, "split": inputs["split"], "mode": mode}))
    lock = settings.home / "public_benchmarks" / "test_attempts" / (key + ".json")
    if inputs["split"] == "test":
        _write_immutable(lock, {"started_at": now(), "freeze_id": frozen["id"],
                               "mode": mode, "note": "attempt consumed even if prediction fails"})
    predictions = []
    for document in inputs["documents"]:
        metadata = {}
        if ablation_stage == "syntax_without_context":
            metadata["_ablation_stage"] = ablation_stage
        if mode == "gold_entities":
            metadata["_gold_entities"] = [entity for entity in oracle_documents[document["pmid"]]["entities"]
                                           if entity["kind"] in {"gene", "component"}]
        result = extract_engine(document["text"], document["sections"], dictionaries,
                                metadata=metadata, engine=engine)
        if ablation_stage == "entities_v2_relations_v1":
            from .extraction import _relations
            result["events"] = _relations(sentence_spans(document["text"], document["sections"]),
                                           result["mentions"], metadata)
            result["benchmark_scopes"] = []
        if ablation_stage == "syntax_without_context" and result.get("manifest", {}).get("ablation_stage") != ablation_stage:
            raise ValueError("Extractor did not attest context component was disabled for ablation")
        if mode == "gold_entities" and not (
                result.get("oracle_entities_used") is True or result.get("manifest", {}).get("oracle") is True):
            raise ValueError("Extractor did not attest gold-entity relation-only execution")
        validate_source_spans(result.get("mentions", []), document["text"])
        validate_source_spans(result.get("events", result.get("assertions", [])), document["text"])
        entity_labels = oracle_documents.get(document["pmid"], {}).get("entities", [])
        labels_for_ids = {"entities": entity_labels}
        events = result.get("events", result.get("assertions", []))
        projected = [projected for event in events
                     if (projected := _project_event(event, labels_for_ids, mapping, mode == "gold_entities"))]
        predictions.append({"pmid": document["pmid"], "group_id": document["group_id"],
                            "source_hash": document["source_hash"], "entities": result.get("mentions", []),
                            "events": events, "relations": projected,
                            "scopes": result.get("benchmark_scopes", []),
                            "annotated_spans": result.get("annotated_spans", []),
                            "status": result.get("status", "processed"), "manifest": result.get("manifest", {}),
                            "source_level": document["source_level"]})
    run = {"schema_version": "literature-public-prediction-v1", "id": uid(), "predicted_at": now(),
           "corpus": inputs["corpus"], "split": inputs["split"], "input_sha256": input_sha,
           "release_sha256": receipt["source_sha256"], "mode": mode, "provenance": provenance,
           "freeze_id": frozen["id"] if frozen else None, "projection": PROJECTION,
           "oracle_entities_used": mode == "gold_entities", "documents": predictions,
           "ablation_stage": ablation_stage,
           "status": "predictions_only_no_relation_gold_read"}
    path = Path(input_path).parent / ("predictions-" + run["id"] + ".json")
    _write_immutable(path, run)
    _receipt(settings, "public_benchmark_prediction", run["id"], run)
    return {"prediction_run": str(path), "id": run["id"], "documents": len(predictions),
            "mode": mode, "provenance": provenance, "freeze_id": run["freeze_id"], "status": run["status"]}


def _relation_sets(document, predicted):
    pmid = document["pmid"]
    ids = {}
    for entity in document["entities"]:
        for value in entity.get("native_ids", []):
            ids.setdefault(value, set()).add(entity["kind"])
    gold = set()
    directed = set()
    for relation in document["relations"]:
        left, right = relation["entity1"], relation["entity2"]
        if not (("gene" in ids.get(left, set()) and "component" in ids.get(right, set()))
                or ("component" in ids.get(left, set()) and "gene" in ids.get(right, set()))):
            continue
        key = (pmid, tuple(sorted((left, right))), relation["predicate"])
        gold.add(key)
        if relation.get("subject"):
            directed.add((*key, relation["subject"]))
    observed = set()
    observed_directed = set()
    directed_pairs = {(row[0], row[1]) for row in directed}
    for relation in predicted["relations"]:
        key = (pmid, tuple(sorted((relation["entity1"], relation["entity2"]))), relation["predicate"])
        observed.add(key)
        # No direction gold on a pair means do not invent a directional label.
        if (key[0], key[1]) in directed_pairs:
            observed_directed.add((*key, relation["subject"]))
    return gold, observed, directed, observed_directed


def _tokens(text):
    return [(match.start(), match.end()) for match in re.finditer(r"\S+", text)]


def _span_sets(document, predicted, field, label_field, annotated_labels=None):
    expected, observed = set(), set()
    tokens = _tokens(document["text"])
    for output, spans in [(expected, document.get(field, [])), (observed, predicted.get(field, []))]:
        for span in spans:
            validate_span(document["text"], span)
            label = span.get(label_field)
            if label == "hedge":
                label = "speculation"
            if label not in {"negation", "speculation", "participants", "interventions", "outcomes"}:
                continue
            if annotated_labels is not None and label not in annotated_labels:
                continue
            cue = _cue_key(span) if field == "scopes" else None
            for index, (start, end) in enumerate(tokens):
                if start < span["end"] and end > span["start"]:
                    output.add((document["pmid"], label, cue, index))
    return expected, observed


def _cue_key(scope):
    if scope.get("cue_spans"):
        return tuple(sorted((span["start"], span["end"]) for span in scope["cue_spans"]))
    if "cue_start" in scope and "cue_end" in scope:
        return ((scope["cue_start"], scope["cue_end"]),)
    return None


def _exact_scopes(document, predicted, annotated_labels=None):
    expected, observed = set(), set()
    for result, values in [(expected, document.get("scopes", [])), (observed, predicted.get("scopes", []))]:
        for span in values:
            kind = "speculation" if span.get("kind") == "hedge" else span.get("kind")
            if kind in {"negation", "speculation"} and (annotated_labels is None or kind in annotated_labels):
                result.add((document["pmid"], kind, _cue_key(span), span["start"], span["end"]))
    return expected, observed


def _provenance_counts(document, predicted):
    good = total = 0
    for field in ["entities", "events", "scopes", "annotated_spans"]:
        for value in predicted.get(field, []):
            if field == "events":
                quotations = value.get("evidence_quotes", [value])
                for quote in quotations:
                    total += 1
                    try:
                        validate_span(document["text"], quote, "quote")
                        good += 1
                    except ValueError:
                        pass
            else:
                total += 1
                try:
                    validate_span(document["text"], value)
                    good += 1
                except ValueError:
                    pass
    return good, total


def _paper_counts(document, prediction, mapping, mode, mappable_ids=None, annotated_tasks=None):
    # The public evaluator always passes manifest-declared tasks. Keeping the
    # optional default supports the private software-fixture helper only.
    tasks = set(TASK_ENDPOINTS) if annotated_tasks is None else set(annotated_tasks)
    if mappable_ids is None:
        mappable_ids = {kind: {value for (mapped_kind, _), native in mapping.items()
                              if mapped_kind == kind for value in native} for kind in ["gene", "component"]}
    counts, relation = {"annotation_tasks": sorted(tasks)}, None
    if "chemical_gene_relation" in tasks or "direction" in tasks:
        relation = _relation_sets(document, prediction)
        if "chemical_gene_relation" in tasks:
            counts["relation"] = _metrics(relation[0], relation[1])
        if "direction" in tasks:
            counts["direction"] = _metrics(relation[2], relation[3])
    if "gene_detection" in tasks:
        expected_gene = {(document["pmid"], entity["start"], entity["end"])
                         for entity in document["entities"] if entity["kind"] == "gene"}
        observed_gene = {(document["pmid"], entity["start"], entity["end"])
                         for entity in prediction["entities"] if entity["kind"] == "gene"}
        counts["gene_detection"] = _metrics(expected_gene, observed_gene)
    for field, label in [("scopes", "kind"), ("annotated_spans", "label")]:
        annotated_labels = ({kind for kind in ["negation", "speculation"] if kind + "_scope" in tasks}
                            if field == "scopes" else tasks & {"participants", "interventions", "outcomes"})
        if not annotated_labels:
            continue
        expected, observed = _span_sets(document, prediction, field, label, annotated_labels)
        counts[field] = _metrics(expected, observed)
        for subtype in sorted(annotated_labels):
            counts[field + ":" + subtype] = _metrics({row for row in expected if row[1] == subtype},
                                                    {row for row in observed if row[1] == subtype})
        if field == "scopes":
            counts["scopes_exact"] = _metrics(*_exact_scopes(document, prediction, annotated_labels))
    if "normalization" in tasks:
        counts["normalization"], counts["normalization_by_kind"] = _normalization_counts(
            document, prediction, mapping, mode, mappable_ids,
        )
    # All predictions retain source validation, including unannotated endpoints.
    counts["provenance_valid"], counts["provenance_total"] = _provenance_counts(document, prediction)
    if "chemical_gene_relation" in tasks:
        sentences = sentence_spans(document["text"], document["sections"])
        within_sentence = 0
        for key in relation[0]:
            left, right = key[1]
            if any(all(any(value in entity.get("native_ids", []) and sentence["start"] <= entity["start"]
                           and entity["end"] <= sentence["end"] for entity in document["entities"])
                           for value in (left, right)) for sentence in sentences):
                within_sentence += 1
        counts["relation_gold_total"] = len(relation[0])
        counts["relation_gold_same_sentence"] = within_sentence
        counts["relation_gold_types"] = dict(Counter(row[2] for row in relation[0]))
        counts["relation_by_type"] = {kind: _metrics({row for row in relation[0] if row[2] == kind},
                                                   {row for row in relation[1] if row[2] == kind})
                                      for kind in {row[2] for row in relation[0] | relation[1]}}
    return counts


def _normalization_counts(document, prediction, mapping, mode, mappable_ids):
    normalized, normalization_correct, mapped, total_gold, predicted_mentions = 0, 0, 0, 0, 0
    norm_by_kind = {kind: {"normalized_predictions": 0, "correct": 0, "gold_mentions": 0,
                           "crosswalk_mappable_gold": 0, "predicted_mentions": 0}
                    for kind in ["gene", "component"]}
    for entity in document["entities"]:
        if entity["kind"] not in {"gene", "component"}:
            continue
        native_ids = set(entity.get("native_ids", []))
        if not native_ids:
            continue  # ID is not annotated: do not invent a normalization target.
        total_gold += 1
        norm_by_kind[entity["kind"]]["gold_mentions"] += 1
        if native_ids & mappable_ids[entity["kind"]]:
            mapped += 1
            norm_by_kind[entity["kind"]]["crosswalk_mappable_gold"] += 1
    deduplicated_entities = {(e["kind"], e["start"], e["end"], e.get("normalized_id")): e
                             for e in prediction["entities"]}
    matched_normalization_spans = set()
    for entity in deduplicated_entities.values():
        kind = _public_kind(entity["kind"])
        if kind not in {"gene", "component"}:
            continue
        predicted_mentions += 1
        norm_by_kind[kind]["predicted_mentions"] += 1
        if not entity.get("normalized_id"):
            continue
        normalized += 1
        norm_by_kind[kind]["normalized_predictions"] += 1
        native = ({entity["normalized_id"]} if mode == "gold_entities" else
                  mapping.get((kind, entity["normalized_id"]), set()))
        matching = [gold for gold in document["entities"] if gold["kind"] == kind
                    and gold["start"] == entity["start"] and gold["end"] == entity["end"]]
        matched_key = (kind, entity["start"], entity["end"])
        if (matched_key not in matched_normalization_spans and len(native) == 1
                and any(native & set(gold.get("native_ids", [])) for gold in matching)):
            matched_normalization_spans.add(matched_key)
            normalization_correct += 1
            norm_by_kind[kind]["correct"] += 1
    return ({"normalized_predictions": normalized, "correct": normalization_correct,
             "gold_mentions": total_gold, "crosswalk_mappable_gold": mapped,
             "predicted_mentions": predicted_mentions}, norm_by_kind)


def _from_counts(tp, fp, fn):
    # Avoid quadratic fake set allocation when accumulating a large token corpus.
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall,
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None}


def _aggregate(papers, endpoint):
    return _from_counts(*(sum(paper.get(endpoint, {}).get(field, 0) for paper in papers)
                          for field in ["tp", "fp", "fn"]))


def _paper_intervals(papers, endpoint, iterations=2000, seed=20261003):
    papers = [paper for paper in papers if endpoint in paper]
    point = _aggregate(papers, endpoint)
    if point["tp"] + point["fp"] + point["fn"] == 0:
        return {key + "_ci95": None for key in ["precision", "recall", "f1"]}
    rng, samples = random.Random(seed), {key: [] for key in ["precision", "recall", "f1"]}
    for _ in range(iterations):
        score = _aggregate(rng.choices(papers, k=len(papers)), endpoint)
        for key in samples:
            if score[key] is not None:
                samples[key].append(score[key])
    output = {"ci_method": "paper bootstrap percentile 95%; paper is the sampling unit",
              "bootstrap_iterations": iterations, "bootstrap_seed": seed}
    for key, values in samples.items():
        values.sort()
        output[key + "_ci95"] = ({"lower": values[int(.025 * (len(values) - 1))],
                                    "upper": values[int(.975 * (len(values) - 1))],
                                    "valid_draws": len(values), "papers": len(papers)} if values else None)
    return output


def _load_run(settings, prediction_run, receipt):
    run = json.loads(Path(prediction_run).read_bytes())
    if (run.get("schema_version") != "literature-public-prediction-v1"
            or not verify_receipt(settings, "public_benchmark_prediction", run)):
        raise ValueError("Public prediction run differs from durable receipt")
    if (run["input_sha256"] != receipt["inputs_sha256"]
            or run["release_sha256"] != receipt["source_sha256"]):
        raise ValueError("Public prediction belongs to another release/partition")
    return run


def evaluate_public(settings, gold_path, prediction_run, freeze_path=None, crosswalk_path=None, reuse=False):
    gold, receipt, gold_sha = _load_gold(settings, gold_path)
    applicability = _annotation_applicability(receipt)
    annotated_tasks = set(applicability["tasks"])
    paper_tasks, coverage_basis = _paper_annotation_tasks(gold, receipt, gold_path)
    applicability["paper_coverage_basis"] = coverage_basis
    applicability["paper_task_coverage_sha256"] = digest(dumps({pmid: sorted(tasks) for pmid, tasks in paper_tasks.items()}))
    applicability["annotated_documents_by_task"] = {
        task: sum(task in tasks for tasks in paper_tasks.values()) for task in sorted(annotated_tasks)
    }
    run = _load_run(settings, prediction_run, receipt)
    if gold["split"] != run["split"] or gold["corpus"] != run["corpus"]:
        raise ValueError("Public gold and prediction partition/corpus differ")
    if gold["split"] == "test" and not run.get("freeze_id"):
        raise ValueError("Test prediction was not frozen")
    if freeze_path is not None:
        frozen = json.loads(Path(freeze_path).read_bytes())
        if (not verify_receipt(settings, "public_benchmark_freeze", frozen)
                or frozen["id"] != run["freeze_id"] or frozen["provenance"] != run["provenance"]):
            raise ValueError("Evaluation freeze does not match prediction")
    evaluator_hashes = evaluator_source_hashes()
    if gold["split"] == "test" and run["provenance"].get("evaluator_source_hashes") != evaluator_hashes:
        raise ValueError("Protected evaluator source changed after frozen test prediction")
    from .dictionaries import load_dictionaries
    from .engines import validate_source_spans
    dictionaries = load_dictionaries(settings, engine=run["provenance"]["engine_version"])
    mapping, crosswalk_manifest = _crosswalk(settings, dictionaries, crosswalk_path)
    if crosswalk_manifest != run["provenance"]["crosswalk"]:
        raise ValueError("Evaluation ID crosswalk changed after prediction")
    if reuse:
        for path in sorted(Path(gold_path).parent.glob("evaluation-*.json")):
            cached = json.loads(path.read_bytes())
            if (cached.get("prediction_run_id") == run["id"] and cached.get("gold_sha256") == gold_sha
                    and cached.get("evaluator_source_hashes") == evaluator_hashes
                    and cached.get("provenance") == run["provenance"]
                    and verify_receipt(settings, "public_benchmark_evaluation", cached)):
                return {**cached, "output_path": str(path)}
    predictions = {d["pmid"]: d for d in run["documents"]}
    if len(predictions) != len(run["documents"]) or set(predictions) != {d["pmid"] for d in gold["documents"]}:
        raise ValueError("Public gold/prediction paper sets differ")
    per_paper = []
    mappable_ids = {kind: {value for (mapped_kind, _), native in mapping.items()
                          if mapped_kind == kind for value in native} for kind in ["gene", "component"]}
    for document in gold["documents"]:
        predicted = predictions[document["pmid"]]
        if document["source_hash"] != predicted["source_hash"]:
            raise ValueError("Public prediction original text differs")
        validate_source_spans(predicted, document["text"])
        counts = _paper_counts(document, predicted, mapping, run["mode"], mappable_ids, paper_tasks[document["pmid"]])
        per_paper.append({"pmid": document["pmid"], "group_id": document["group_id"],
                          "source_level": document["source_level"], **counts})
    endpoints = sorted({key for paper in per_paper for key, value in paper.items()
                        if isinstance(value, dict) and "tp" in value})
    metrics = {endpoint: {**_aggregate(per_paper, endpoint), **_paper_intervals(per_paper, endpoint)}
               for endpoint in endpoints}
    applicability["annotated_documents_by_endpoint"] = {
        endpoint: sum(endpoint in paper for paper in per_paper) for endpoint in endpoints
    }
    by_type = {} if "chemical_gene_relation" in annotated_tasks else None
    for kind in sorted({key for paper in per_paper for key in paper.get("relation_by_type", {})}):
        by_type[kind] = _from_counts(*(sum(paper["relation_by_type"].get(kind, {}).get(field, 0)
                                         for paper in per_paper) for field in ["tp", "fp", "fn"]))
    n = _aggregate_normalization(per_paper, run["mode"]) if "normalization" in annotated_tasks else None
    total_provenance = sum(p["provenance_total"] for p in per_paper)
    valid_provenance = sum(p["provenance_valid"] for p in per_paper)
    report = {"schema_version": "literature-public-evaluation-v1", "id": uid(), "evaluated_at": now(),
              "status": "measured_on_published_public_gold", "corpus": gold["corpus"], "split": gold["split"],
              "official_split": receipt["official_split"], "complete_partition": receipt["complete_partition"],
              "preregistered_split": receipt.get("preregistered_split", False),
              "partition_sha256": receipt.get("partition_sha256"),
              "mode": run["mode"], "prediction_run_id": run["id"], "gold_sha256": gold_sha,
              "ablation_stage": run.get("ablation_stage"),
              "input_sha256": receipt["inputs_sha256"], "release_sha256": receipt["source_sha256"],
              "provenance": run["provenance"], "freeze_id": run["freeze_id"], "documents": len(per_paper),
              "evaluator_source_hashes": evaluator_hashes,
              "annotation_applicability": applicability,
              "metrics": metrics, "normalization": n, "relation_by_type": by_type, "per_paper": per_paper,
              "scope_by_source": {level: {key: {**_aggregate([p for p in per_paper if p["source_level"] == level], key),
                                                **_paper_intervals([p for p in per_paper if p["source_level"] == level], key)}
                                          for key in endpoints if key.startswith("scopes")}
                                  for level in sorted({p["source_level"] for p in per_paper})
                                  if any(key.startswith("scopes") for key in endpoints)},
              "provenance_coverage": {"valid": valid_provenance, "total": total_provenance,
                                      "fraction": valid_provenance / total_provenance if total_provenance else None},
              "relation_coverage": {"gold_all_types": sum(p["relation_gold_total"] for p in per_paper),
                                    "gold_same_sentence": sum(p["relation_gold_same_sentence"] for p in per_paper),
                                    "unsupported_types": sorted(set(by_type) - set(PROJECTION["predicates"].values())),
                                    "note": "All gold types/cross-sentence misses remain in primary FN denominator"}
                                   if "chemical_gene_relation" in annotated_tasks else None,
              "projection": run["projection"], "source_manifest": receipt,
              "claims": {"nutrition_accuracy": "unmeasured", "review_time_saved": "unmeasured",
                         "scope": "published public task only; normalization oracle not detector accuracy",
                         "scope_metric": "cue-linked whitespace token F1 and exact span F1; not the official Java scorer",
                         "pretraining_overlap": receipt.get("pretraining_overlap", "unknown")}}
    path = Path(gold_path).parent / ("evaluation-" + report["id"] + ".json")
    _write_immutable(path, report)
    _receipt(settings, "public_benchmark_evaluation", report["id"], report)
    return {**report, "output_path": str(path)}


def _aggregate_normalization(per_paper, mode):
    norm_fields = ["normalized_predictions", "correct", "gold_mentions", "crosswalk_mappable_gold", "predicted_mentions"]
    n = {key: sum(paper["normalization"][key] for paper in per_paper) for key in norm_fields}
    n.update(accuracy=n["correct"] / n["normalized_predictions"] if n["normalized_predictions"] else None,
             recall=n["correct"] / n["gold_mentions"] if n["gold_mentions"] else None,
             crosswalk_coverage=n["crosswalk_mappable_gold"] / n["gold_mentions"] if n["gold_mentions"] else None,
             unnormalized_fraction=(n["predicted_mentions"] - n["normalized_predictions"]) / n["predicted_mentions"]
             if n["predicted_mentions"] else None,
             oracle_not_detector_result=mode == "gold_entities")
    n["by_kind"] = {}
    for kind in ["gene", "component"]:
        values = {key: sum(paper["normalization_by_kind"][kind][key] for paper in per_paper) for key in norm_fields}
        n["by_kind"][kind] = {
            **values, "accuracy": values["correct"] / values["normalized_predictions"] if values["normalized_predictions"] else None,
            "recall": values["correct"] / values["gold_mentions"] if values["gold_mentions"] else None,
            "crosswalk_coverage": values["crosswalk_mappable_gold"] / values["gold_mentions"] if values["gold_mentions"] else None,
            "unnormalized_fraction": (values["predicted_mentions"] - values["normalized_predictions"]) / values["predicted_mentions"]
            if values["predicted_mentions"] else None,
        }
    return n


def paired_bootstrap(papers, baseline, candidate, endpoint="relation", iterations=2000, seed=20261003):
    """Resample papers, preserving all annotation events and both engines together."""
    if type(iterations) is not int or iterations < 100:
        raise ValueError("At least 100 paper bootstrap iterations required")
    if not papers or set(papers) != set(baseline) or set(papers) != set(candidate):
        raise ValueError("Paired bootstrap requires exactly the same nonempty papers")
    if {p for p in papers if endpoint in baseline[p]} != {p for p in papers if endpoint in candidate[p]}:
        raise ValueError("Paired endpoint annotation coverage differs")
    papers = [p for p in papers if endpoint in baseline[p]]
    if not papers:
        raise ValueError("Paired endpoint has no annotated papers")
    rng, draws = random.Random(seed), {key: [] for key in ["precision", "recall", "f1"]}
    for _ in range(iterations):
        selected = rng.choices(papers, k=len(papers))
        old = _aggregate([baseline[p] for p in selected], endpoint)
        new = _aggregate([candidate[p] for p in selected], endpoint)
        for metric in draws:
            if old[metric] is not None and new[metric] is not None:
                draws[metric].append(new[metric] - old[metric])
    result = {}
    old = _aggregate([baseline[p] for p in papers], endpoint)
    new = _aggregate([candidate[p] for p in papers], endpoint)
    for metric, values in draws.items():
        values.sort()
        result[metric] = {"baseline": old[metric], "candidate": new[metric],
                          "delta": new[metric] - old[metric] if new[metric] is not None and old[metric] is not None else None,
                          "lower_ci95": values[int(.025 * (len(values) - 1))] if values else None,
                          "upper_ci95": values[int(.975 * (len(values) - 1))] if values else None,
                          "valid_draws": len(values)}
    return {"endpoint": endpoint, "iterations": iterations, "seed": seed, "papers": len(papers),
            "method": "paired paper bootstrap percentile 95%; no annotation-event independence assumed",
            "metrics": result}


def compare_public(settings, gold_path, baseline_run, candidate_run, iterations=2000, seed=20261003,
                   crosswalk_path=None):
    old = evaluate_public(settings, gold_path, baseline_run, crosswalk_path=crosswalk_path, reuse=True)
    new = evaluate_public(settings, gold_path, candidate_run, crosswalk_path=crosswalk_path, reuse=True)
    if old["mode"] != new["mode"]:
        raise ValueError("Cannot compare relation oracle with end-to-end detector")
    if (old["input_sha256"] != new["input_sha256"] or old["gold_sha256"] != new["gold_sha256"]
            or old["projection"] != new["projection"]
            or old["annotation_applicability"] != new["annotation_applicability"]
            or set(old["metrics"]) != set(new["metrics"])):
        raise ValueError("Paired comparison requires unchanged paper cohort, labels, and label projection")
    baseline = {paper["pmid"]: paper for paper in old["per_paper"]}
    candidate = {paper["pmid"]: paper for paper in new["per_paper"]}
    result = {"schema_version": "literature-public-comparison-v1", "id": uid(), "compared_at": now(),
              "corpus": new["corpus"], "split": new["split"], "mode": new["mode"],
              "status": "paired_public_gold_comparison", "baseline_report_id": old["id"],
              "candidate_report_id": new["id"], "input_sha256": new["input_sha256"],
              "gold_sha256": new["gold_sha256"], "baseline_provenance": old["provenance"],
              "candidate_provenance": new["provenance"], "projection": PROJECTION,
              "annotation_applicability": new["annotation_applicability"],
              "evaluator_source_hashes": evaluator_source_hashes(),
              "bootstrap": {endpoint: paired_bootstrap(sorted(baseline), baseline, candidate, endpoint, iterations, seed)
                            for endpoint in sorted(new["metrics"])}}
    path = Path(gold_path).parent / ("comparison-" + result["id"] + ".json")
    _write_immutable(path, result)
    _receipt(settings, "public_benchmark_comparison", result["id"], result)
    return {**result, "output_path": str(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("corpus", choices=sorted(CATALOG))
    prepare.add_argument("--split", choices=["train", "development", "test"], default="development")
    prepare.add_argument("--source", type=Path)
    prepare.add_argument("--license-reference")
    prepare.add_argument("--source-level", choices=["abstract", "fulltext"], default="abstract")
    for name in ["freeze", "predict"]:
        command = sub.add_parser(name)
        command.add_argument("inputs", type=Path)
        command.add_argument("--engine", choices=["heuristic-v1", "heuristic-v2"], default="heuristic-v2")
        command.add_argument("--crosswalk", type=Path)
        if name == "predict":
            command.add_argument("--freeze", type=Path)
            command.add_argument("--mode", choices=["end_to_end", "gold_entities"], default="end_to_end")
            command.add_argument("--ablation-stage", choices=["entities_v2_relations_v1", "syntax_without_context"])
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("gold", type=Path)
    evaluate.add_argument("prediction", type=Path)
    evaluate.add_argument("--crosswalk", type=Path)
    compare = sub.add_parser("compare")
    compare.add_argument("gold", type=Path)
    compare.add_argument("baseline", type=Path)
    compare.add_argument("candidate", type=Path)
    compare.add_argument("--crosswalk", type=Path)
    args = parser.parse_args()
    from .db import initialize
    from .settings import Settings
    settings = Settings.from_env()
    if args.home:
        settings = Settings(args.home, settings.config)
    initialize(settings)
    if args.command == "prepare":
        result = prepare_public(settings, args.corpus, args.split, args.source, args.license_reference,
                                source_level=args.source_level)
    elif args.command == "freeze":
        result = freeze_benchmark(settings, args.inputs, args.engine, args.crosswalk)
    elif args.command == "predict":
        result = predict_public(settings, args.inputs, args.engine, args.freeze, args.mode, args.crosswalk, args.ablation_stage)
    elif args.command == "evaluate":
        result = evaluate_public(settings, args.gold, args.prediction, crosswalk_path=args.crosswalk)
    else:
        result = compare_public(settings, args.gold, args.baseline, args.candidate, crosswalk_path=args.crosswalk)
    print(dumps(result))


if __name__ == "__main__":
    main()
