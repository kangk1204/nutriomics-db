"""Freeze-bound normalized NLM/clinical tests; predictions precede annotations."""

import json
from pathlib import Path

from . import benchmarks as scoring
from .db import digest, dumps, now, uid
from .training_v4 import text_key

CORPORA = {"nlmgene", "nlmchem", "pubmedrct", "evidenceinference"}
FORBIDDEN = {
    "entities",
    "relations",
    "scopes",
    "annotated_spans",
    "labels",
    "rhetorical_labels",
    "comparisons",
    "evidence",
}


def source_inputs(spec):
    raw = Path(spec["inputs_path"]).read_bytes()
    if digest(raw) != spec["inputs_sha256"]:
        raise ValueError("Frozen source-only holdout changed")
    value = json.loads(raw)
    if value.get("split") != "test" or value.get("corpus") != spec["corpus"]:
        raise ValueError("Official complete test partition required")
    docs = value["documents"]
    if len(docs) != spec["papers"] or len({doc["pmid"] for doc in docs}) != len(docs):
        raise ValueError("Final cohort is partial or duplicated")
    for doc in docs:
        if set(doc) & FORBIDDEN or digest(doc["text"]) != doc["source_hash"]:
            raise ValueError("Final inputs contain labels or a changed source")
        for row in doc.get("rhetorical_sentences", []):
            if set(row) & {"rhetorical_role", "role", "gold_label"} or row.get("label") != "Abstract":
                raise ValueError("RCT prediction sentences contain role labels")
            if doc["text"][row["start"] : row["end"]] != row["text"]:
                raise ValueError("RCT sentence/source offsets differ")
    return value


def bind_specs(path, training_audit, artifact):
    """Read only test source texts and hashes after development gates pass."""
    record = json.loads(Path(path).read_bytes())
    specs = record.get("specs", [])
    if (
        record.get("completed") is not True
        or {spec["corpus"] for spec in specs} != CORPORA
        or len(specs) != len(CORPORA)
    ):
        raise ValueError("Completed four-corpus official holdout audit required")
    audits = {
        digest(Path(p).read_bytes()): json.loads(Path(p).read_bytes())
        for p in record.get("training_audit_paths", [])
    }
    audits[digest(dumps(training_audit))] = training_audit
    needed = {artifact["v6_models"]["audit_sha256"]} | {
        head["audit_sha256"] for head in artifact["v6_models"].get("clinical_heads", {}).values()
    }
    # The exact serialized audit bytes may differ from dumps(); explicit audit
    # paths supply the actual hashes. The caller also passes its verified hash.
    if not needed <= set(audits):
        raise ValueError("Every fitted head's original training audit must be supplied")
    normalized = []
    clinical_raw = Path(record["clinical_manifest_path"]).read_bytes()
    if digest(clinical_raw) != artifact["clinical_models"]["manifest_sha256"]:
        raise ValueError("Clinical proposal training provenance differs from frozen artifact")
    clinical_ids = json.loads(clinical_raw).get("training_pmids", {})
    base_ids = (
        set().union(*(set(value) for value in clinical_ids.values()))
        if isinstance(clinical_ids, dict)
        else set(clinical_ids)
    )
    for spec in specs:
        if spec.get("official_split") != "test":
            raise ValueError("Holdout spec is not an official test split")
        inputs = source_inputs(spec)
        ids = {str(doc["pmid"]) for doc in inputs["documents"]}
        hashes = {text_key(doc["text"]) for doc in inputs["documents"]}
        if ids & base_ids:
            raise ValueError("Final cohort overlaps the clinical proposal model's training")
        for key in needed:
            audit = audits[key]
            if (
                not audit.get("passed")
                or audit.get("protected_overlap") != 0
                or ids & set(audit["training_pmids"])
                or not hashes <= set(audit["protected_text_hashes"])
            ):
                raise ValueError("Final source cohort was not protected before fitting every head")
        if digest(Path(spec["gold_path"]).read_bytes()) != spec["gold_sha256"]:
            raise ValueError("Declared sealed annotations changed")
        normalized.append({**spec, "v6_source_protection_verified": True})
    return normalized


def entity_counts(document, prediction, mapping, corpus):
    from .evaluation_v6 import ids, strict_gene_counts

    kind = "gene" if corpus == "nlmgene" else "component"
    expected = {(m["start"], m["end"]) for m in document["entities"] if m["kind"] == kind}
    selected = [
        m
        for m in prediction["entities"]
        if m["kind"] in ({"gene"} if kind == "gene" else {"component", "nutrient"})
        and m.get("payload", {}).get("automatic_selected", True)
    ]
    result = {kind + "_detection": scoring._metrics(expected, {(m["start"], m["end"]) for m in selected})}
    if kind == "gene":
        result["gene_strict_ids"] = strict_gene_counts(document, prediction, mapping)
    else:
        gold = {
            (m["start"], m["end"], str(identifier))
            for m in document["entities"]
            if m["kind"] == kind
            for identifier in m.get("native_ids", [])
        }
        observed = {
            (m["start"], m["end"], identifier)
            for m in selected
            for identifier in ids(m, mapping) or {"UNRESOLVED"}
        }
        result["chemical_strict_ids"] = scoring._metrics(gold, observed)
    return result


def evaluate(settings, spec, freeze):
    from .operations_v6 import components, identity
    from .dictionaries import load_dictionaries
    from .evaluation_v4 import _mapping
    from .registry_v6 import extract_engine
    from .clinical_evaluation_v6 import automatic_comparisons, provided_comparisons, evidence_counts

    members = freeze.get("normalized_specs", [])
    pinned = next(
        (
            member
            for member in members
            if {key: value for key, value in member.items() if key != "v6_source_protection_verified"} == spec
        ),
        None,
    )
    if pinned is None or spec["corpus"] not in CORPORA:
        raise ValueError("Official normalized test spec was not bound to the qualifying freeze")
    values = components(settings, freeze["artifact"]["candidate_provider"]["gene_provider"])
    dictionaries = load_dictionaries(settings, "heuristic-v2")
    artifact = identity(settings, values, dictionaries)
    if artifact != freeze["artifact"]:
        raise ValueError("Final artifact differs from qualifying freeze")
    if (
        digest(Path(spec["chemical_lexicon_path"]).read_bytes())
        != artifact["chemical_dictionary"]["snapshot_sha256"]
    ):
        raise ValueError("Final chemical dictionary differs from frozen batch")
    inputs = source_inputs(spec)
    mapping, _ = _mapping(settings, dictionaries, values, spec["chemical_lexicon_path"])
    directory = settings.home / "experiments/v6/final" / spec["corpus"]
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "attempt.json").open("x", encoding="utf-8") as stream:
        stream.write(
            dumps({"started_at": now(), "freeze_id": freeze["id"], "spec_sha256": digest(dumps(spec))})
        )
    predictions = {}
    for doc in inputs["documents"]:
        if spec["corpus"] == "pubmedrct":
            result = {
                "mentions": [],
                "sentence_roles": values["_clinical_models"].predict(doc["rhetorical_sentences"]),
            }
        else:
            result = extract_engine(
                doc["text"],
                doc["sections"],
                dictionaries,
                {**values, "_artifact_identity": artifact["artifact_identity"]},
                "heuristic-v6",
            )
        prediction = {
            "pmid": doc["pmid"],
            "source_hash": doc["source_hash"],
            "artifact_identity": artifact["artifact_identity"],
            "entities": result["mentions"],
            "sentence_roles": result["sentence_roles"],
        }
        if spec["corpus"] == "evidenceinference":
            threshold = artifact["v6_models"]["clinical_heads"]["evidence"]["selection"]["threshold"]
            prediction["automatic_comparisons"] = automatic_comparisons(result, threshold)
        scoring._write_immutable(directory / ("paper-" + doc["pmid"] + ".json"), prediction)
        predictions[doc["pmid"]] = prediction
    # Annotated contents become readable only after the complete source-only loop.
    raw = Path(spec["gold_path"]).read_bytes()
    if digest(raw) != spec["gold_sha256"]:
        raise ValueError("Frozen final labels changed")
    gold = json.loads(raw)
    if (
        gold.get("split") != "test"
        or gold.get("corpus") != spec["corpus"]
        or {d["pmid"] for d in gold["documents"]} != set(predictions)
    ):
        raise ValueError("Final annotated cohort differs from predictions")
    rows, role_gold, role_pred = [], [], []
    for doc in gold["documents"]:
        pred = predictions[doc["pmid"]]
        if digest(doc["text"]) != pred["source_hash"]:
            raise ValueError("Final annotated text differs from predicted source")
        counts = {}
        if spec["corpus"] in {"nlmgene", "nlmchem"}:
            counts = entity_counts(doc, pred, mapping, spec["corpus"])
        elif spec["corpus"] == "pubmedrct":
            roles = {(s["start"], s["end"]): s["rhetorical_role"] for s in pred["sentence_roles"]}
            expected = {(s["start"], s["end"], s["label"].casefold()) for s in doc["rhetorical_labels"]}
            observed = {(start, end, label) for (start, end), label in roles.items()}
            counts["rhetorical_sentence_role"] = scoring._metrics(expected, observed)
            for row in doc["rhetorical_labels"]:
                role_gold.append(row["label"].casefold())
                role_pred.append(roles.get((row["start"], row["end"]), "unaligned"))
        else:
            provided = provided_comparisons(
                doc, doc["comparisons"], values["_encoder"], values["_v6_models"].heads["evidence"], threshold
            )
            scoring._write_immutable(directory / ("paper-provided-" + doc["pmid"] + ".json"), provided)
            counts = evidence_counts(doc, pred["automatic_comparisons"], provided)
        rows.append({"pmid": doc["pmid"], **counts})
    endpoints = {
        key
        for row in rows
        for key, value in row.items()
        if isinstance(value, dict) and {"tp", "fp", "fn"} <= set(value)
    }
    report = {
        "schema": "v6-independent-normalized-final",
        "id": uid(),
        "at": now(),
        "artifact": artifact,
        "corpus": spec["corpus"],
        "split": "test",
        "papers": len(rows),
        "metrics": {key: scoring._aggregate(rows, key) for key in endpoints},
        "per_paper": rows,
        "intervals": {key: scoring._paper_intervals(rows, key, 2000, 20261010) for key in endpoints},
        "one_shot": True,
        "source_protection_verified": True,
        "nutrition_accuracy": "unmeasured",
        "pretraining_overlap": "unknown",
    }
    if role_gold:
        from sklearn.metrics import accuracy_score, f1_score

        report["rhetorical_role"] = {
            "accuracy": accuracy_score(role_gold, role_pred),
            "macro_f1": f1_score(role_gold, role_pred, average="macro"),
            "sentences": len(role_gold),
        }
    if spec["corpus"] == "evidenceinference":
        report["automatic_ico_mapping"] = (
            "strict normalized three-description identity; unmatched predictions count as FP"
        )
        report["ico_literal_span_accuracy"] = "not annotated; not measured"
        report["effect_label_accuracy"] = "not measured"
    scoring._write_immutable(directory / "report.json", report)
    scoring._receipt(settings, "v6_final_evaluation", report["id"], report)
    return report
