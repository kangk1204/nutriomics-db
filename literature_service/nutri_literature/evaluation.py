"""Document-grouped, frozen manual gold; report measured counts only after review."""

import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path

from .db import connect, digest, dumps, now, uid

_COHORT_FIELDS = (
    "pmid", "group_id", "split", "primary_topic", "topics", "source_level", "gene_presence_stratum",
    "sampling_stratum", "title", "source_id", "source_hash", "source_url", "extraction_id",
    "engine_version", "dictionary_version", "rules_version", "double_review_required",
)
_IDENTITY_FIELDS = tuple(field for field in _COHORT_FIELDS if field not in {
    "extraction_id", "engine_version", "dictionary_version", "rules_version",
})
_MANIFEST_FIELDS = ("cohort_id", "created_at", "seed", "sampling_method")


def _prediction(con, extraction_id):
    return {
        "entities": [
            dict(row)
            for row in con.execute(
                "SELECT kind,text,normalized_id,start,end FROM mentions WHERE extraction_id=? ORDER BY kind,start,end",
                (extraction_id,),
            )
        ],
        "relations": [
            dict(row)
            for row in con.execute(
                "SELECT subject,predicate,object,quote,start,end,payload FROM assertions WHERE extraction_id=? ORDER BY start,assertion_id",
                (extraction_id,),
            )
        ],
    }


def prepare_gold(settings, n=200, seed=20261001):
    from .extraction import RULES_VERSION

    if n < 2 or n % 2:
        raise ValueError("Gold document count must be a positive even number")
    rng = random.Random(seed)
    with connect(settings) as con:
        con.execute("BEGIN")
        rows = con.execute(
            "SELECT p.paper_id,p.pmid,p.doi,p.title,p.current_source_id,e.extraction_id,e.engine_version,"
            "e.dictionary_version,e.result_json,e.source_hash,s.text_content,s.text_hash,s.url,s.kind AS source_level,"
            "s.sections AS source_sections,"
            "EXISTS(SELECT 1 FROM mentions m WHERE m.extraction_id=e.extraction_id AND m.kind='gene') AS gene_present "
            "FROM papers p "
            "JOIN extractions e ON e.paper_id=p.paper_id AND e.source_id=p.current_source_id "
            "JOIN sources s ON s.source_id=e.source_id WHERE e.active=1 AND p.retracted=0 "
            "AND e.source_hash=s.text_hash ORDER BY p.pmid,e.created_at DESC"
        ).fetchall()
        groups, memberships = {}, {}
        for row in rows:
            result = json.loads(row["result_json"])
            if digest(row["text_content"]) != row["text_hash"] or result.get("rules_version") != RULES_VERSION:
                continue
            # DOI variants of the same publication never cross development and heldout.
            key = "doi:" + row["doi"].lower().strip() if row["doi"] else "pmid:" + row["pmid"]
            if key in groups:
                continue
            topics = [
                item[0]
                for item in con.execute(
                    "SELECT DISTINCT pt.topic_id FROM paper_topics pt JOIN topics t ON t.topic_id=pt.topic_id "
                    "AND t.query_version=pt.query_version WHERE pt.paper_id=? AND t.enabled=1 ORDER BY pt.topic_id",
                    (row["paper_id"],),
                )
            ]
            if topics:
                groups[key], memberships[key] = dict(row), topics
        if len(groups) < n:
            raise ValueError(f"Need {n} extracted, nonretracted document groups; found {len(groups)}")
        prevalence = Counter(topic for topics in memberships.values() for topic in topics)
        pools = defaultdict(list)
        for key, row in groups.items():
            primary = min(memberships[key], key=lambda topic: (prevalence[topic], topic))
            gene_presence = "predicted_present" if row["gene_present"] else "predicted_absent"
            pools[(primary, row["source_level"], gene_presence)].append(key)
        for pool in pools.values():
            rng.shuffle(pool)
        selected = []
        # Round-robin strata redistribute empty strata without duplicating papers.
        while len(selected) < n:
            for stratum in sorted(pools):
                if pools[stratum] and len(selected) < n:
                    selected.append((pools[stratum].pop(), stratum))
        # Alternate within strata and rotate across dimensions, avoiding presence/split parity bias.
        by_stratum = defaultdict(list)
        for item in selected:
            by_stratum[item[1]].append(item)
        split_members = {"development": [], "heldout": []}
        dimension_orders = [
            {value: index for index, value in enumerate(sorted({s[dimension] for s in by_stratum}))}
            for dimension in range(3)
        ]
        for stratum in sorted(by_stratum):
            phase = sum(dimension_orders[dimension][stratum[dimension]] for dimension in range(3))
            for index, item in enumerate(by_stratum[stratum]):
                split_members["development" if (index + phase) % 2 == 0 else "heldout"].append(item)
        for larger, smaller in [("development", "heldout"), ("heldout", "development")]:
            while len(split_members[larger]) > n // 2:
                split_members[smaller].append(split_members[larger].pop())
        double_count = n // 5
        double_groups = set(rng.sample([key for key, _ in selected], double_count))
        documents, predictions, inputs = [], [], []
        for split, items in split_members.items():
            for key, stratum in items:
                row = groups[key]
                primary, source_level, gene_presence = stratum
                document = {
                    "pmid": row["pmid"],
                    "group_id": key,
                    "split": split,
                    "primary_topic": primary,
                    "source_level": source_level,
                    "gene_presence_stratum": gene_presence,
                    "sampling_stratum": ":".join(stratum),
                    "topics": memberships[key],
                    "title": row["title"],
                    "text": row["text_content"],
                    "source_id": row["current_source_id"],
                    "source_hash": row["source_hash"],
                    "source_url": row["url"],
                    "extraction_id": row["extraction_id"],
                    "engine_version": row["engine_version"],
                    "dictionary_version": row["dictionary_version"],
                    "rules_version": RULES_VERSION,
                    "review_status": "pending",
                    "reviewers": [],
                    "double_review_required": key in double_groups,
                    "adjudicated": False,
                    "entities": [],
                    "relations": [],
                }
                documents.append(document)
                predictions.append(
                    {
                        **{field: document[field] for field in _COHORT_FIELDS},
                        **_prediction(con, row["extraction_id"]),
                    }
                )
                metadata = dict(con.execute("SELECT * FROM papers WHERE paper_id=?", (row["paper_id"],)).fetchone())
                metadata["publication_types"] = json.loads(metadata["publication_types"])
                metadata["mesh"] = json.loads(metadata["mesh"])
                topic_versions = dict(con.execute(
                    "SELECT pt.topic_id,pt.query_version FROM paper_topics pt JOIN topics t ON t.topic_id=pt.topic_id "
                    "AND t.query_version=pt.query_version WHERE pt.paper_id=? AND t.enabled=1 ORDER BY pt.topic_id",
                    (row["paper_id"],),
                ).fetchall())
                inputs.append({
                    **{field: document[field] for field in _COHORT_FIELDS},
                    "text": document["text"], "sections": json.loads(row["source_sections"]),
                    "metadata": metadata, "topic_versions": topic_versions,
                })
    cohort_id, prepared_at = uid(), now()
    directory = settings.home / "evaluation" / cohort_id
    directory.mkdir(parents=True, exist_ok=False)
    prediction_path = directory / "frozen_predictions.json"
    input_path = directory / "frozen_inputs.json"
    sampling_method = "Round-robin topic × source level × heuristic gene-presence strata. Predicted presence is a sampling signal, not a reviewed gold label."
    input_serialized = dumps({"schema_version": "literature-inputs-v1", "cohort_id": cohort_id,
                              "created_at": prepared_at, "seed": seed, "sampling_method": sampling_method,
                              "documents": inputs})
    input_sha256 = digest(input_serialized)
    input_path.write_text(input_serialized, encoding="utf-8")
    serialized = dumps({"schema_version": "literature-predictions-v1", "cohort_provenance_version": 1,
                        "cohort_id": cohort_id, "created_at": prepared_at, "seed": seed,
                        "sampling_method": sampling_method, "documents": predictions})
    prediction_path.write_text(serialized, encoding="utf-8")
    manifest = {
        "schema_version": "literature-gold-v1",
        "cohort_id": cohort_id,
        "created_at": prepared_at,
        "seed": seed,
        "prediction_file": prediction_path.name,
        "prediction_sha256": digest(serialized),
        "input_file": input_path.name,
        "input_sha256": input_sha256,
        "sampling_method": sampling_method,
        "instructions": "원문에서 entities(kind,text,start,end,normalized_id)와 relations(subject,predicate,object,quote,start,end)를 독립 표기하세요. 검토 완료 후 review_status=reviewed, reviewers를 기록하고 이중 검토 문헌은 adjudicated=true로 확정하세요. 빈 목록은 검토자가 확정한 음성 문헌에만 사용하세요.",
        "documents": documents,
    }
    gold_path = directory / "gold.json"
    gold_path.write_text(dumps(manifest), encoding="utf-8")
    with connect(settings, durable=True) as con:
        con.execute("INSERT INTO audit VALUES(?,?,?,?,?)",
                    (uid(), "gold_prepared", None, dumps({"cohort_id": cohort_id, "documents": n,
                                                         "prediction_sha256": manifest["prediction_sha256"],
                                                         "input_sha256": input_sha256}), prepared_at))
    return {
        "gold_path": str(gold_path),
        "predictions_path": str(prediction_path),
        "inputs_path": str(input_path),
        "documents": n,
        "development": n // 2,
        "heldout": n // 2,
        "double_review": double_count,
        "strata": dict(Counter(d["primary_topic"] for d in documents)),
        "sampling_strata": dict(Counter(d["sampling_stratum"] for d in documents)),
        "status": "pending_manual_review",
        "cohort_id": cohort_id,
    }


def _wilson(successes, denominator):
    if not denominator:
        return None
    z = 1.959963984540054
    fraction = successes / denominator
    scale = 1 + z * z / denominator
    center = (fraction + z * z / (2 * denominator)) / scale
    half = z * math.sqrt(fraction * (1 - fraction) / denominator + z * z / (4 * denominator**2)) / scale
    return {"lower": max(0.0, center - half), "upper": min(1.0, center + half), "denominator": denominator}


def _metrics(expected, observed):
    tp, fp, fn = len(expected & observed), len(observed - expected), len(expected - observed)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
        "precision_ci95": _wilson(tp, tp + fp),
        "recall_ci95": _wilson(tp, tp + fn),
        "ci_method": "Wilson 95% binomial approximation",
    }


_SEMANTIC_FIELDS = (
    "polarity", "speculative", "species", "organism", "study_model", "outcome_type",
    "direction", "arm_id", "arm", "comparator", "timepoint", "statistical_significance",
    "claim_status", "context", "scope_start", "scope_end", "speculation", "negation",
    "significance", "conditions", "scope", "association_direction",
)


def _semantic_value(relation, field):
    """Read direct v2 fields or legacy assertion payload without inventing labels."""
    if field in relation:
        return relation[field]
    payload = relation.get("payload", {})
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        return None
    event = payload.get("event", {})
    if isinstance(event, dict):
        payload = {**event, **payload}
    if field in payload:
        return payload[field]
    conditions = relation.get("conditions", payload.get("conditions", {}))
    if field in {"arm_id", "arm", "comparator", "timepoint"} and isinstance(conditions, dict):
        return conditions.get(field)
    scope = relation.get("scope", payload.get("scope", {}))
    if field in {"scope_start", "scope_end"} and isinstance(scope, dict):
        return scope.get(field.removeprefix("scope_"))
    return None


def _annotated_semantics(document):
    explicit = document.get("relation_semantic_fields")
    if explicit is not None:
        if (not isinstance(explicit, list) or len(set(explicit)) != len(explicit)
                or any(field not in _SEMANTIC_FIELDS for field in explicit)):
            raise ValueError("Invalid annotated relation semantic fields")
        return tuple(explicit)
    present = set()
    for relation in document.get("relations", []):
        if not isinstance(relation, dict):
            continue
        payload = relation.get("payload", {})
        if isinstance(payload, str):
            payload = json.loads(payload)
        present.update(relation)
        if isinstance(payload, dict):
            present.update(payload)
            if isinstance(payload.get("event"), dict):
                present.update(payload["event"])
    return tuple(field for field in _SEMANTIC_FIELDS if field in present)


def _sets(document, payload):
    pmid, text = document["pmid"], document["text"]
    detected, normalized, relations = set(), set(), set()
    for entity in payload["entities"]:
        start, end = entity["start"], entity["end"]
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not 0 <= start < end <= len(text)
            or text[start:end] != entity["text"]
        ):
            raise ValueError(f"Invalid entity source span in PMID {pmid}")
        key = (pmid, entity["kind"], start, end)
        detected.add(key)
        if entity.get("normalized_id"):
            normalized.add((*key, entity["normalized_id"]))
    semantic_fields = _annotated_semantics(document)
    for relation in payload["relations"]:
        start, end = relation["start"], relation["end"]
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not 0 <= start < end <= len(text)
            or text[start:end] != relation["quote"]
        ):
            raise ValueError(f"Invalid relation source span in PMID {pmid}")
        semantics = tuple((field, dumps(_semantic_value(relation, field))) for field in semantic_fields)
        key = (pmid, relation["subject"], relation["predicate"], relation["object"])
        relations.add((*key, semantics) if semantic_fields else key)
    return {"detection": detected, "normalization": normalized, "relation": relations}


def _score_documents(documents, predictions):
    expected = {kind: set() for kind in ["detection", "normalization", "relation"]}
    observed = {kind: set() for kind in expected}
    gold_gene_documents, predicted_gene_documents, papers_with_predictions = set(), set(), set()
    for document in documents:
        pmid = document["pmid"]
        predicted = predictions[pmid]
        if predicted["source_hash"] != document["source_hash"]:
            raise ValueError("Frozen prediction source differs from gold")
        for kind, values in _sets(document, document).items():
            expected[kind].update(values)
        for kind, values in _sets(document, predicted).items():
            observed[kind].update(values)
        if any(entity["kind"] == "gene" for entity in document["entities"]):
            gold_gene_documents.add(pmid)
        if any(entity["kind"] == "gene" for entity in predicted["entities"]):
            predicted_gene_documents.add(pmid)
        if predicted["entities"] or predicted["relations"]:
            papers_with_predictions.add(pmid)
    count = len(documents)
    gene_detected = len(gold_gene_documents & predicted_gene_documents)
    result = {
        "documents": count,
        **{kind: _metrics(expected[kind], observed[kind]) for kind in expected},
        "coverage": {
            "evaluated_papers": count,
            "papers_with_predictions": len(papers_with_predictions),
            "paper_prediction_coverage": len(papers_with_predictions) / count if count else None,
            "predicted_gene_documents": len(predicted_gene_documents),
            "predicted_gene_document_coverage": len(predicted_gene_documents) / count if count else None,
            "gold_gene_documents": len(gold_gene_documents),
            "gene_positive_documents_detected": gene_detected,
            "gene_document_recall": gene_detected / len(gold_gene_documents) if gold_gene_documents else None,
            "gene_document_recall_ci95": _wilson(gene_detected, len(gold_gene_documents)),
        },
    }
    entity_kinds = {row[1] for row in expected["detection"] | observed["detection"]}
    result["by_kind"] = {
        entity_kind: {
            level: _metrics(
                {row for row in expected[level] if row[1] == entity_kind},
                {row for row in observed[level] if row[1] == entity_kind},
            )
            for level in ["detection", "normalization"]
        }
        for entity_kind in sorted(entity_kinds)
    }
    return result


def _audit_record(settings, kind, key, value):
    with connect(settings) as con:
        records = [json.loads(row[0]) for row in con.execute("SELECT detail FROM audit WHERE kind=?", (kind,))]
    return next((record for record in records if record.get(key) == value), None)


def _beside(path, filename, label):
    candidate = path.parent / filename
    if candidate.resolve().parent != path.parent.resolve():
        raise ValueError(f"{label} file must stay beside gold manifest")
    return candidate


def _document_map(manifest, label):
    documents = manifest.get("documents", [])
    if not documents:
        raise ValueError(f"Expected nonempty {label} documents")
    mapped = {document["pmid"]: document for document in documents}
    if len(mapped) != len(documents):
        raise ValueError(f"Duplicate PMID in {label}")
    return mapped


def _load_inputs(settings, input_path):
    """Load only frozen extractor inputs, independently of editable gold labels."""
    input_path = Path(input_path)
    raw = input_path.read_bytes()
    inputs = json.loads(raw)
    if inputs.get("schema_version") != "literature-inputs-v1":
        raise ValueError("Expected a literature-inputs-v1 frozen input manifest")
    pin = _audit_record(settings, "gold_prepared", "cohort_id", inputs.get("cohort_id"))
    if pin is None or pin.get("input_sha256") != digest(raw):
        raise ValueError("Frozen inputs do not match the recorded cohort hash")
    documents = _document_map(inputs, "frozen input")
    for document in documents.values():
        if digest(document["text"]) != document["source_hash"]:
            raise ValueError("Frozen input source text hash changed")
        if not isinstance(document.get("sections"), list) or not isinstance(document.get("metadata"), dict):
            raise ValueError("Frozen inputs require source sections and extraction metadata")
    return inputs, documents, digest(raw)


def predict_gold(settings, input_path, engine="heuristic-v1"):
    """Apply installed rules to pinned inputs; never open annotation or baseline files."""
    from .dictionaries import load_dictionaries
    from .extraction import extract

    input_path = Path(input_path)
    inputs, documents, input_sha256 = _load_inputs(settings, input_path)
    # Keep legacy imports/call shape for historical v1 reruns and their receipts.
    if engine == "heuristic-v1":
        dictionaries = load_dictionaries(settings)
    else:
        from .engines import extract_engine
        dictionaries = load_dictionaries(settings, engine=engine)
    predictions = []
    for document in documents.values():
        extracted = (
            extract(document["text"], document["sections"], document["metadata"], dictionaries)
            if engine == "heuristic-v1" else extract_engine(
                document["text"], document["sections"], dictionaries,
                metadata=document["metadata"], engine=engine,
            )
        )
        predicted = {
            **{field: document[field] for field in _IDENTITY_FIELDS},
            **{field: extracted[field] for field in ["engine_version", "dictionary_version", "rules_version"]},
            "entities": extracted["mentions"],
            "relations": extracted["assertions"],
        }
        _sets(document, predicted)
        predictions.append(predicted)
    run_id, created_at = uid(), now()
    run = {
        "schema_version": "literature-prediction-run-v1", "run_id": run_id,
        "cohort_id": inputs["cohort_id"], "input_sha256": input_sha256, "created_at": created_at,
        "dictionary_manifest": dictionaries.manifest, "documents": predictions,
    }
    serialized = dumps(run)
    output = input_path.parent / ("prediction-run-" + run_id + ".json")
    output.write_text(serialized, encoding="utf-8")
    with connect(settings, durable=True) as con:
        con.execute("INSERT INTO audit VALUES(?,?,?,?,?)", (
            uid(), "gold_prediction_run", None,
            dumps({"run_id": run_id, "cohort_id": inputs["cohort_id"], "input_sha256": input_sha256,
                   "prediction_sha256": digest(serialized)}), created_at,
        ))
    return {
        "prediction_run": str(output), "run_id": run_id, "cohort_id": inputs["cohort_id"],
        "input_sha256": input_sha256, "prediction_sha256": digest(serialized), "documents": len(predictions),
        "splits": dict(Counter(document["split"] for document in predictions)),
        "engine_versions": sorted({document["engine_version"] for document in predictions}),
        "dictionary_versions": sorted({document["dictionary_version"] for document in predictions}),
        "rules_versions": sorted({document["rules_version"] for document in predictions}),
        "status": "predictions_only_no_gold_labels_read",
    }


def _load_prediction_run(settings, prediction_run, inputs, input_documents, input_sha256):
    raw = Path(prediction_run).read_bytes()
    run = json.loads(raw)
    if run.get("schema_version") != "literature-prediction-run-v1":
        raise ValueError("Expected a literature-prediction-run-v1 manifest")
    pin = _audit_record(settings, "gold_prediction_run", "run_id", run.get("run_id"))
    if pin is None or pin.get("prediction_sha256") != digest(raw):
        raise ValueError("Prediction run does not match its recorded hash")
    if (run.get("cohort_id") != inputs["cohort_id"] or pin.get("cohort_id") != inputs["cohort_id"]
            or run.get("input_sha256") != input_sha256 or pin.get("input_sha256") != input_sha256):
        raise ValueError("Prediction run belongs to a different frozen input cohort")
    predictions = _document_map(run, "prediction run")
    if set(predictions) != set(input_documents):
        raise ValueError("Prediction run and frozen input document sets differ")
    for pmid, document in input_documents.items():
        predicted = predictions[pmid]
        for field in _IDENTITY_FIELDS:
            if predicted.get(field) != document[field]:
                raise ValueError(f"Prediction run changed immutable cohort metadata: {field}")
        for field in ["engine_version", "dictionary_version", "rules_version"]:
            if not isinstance(predicted.get(field), str) or not predicted[field]:
                raise ValueError(f"Prediction run requires {field} provenance")
    return run, predictions, digest(raw)


def _validate_review(document):
    reviewers = document.get("reviewers", [])
    if (document.get("review_status") != "reviewed" or not isinstance(reviewers, list) or not reviewers
            or any(not isinstance(reviewer, str) or not reviewer.strip() for reviewer in reviewers)):
        raise ValueError("Refusing unreviewed gold: selected documents require named manual review")
    identities = {reviewer.strip().casefold() for reviewer in reviewers}
    if document.get("double_review_required") and (
            len(identities) < 2 or document.get("adjudicated") is not True):
        raise ValueError("Double review requires two reviewers and adjudication")


def _review_complete(document, validate_labels=False):
    try:
        _validate_review(document)
        if validate_labels:
            _sets(document, document)
        return True
    except (ValueError, TypeError, KeyError):
        return False


def evaluate(settings, gold_path, prediction_run=None, split="both", acceptance_contract=None):
    from .acceptance import acceptance_status, load_contract

    contract, contract_sha256 = (
        load_contract(acceptance_contract) if acceptance_contract is not None else (None, None)
    )
    if split not in {"development", "heldout", "both"}:
        raise ValueError("Evaluation split must be development, heldout, or both")
    gold_path = Path(gold_path)
    raw = gold_path.read_bytes()
    gold = json.loads(raw)
    if gold.get("schema_version") != "literature-gold-v1" or not gold.get("documents"):
        raise ValueError("Expected a nonempty literature-gold-v1 manifest")
    seen, groups = set(), {}
    for document in gold["documents"]:
        pmid = document["pmid"]
        if pmid in seen or document["split"] not in {"development", "heldout"}:
            raise ValueError("Duplicate PMID or invalid gold split")
        seen.add(pmid)
        group = document["group_id"]
        if group in groups and groups[group] != document["split"]:
            raise ValueError("Document group crosses development and heldout")
        groups[group] = document["split"]
        if digest(document["text"]) != document["source_hash"]:
            raise ValueError("Gold source text hash changed")
    prediction_path = _beside(gold_path, gold["prediction_file"], "Prediction")
    prediction_raw = prediction_path.read_bytes()
    if digest(prediction_raw) != gold["prediction_sha256"]:
        raise ValueError("Frozen predictions changed")
    frozen = json.loads(prediction_raw)
    if frozen.get("cohort_provenance_version") != 1:
        raise ValueError("Frozen cohort provenance is missing; prepare a new gold cohort before annotation")
    for field in _MANIFEST_FIELDS:
        if gold.get(field) != frozen.get(field):
            raise ValueError(f"Immutable cohort metadata changed: {field}")
    pin = _audit_record(settings, "gold_prepared", "cohort_id", frozen["cohort_id"])
    if pin is None or pin.get("prediction_sha256") != digest(prediction_raw):
        raise ValueError("Frozen predictions do not match the recorded cohort hash")
    predictions = _document_map(frozen, "frozen prediction")
    if set(predictions) != seen:
        raise ValueError("Frozen predictions and gold document sets differ")
    for document in gold["documents"]:
        predicted = predictions[document["pmid"]]
        for field in _COHORT_FIELDS:
            if field not in document or field not in predicted or document[field] != predicted[field]:
                raise ValueError(f"Immutable cohort metadata changed in PMID {document['pmid']}: {field}")
    input_sha256 = pin.get("input_sha256")
    if input_sha256:
        if gold.get("input_sha256") != input_sha256 or not gold.get("input_file"):
            raise ValueError("Frozen input reference differs from the recorded cohort hash")
        inputs, input_documents, actual_hash = _load_inputs(settings, _beside(gold_path, gold["input_file"], "Input"))
        if actual_hash != input_sha256 or set(input_documents) != seen:
            raise ValueError("Frozen input and gold document cohorts differ")
        for field in _MANIFEST_FIELDS:
            if inputs.get(field) != frozen.get(field):
                raise ValueError(f"Frozen input cohort metadata differs: {field}")
        for document in gold["documents"]:
            source_input = input_documents[document["pmid"]]
            for field in (*_COHORT_FIELDS, "text"):
                if source_input.get(field) != document[field]:
                    raise ValueError(f"Frozen input and gold immutable cohort metadata differs: {field}")
    elif prediction_run is not None:
        raise ValueError("Historical baseline has no pinned extractor inputs; prepare a new cohort for rule reruns")
    run_id = None
    prediction_sha256 = gold["prediction_sha256"]
    if prediction_run is not None:
        run, predictions, prediction_sha256 = _load_prediction_run(
            settings, prediction_run, inputs, input_documents, input_sha256,
        )
        run_id = run["run_id"]
    selected_splits = ["development", "heldout"] if split == "both" else [split]
    selected_documents = [document for document in gold["documents"] if document["split"] in selected_splits]
    if not selected_documents:
        raise ValueError("No documents in selected evaluation split")
    for document in selected_documents:
        _validate_review(document)
    prediction_documents = [predictions[document["pmid"]] for document in selected_documents]
    result = {
        "schema_version": "literature-evaluation-v1",
        "evaluated_at": now(),
        "gold_sha256": digest(raw),
        "prediction_sha256": prediction_sha256,
        "baseline_prediction_sha256": gold["prediction_sha256"],
        "input_sha256": input_sha256,
        "prediction_run_id": run_id,
        "prediction_scope": "independent_rule_run" if run_id else "preparation_time_baseline",
        "selected_split": split,
        "cohort_id": frozen["cohort_id"],
        "engine_versions": sorted({d["engine_version"] for d in prediction_documents}),
        "dictionary_versions": sorted({d["dictionary_version"] for d in prediction_documents}),
        "rules_versions": sorted({d["rules_version"] for d in prediction_documents}),
        "documents": len(selected_documents),
        "cohort_documents": len(seen),
        "cohort_split_documents": dict(Counter(d["split"] for d in gold["documents"])),
        "cohort_double_review_documents": sum(d["double_review_required"] for d in gold["documents"]),
        "cohort_reviewed_documents": sum(_review_complete(d) for d in gold["documents"]),
        "cohort_completed_double_review_documents": sum(
            d["double_review_required"] and _review_complete(d) for d in gold["documents"]
        ),
        "cohort_completed_gold_documents": (
            sum(_review_complete(d, validate_labels=True) for d in gold["documents"])
            if contract is not None else None
        ),
        "status": "measured_on_manually_reviewed_gold",
        "splits": {},
        "ci_note": "Wilson 95% binomial approximation assumes independent annotation events; within-paper dependence is not modeled. F1 confidence intervals are not estimated.",
        "by_topic_scope": "Primary sampling topic: each paper belongs to one topic in this breakdown.",
        "sampling_note": "Scores describe this stratified cohort; they are not weighted estimates for the literature population.",
    }
    for selected_split in selected_splits:
        documents = [d for d in selected_documents if d["split"] == selected_split]
        result["splits"][selected_split] = _score_documents(documents, predictions)
        topics = sorted({d["primary_topic"] for d in documents})
        result["splits"][selected_split]["by_topic"] = {
            topic: _score_documents([d for d in documents if d["primary_topic"] == topic], predictions)
            for topic in topics
        }
    result["acceptance"] = acceptance_status(result, contract, contract_sha256)
    output = gold_path.parent / ("evaluation-" + uid() + ".json")
    output.write_text(dumps(result), encoding="utf-8")
    return {**result, "output_path": str(output)}
