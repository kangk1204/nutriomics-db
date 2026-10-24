"""Separate provided-ICO and fully automatic comparative-evidence endpoints."""

import json
from pathlib import Path
import re

from . import benchmarks as scoring
from .clinical_training_v6 import inference_features
from .clinical_v6 import state
from .db import digest, now, uid
from .text import sentence_spans


def descriptions(comparison):
    return {
        field + "_description": comparison.get(field + "_description", comparison.get(field, ""))
        for field in ("intervention", "comparator", "outcome")
    }


def automatic_comparisons(result, threshold):
    """Require all three independently detected roles; keep missing-ICO losses."""
    rows = []
    for observation in result.get("clinical_observations", []):
        intervention, comparator, outcome = (
            observation.get(field) for field in ("intervention", "comparator", "outcome")
        )
        score = observation.get("ico_evidence_priority_score")
        if not all((intervention, comparator, outcome)) or score is None or score < threshold:
            continue
        rows.append(
            {
                "intervention_description": intervention["text"],
                "comparator_description": comparator["text"],
                "outcome_description": outcome["text"],
                "evidence": observation["evidence_quotes"],
                "observation_id": observation["observation_id"],
                "score": score,
            }
        )
    return rows


def provided_comparisons(document, comparisons, encoder, head, threshold):
    """Use provided prompt descriptions, never evidence spans as model inputs."""
    sentences = sentence_spans(document["text"], document["sections"])
    output = []
    for comparison in comparisons:
        prompt = descriptions(comparison)
        values = head.probabilities(
            [inference_features(document["text"], sentence, prompt, encoder) for sentence in sentences]
        )
        predicted, checked = [], []
        for sentence, value in zip(sentences, values):
            if value.get("1", 0) < threshold:
                continue
            quote = {"start": sentence["start"], "end": sentence["end"], "quote": sentence["text"]}
            predicted.append(quote)
            if state(sentence, {})[0] == "observed":
                checked.append(quote)
        output.append(
            {"prompt_id": comparison["prompt_id"], "evidence": predicted, "verified_evidence": checked}
        )
    return output


def _key(description):
    # Strict lexical identity only. Descriptions are not literal span gold and
    # partial word overlap must not manufacture an ICO match.
    return tuple(re.findall(r"\w+", description.casefold()))


def evidence_counts(document, automatic, provided):
    tokens = scoring._tokens(document["text"])
    expected, observed, verified, automatic_observed = set(), set(), set(), set()

    def add(target, prompt, quotes):
        for quote in quotes:
            lo, hi = quote["start"], quote["end"]
            surface = quote.get("text", quote.get("quote"))
            if not 0 <= lo < hi <= len(document["text"]) or document["text"][lo:hi] != surface:
                raise ValueError("Comparative evidence/source offset mismatch")
            target.update((prompt, i) for i, (start, end) in enumerate(tokens) if start < hi and lo < end)

    mapping = {}
    for comparison in document.get("comparisons", []):
        add(expected, comparison["prompt_id"], comparison["evidence"])
        key = tuple(_key(value) for value in descriptions(comparison).values())
        mapping.setdefault(key, []).append(comparison["prompt_id"])
    for comparison in provided:
        add(observed, comparison["prompt_id"], comparison["evidence"])
        add(verified, comparison["prompt_id"], comparison.get("verified_evidence", []))
    matched = set()
    unmatched_predictions = 0
    for index, comparison in enumerate(automatic):
        key = tuple(_key(value) for value in descriptions(comparison).values())
        if key not in mapping:
            # Off-prompt automatic predictions remain false positives rather
            # than disappearing from an end-to-end denominator.
            add(automatic_observed, "unmatched-automatic-" + str(index), comparison["evidence"])
            unmatched_predictions += 1
        else:
            for prompt in mapping[key]:
                matched.add(prompt)
                add(automatic_observed, prompt, comparison["evidence"])
    return {
        "provided_ico_evidence_tokens": scoring._metrics(expected, observed),
        "provided_ico_verified_tokens": scoring._metrics(expected, verified),
        "automatic_ico_evidence_tokens": scoring._metrics(expected, automatic_observed),
        "ico_alignment": {
            "gold_prompts": len(document.get("comparisons", [])),
            "matched_prompts": len(matched),
            "automatic_comparisons": len(automatic),
            "unmatched_automatic_comparisons": unmatched_predictions,
        },
    }


def development(settings, spec_path, provider="union", audits=()):
    from .operations_v6 import components, identity
    from .dictionaries import load_dictionaries
    from .registry_v6 import extract_engine

    spec = json.loads(Path(spec_path).read_bytes())
    if spec.get("official_split") != "development" or spec.get("corpus") != "evidenceinference":
        raise ValueError("Clinical development cannot open sealed test corpora")
    raw = Path(spec["inputs_path"]).read_bytes()
    if digest(raw) != spec["inputs_sha256"]:
        raise ValueError("Clinical source-only cohort changed")
    inputs = json.loads(raw)
    if inputs.get("split") != "development" or any(
        set(d) & {"comparisons", "evidence", "entities", "relations", "annotated_spans", "labels"}
        for d in inputs["documents"]
    ):
        raise ValueError("Automatic clinical predictions require source-only inputs")
    from .clinical_identity_v6 import auditable_identity

    if any(not auditable_identity(doc, Path(spec["inputs_path"]).parent) for doc in inputs["documents"]):
        raise ValueError(
            "Official PMCID-to-PMID mapping is required for independent clinical cohort auditing"
        )
    values = components(settings, provider)
    evidence = values["_v6_models"].manifest["clinical_heads"].get("evidence")
    if evidence is None:
        raise ValueError("An audited ICO evidence head is required")
    head = values["_v6_models"].heads["evidence"]
    dictionaries = load_dictionaries(settings, "heuristic-v2")
    artifact = identity(settings, values, dictionaries)
    pmids = {str(doc["pmid"]) for doc in inputs["documents"]}
    hashes = {digest(doc["text"]) for doc in inputs["documents"]}
    from .training_v4 import text_key

    normalized_hashes = {text_key(doc["text"]) for doc in inputs["documents"]}
    audits_by_hash = {digest(Path(path).read_bytes()): json.loads(Path(path).read_bytes()) for path in audits}
    for record in (values["_v6_models"].manifest, evidence):
        trained = record.get("training_pmids", [])
        if isinstance(trained, dict):
            trained = set().union(*map(set, trained.values()))
        audit = audits_by_hash.get(record.get("audit_sha256"))
        if audit is None:
            raise ValueError("Clinical development requires a protected-cohort training audit")
        if (
            not audit.get("passed")
            or audit.get("protected_overlap") != 0
            or pmids & set(trained)
            or not normalized_hashes <= set(audit["protected_text_hashes"])
        ):
            raise ValueError("Clinical development cohort was not protected before model fitting")
        from .audit_v6 import upstream_clinical

        upstream_clinical(values["_clinical_models"].path, audit)
    directory = (
        settings.home
        / "experiments/v6/clinical"
        / digest(artifact["artifact_identity"] + digest(raw) + provider)
    )
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "report.json").exists():
        report = json.loads((directory / "report.json").read_bytes())
        if not scoring.verify_receipt(settings, "v6_clinical_development", report):
            raise ValueError("Clinical development receipt changed")
        return report
    predictions = {}
    threshold = evidence["selection"]["threshold"]
    for doc in inputs["documents"]:
        if digest(doc["text"]) != doc["source_hash"]:
            raise ValueError("Clinical source hash changed")
        path = directory / ("paper-" + doc["pmid"] + ".json")
        if path.exists():
            prediction = json.loads(path.read_bytes())
            if (
                prediction.get("source_hash") != doc["source_hash"]
                or prediction.get("artifact_identity") != artifact["artifact_identity"]
            ):
                raise ValueError("Resumed clinical prediction belongs to a different source or artifact")
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
                "automatic_comparisons": automatic_comparisons(result, threshold),
            }
            scoring._write_immutable(path, prediction)
        predictions[doc["pmid"]] = prediction
    # Gold opens only after all automatic source-only predictions are durable.
    raw_gold = Path(spec["gold_path"]).read_bytes()
    if digest(raw_gold) != spec["gold_sha256"]:
        raise ValueError("Clinical development annotations changed")
    gold = json.loads(raw_gold)
    if gold.get("split") != "development" or {d["pmid"] for d in gold["documents"]} != set(predictions):
        raise ValueError("Clinical prediction and annotation cohorts differ")
    rows = []
    for doc in gold["documents"]:
        if (
            digest(doc["text"]) not in hashes
            or digest(doc["text"]) != predictions[doc["pmid"]]["source_hash"]
        ):
            raise ValueError("Clinical annotated source differs from prediction")
        path = directory / ("paper-provided-" + doc["pmid"] + ".json")
        if path.exists():
            provided = json.loads(path.read_bytes())
        else:
            provided = provided_comparisons(doc, doc["comparisons"], values["_encoder"], head, threshold)
            scoring._write_immutable(path, provided)
        rows.append(
            {
                "pmid": doc["pmid"],
                **evidence_counts(doc, predictions[doc["pmid"]]["automatic_comparisons"], provided),
            }
        )
    endpoints = (
        "provided_ico_evidence_tokens",
        "provided_ico_verified_tokens",
        "automatic_ico_evidence_tokens",
    )
    report = {
        "schema": "v6-clinical-development-1",
        "id": uid(),
        "at": now(),
        "artifact": artifact,
        "corpus": "evidenceinference",
        "split": "development",
        "papers": len(rows),
        "metrics": {key: scoring._aggregate(rows, key) for key in endpoints},
        "per_paper": rows,
        "intervals": {key: scoring._paper_intervals(rows, key, 2000, 20261010) for key in endpoints},
        "automatic_ico_mapping": "strict case/punctuation-normalized three-description identity; all unmatched predictions count as FP",
        "gold_policy": "union of valid annotators' evidence tokens per prompt",
        "ico_literal_span_accuracy": "not annotated; not measured",
        "effect_label_accuracy": "not measured",
        "confirmed_pmc_only_articles": sum(
            d.get("identifier_resolution") == "confirmed_pmc_only" for d in inputs["documents"]
        ),
        "nutrition_accuracy": "unmeasured",
        "pretraining_overlap": "unknown",
        "inputs_sha256": digest(raw),
        "gold_sha256": digest(raw_gold),
    }
    scoring._write_immutable(directory / "report.json", report)
    scoring._receipt(settings, "v6_clinical_development", report["id"], report)
    return report
