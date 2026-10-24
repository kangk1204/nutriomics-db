"""Input-first immutable experiments; native type, role and ID endpoints separate."""

from collections import Counter
import json
from pathlib import Path
import time

from . import benchmarks as scoring
from .db import digest, dumps, now, uid

ENDPOINT_NOTICE = "Native benchmark relation type + IDs; role direction is separate; verified source events are a stricter endpoint. Nutrition accuracy is unmeasured."


def ids(mention, mapping, oracle=False):
    if oracle:
        return set(
            mention.get("payload", {}).get("provided_native_ids", [])
            or mention.get("native_ids", [])
            or ([mention["normalized_id"]] if mention.get("normalized_id") else [])
        )
    return set(
        mapping.get(
            ("gene" if mention["kind"] == "gene" else "component", mention.get("normalized_id")), set()
        )
    )


def project_hypotheses(result, mapping, oracle=False):
    relations = []
    verified = []
    for h in result["relation_hypotheses"]:
        if not h["type_selected"]:
            continue
        chemicals = ids(h["chemical"], mapping, oracle) or {"UNRESOLVED_CHEMICAL:" + h["chemical"]["text"]}
        genes = ids(h["gene"], mapping, oracle) or {"UNRESOLVED_GENE:" + h["gene"]["text"]}
        for chemical in chemicals:
            for gene in genes:
                relation = {
                    "entity1": chemical,
                    "entity2": gene,
                    "predicate": h["benchmark_relation_type"],
                    "evidence": h["evidence"],
                    "semantic_verified": h["semantic_verified"],
                }
                if h["role_direction"] != "unknown":
                    relation["subject"] = chemical if h["role_direction"] == "chemical_to_gene" else gene
                relations.append(relation)
                if h["semantic_verified"]:
                    verified.append(relation)
    return relations, verified


def strict_gene_counts(document, prediction, mapping, oracle=False):
    expected = {
        (e["start"], e["end"], i)
        for e in document["entities"]
        if e["kind"] == "gene"
        for i in e.get("native_ids", [])
    }
    predicted = set()
    for e in prediction["entities"]:
        if e["kind"] == "gene":
            for identifier in ids(e, mapping, oracle) or {"UNRESOLVED"}:
                predicted.add((e["start"], e["end"], identifier))
    return scoring._metrics(expected, predicted)


def candidate_coverage(document, prediction):
    """Gold relation coverage before the ID/type gate, including unsupported pairs."""
    gold, _, _, _ = relation_sets(document, {"relations": []})
    by_id = {}
    for entity in document.get("entities", []):
        for identifier in entity.get("native_ids", []):
            by_id.setdefault(identifier, []).append(entity)

    def same(left, right):
        return (left["kind"], left["start"], left["end"]) == (right["kind"], right["start"], right["end"])

    matched = 0
    for key in gold:
        first, second = (by_id.get(identifier, []) for identifier in key[1])
        if any(
            (any(same(a, e) for e in first) and any(same(b, e) for e in second))
            or (any(same(b, e) for e in first) and any(same(a, e) for e in second))
            for a, b in prediction["candidate_pairs"]
        ):
            matched += 1
    return {
        "tp": matched,
        "fn": len(gold) - matched,
        "gold_relations": len(gold),
        "recall": matched / len(gold) if gold else None,
        "identities_not_required": True,
    }


def relation_sets(document, prediction):
    gold, observed, directed, _ = scoring._relation_sets(document, {"relations": []})
    observed = set()
    observed_directed = set()
    labelled_pairs = {(k[0], k[1]) for k in directed}
    for relation in prediction["relations"]:
        key = (
            document["pmid"],
            tuple(sorted((relation["entity1"], relation["entity2"]))),
            relation["predicate"],
        )
        observed.add(key)
        if relation.get("subject") and (key[0], key[1]) in labelled_pairs:
            observed_directed.add((*key, relation["subject"]))
    return gold, observed, directed, observed_directed


def failures(document, prediction, mapping, oracle):
    gold, observed, directed, predicted_direction = relation_sets(document, prediction)
    by_id = {}
    for e in document["entities"]:
        for i in e.get("native_ids", []):
            by_id.setdefault(i, []).append(e)
    stages = Counter()
    direction_stages = Counter()
    errors = []
    for key in sorted(gold):
        if key in observed:
            stages["matched_type_ids"] += 1
            continue
        pair = key[1]
        entities = [by_id.get(i, []) for i in pair]
        detected = [
            [
                m
                for m in prediction["entities"]
                if any((m["kind"], m["start"], m["end"]) == (g["kind"], g["start"], g["end"]) for g in es)
            ]
            for es in entities
        ]
        if not all(detected):
            stage = "entity_boundary"
        elif not all(
            any(pair[j] in ids(m, mapping, oracle) for m in group) for j, group in enumerate(detected)
        ):
            stage = "identity_mapping"
        elif not any(
            any((a["start"], a["end"]) == (m["start"], m["end"]) for m in detected[0])
            and any((b["start"], b["end"]) == (m["start"], m["end"]) for m in detected[1])
            or any((a["start"], a["end"]) == (m["start"], m["end"]) for m in detected[1])
            and any((b["start"], b["end"]) == (m["start"], m["end"]) for m in detected[0])
            for a, b in prediction["candidate_pairs"]
        ):
            stage = "same_sentence_candidate_unsupported"
        else:
            stage = "type_classification"
        stages[stage] += 1
        errors.append(
            {"pmid": document["pmid"], "kind": "false_negative", "relation": key, "first_failure": stage}
        )
    for key in directed:
        direction_stages[
            "matched_direction"
            if key in predicted_direction
            else "type_or_id"
            if key[:3] not in observed
            else "role_or_semantic_validation"
        ] += 1
    fp = Counter()
    for key in observed - gold:
        stage = (
            "unresolved_id"
            if any(i.startswith("UNRESOLVED") for i in key[1])
            else "wrong_type"
            if any(g[1] == key[1] for g in gold)
            else "wrong_pair_or_entity"
        )
        fp[stage] += 1
        errors.append(
            {"pmid": document["pmid"], "kind": "false_positive", "relation": key, "first_failure": stage}
        )
    return dict(stages), dict(direction_stages), dict(fp), errors


def _metrics_for(doc, pred, mapping, oracle, corpus):
    gene_gold = {(e["start"], e["end"]) for e in doc.get("entities", []) if e["kind"] == "gene"}
    gene_pred = {(e["start"], e["end"]) for e in pred["entities"] if e["kind"] == "gene"}
    result = {}
    if corpus == "bioredirect":
        g, p, gd, pd = relation_sets(doc, pred)
        vg, vp, _, _ = relation_sets(doc, {"relations": pred["verified_relations"]})
        result = {
            "gene": scoring._metrics(gene_gold, gene_pred),
            "gene_strict_ids": strict_gene_counts(doc, pred, mapping, oracle),
            "relation": scoring._metrics(g, p),
            "relation_direction": scoring._metrics(gd, pd),
            "verified_relation": scoring._metrics(vg, vp),
        }
    if corpus == "bioscope":
        expected, observed = scoring._span_sets(doc, pred, "scopes", "kind")
        for kind in ("negation", "speculation"):
            result["scopes_" + kind] = scoring._metrics(
                {k for k in expected if k[1] == kind}, {k for k in observed if k[1] == kind}
            )
    if corpus in {"ebmnlp", "evidenceoutcomes"}:
        kinds = ("participants", "interventions", "outcomes") if corpus == "ebmnlp" else ("outcomes",)
        expected, observed = scoring._span_sets(doc, pred, "annotated_spans", "label", kinds)
        for kind in kinds:
            result["spans_" + kind] = scoring._metrics(
                {k for k in expected if k[1] == kind}, {k for k in observed if k[1] == kind}
            )
    return result


def development(
    settings,
    corpus,
    lexicon,
    provider="union",
    mode="end_to_end",
    ablation="context",
    level="abstract",
    limit=200,
):
    from .operations_v5 import components, identity
    from .dictionaries import load_dictionaries
    from .evaluation_v4 import _mapping
    from .chemical_v3 import load_snapshot
    from .registry_v5 import extract_engine

    index = json.loads((settings.home / "public_benchmarks/v2-measurement-index.json").read_bytes())
    prepared = index["steps"]["prepare:development:" + corpus + ":" + level]
    inputs, receipt, input_sha = scoring._load_inputs(settings, prepared["inputs_path"])
    if inputs["split"] != "development":
        raise ValueError("Development cannot open protected tests")
    documents = sorted(inputs["documents"], key=lambda d: digest("v3-development-20261003:" + d["pmid"]))[
        :limit
    ]
    values = components(settings, provider)
    dictionaries = load_dictionaries(settings, "heuristic-v2")
    artifact = identity(settings, values, dictionaries)
    experiment = {
        "artifact": artifact,
        "input_sha256": input_sha,
        "corpus": corpus,
        "mode": mode,
        "provider": provider,
        "ablation": ablation,
        "source_level": level,
        "pmids": [d["pmid"] for d in documents],
        "lexicon_sha256": digest(Path(lexicon).read_bytes()),
    }
    directory = settings.home / "experiments/v5" / digest(dumps(experiment))
    directory.mkdir(parents=True, exist_ok=True)
    identity_path = directory / "identity.json"
    if not identity_path.exists():
        scoring._write_immutable(identity_path, experiment)
    if json.loads(identity_path.read_bytes()) != experiment:
        raise ValueError("Experiment identity changed")
    if (directory / "report.json").exists():
        report = json.loads((directory / "report.json").read_bytes())
        if not scoring.verify_receipt(settings, "public_v5_development_experiment", report):
            raise ValueError("Saved experiment receipt differs")
        return report
    oracle = {}
    if mode == "gold_entities":
        path = Path(prepared["inputs_path"]).with_name("oracle_entities.json")
        if digest(path.read_bytes()) != receipt["oracle_entities_sha256"]:
            raise ValueError("Entity oracle changed")
        oracle = {d["pmid"]: d for d in json.loads(path.read_bytes())["documents"]}
        if any(set(d) != {"pmid", "source_hash", "entities"} for d in oracle.values()):
            raise ValueError("Non-entity labels in oracle")
    mapping, _ = _mapping(settings, dictionaries, values, lexicon)
    chemistry = load_snapshot(lexicon)
    predictions = {}
    started = time.monotonic()
    for doc in documents:
        path = directory / ("paper-" + doc["pmid"] + ".json")
        if path.exists():
            pred = json.loads(path.read_bytes())
            if (
                pred["artifact_identity"] != artifact["artifact_identity"]
                or pred["source_hash"] != doc["source_hash"]
            ):
                raise ValueError("Prediction source/artifact differs on resume")
        else:
            metadata = {
                **values,
                "_chemical_lexicon": chemistry,
                "_artifact_identity": artifact["artifact_identity"],
                "_ablation": ablation,
                "_compare_relations": corpus == "bioredirect",
            }
            if mode == "gold_entities":
                metadata["_gold_entities"] = [
                    e for e in oracle[doc["pmid"]]["entities"] if e["kind"] in {"gene", "component"}
                ]
            result = extract_engine(doc["text"], doc["sections"], dictionaries, metadata, "heuristic-v5")
            if (
                result["rules_version"] != artifact["rules_version"]
                or result["manifest"]["rule_source_hashes"] != artifact["rule_source_hashes"]
            ):
                raise ValueError("Scientific sources changed during prediction")
            relations, verified = project_hypotheses(result, mapping, mode == "gold_entities")
            if ablation == "entities":
                from .evaluation_v4 import project_source_event

                relations = [
                    p
                    for e in result["events"]
                    if (
                        p := project_source_event(
                            e,
                            {"entities": oracle.get(doc["pmid"], {}).get("entities", [])},
                            mapping,
                            mode == "gold_entities",
                        )
                    )
                ]
                verified = relations
            pred = {
                "pmid": doc["pmid"],
                "group_id": doc.get("group_id", doc["pmid"]),
                "source_hash": doc["source_hash"],
                "artifact_identity": artifact["artifact_identity"],
                "entities": result["mentions"],
                "events": result["events"],
                "relations": relations,
                "verified_relations": verified,
                "scopes": result["benchmark_scopes"],
                "annotated_spans": result["annotated_spans"],
                "summary": result["summary"],
                "candidate_pairs": result["candidate_pairs"],
                "entity_hypotheses": result["entity_hypotheses"],
                "relation_hypotheses": result["relation_hypotheses"],
                "relation_comparison_hypotheses": result["relation_comparison_hypotheses"],
                "decision_trace": result["decision_trace"],
                "diagnostics": result["diagnostics"],
                "manifest": result["manifest"],
            }
            scoring._write_immutable(path, pred)
        predictions[doc["pmid"]] = pred
        if len(predictions) % 10 == 0:
            print(
                dumps(
                    {
                        "stage": "development_predict",
                        "corpus": corpus,
                        "mode": mode,
                        "provider": provider,
                        "papers": len(predictions),
                    }
                ),
                flush=True,
            )
    # Labels become readable only after every source-only prediction is durable.
    gold, _, gold_sha = scoring._load_gold(settings, prepared["gold_path"])
    labels = {d["pmid"]: d for d in gold["documents"]}
    per_paper = []
    fn = Counter()
    direction_fn = Counter()
    fp = Counter()
    errors = []
    identities = Counter()
    for doc in documents:
        pred = predictions[doc["pmid"]]
        label = labels[doc["pmid"]]
        metrics = _metrics_for(label, pred, mapping, mode == "gold_entities", corpus)
        per_paper.append({"pmid": doc["pmid"], "group_id": doc.get("group_id", doc["pmid"]), **metrics})
        if corpus == "bioredirect":
            a, b, c, e = failures(label, pred, mapping, mode == "gold_entities")
            fn.update(a)
            direction_fn.update(b)
            fp.update(c)
            errors.extend(e)
        for entity in pred["entities"]:
            if entity["kind"] == "gene":
                identities[entity.get("payload", {}).get("identity_reason") or "resolved"] += 1
    endpoints = set().union(*(set(p) - {"pmid", "group_id"} for p in per_paper))
    metrics = {key: scoring._aggregate(per_paper, key) for key in sorted(endpoints)}
    report = {
        "id": uid(),
        "at": now(),
        "engine": "heuristic-v5",
        "corpus": corpus,
        "provider": provider,
        "mode": mode,
        "ablation": ablation,
        "source_level": level,
        "profile": "local",
        "papers": len(documents),
        "metrics": metrics,
        "failure_counts": dict(fn),
        "direction_failure_counts": dict(direction_fn),
        "false_positive_counts": dict(fp),
        "gene_identity_reasons": dict(identities),
        "artifact": artifact,
        "input_sha256": input_sha,
        "gold_sha256": gold_sha,
        "per_paper": per_paper,
        "error_examples": errors[:50],
        "relation_evaluation_scope": ENDPOINT_NOTICE,
        "all_unsupported_and_cross_sentence_count_as_fn": True,
        "oracle_multi_ids_preserved": True,
        "candidate_pairs": sum(p["diagnostics"]["candidate_pairs"] for p in predictions.values()),
        "candidate_id_unresolved": sum(
            p["diagnostics"]["id_unresolved_candidates_kept"] for p in predictions.values()
        ),
        "type_hypotheses": sum(p["diagnostics"]["type_selected"] for p in predictions.values()),
        "semantic_verified": sum(p["diagnostics"]["semantic_verified"] for p in predictions.values()),
        "development_exposure": True,
        "nutrition_accuracy": "unmeasured",
        "review_time_saved": "unmeasured",
        "elapsed_seconds": time.monotonic() - started,
        "output_path": str(directory),
    }
    report["intervals"] = {key: scoring._paper_intervals(per_paper, key, 2000, 20261009) for key in endpoints}
    if corpus == "bioredirect":
        coverage = [candidate_coverage(labels[doc["pmid"]], predictions[doc["pmid"]]) for doc in documents]
        total = sum(c["gold_relations"] for c in coverage)
        covered = sum(c["tp"] for c in coverage)
        report["candidate_coverage"] = {
            "tp": covered,
            "fn": total - covered,
            "gold_relations": total,
            "recall": covered / total if total else None,
            "includes_unsupported": True,
        }
        report["selected_type_semantic_abstention_rate"] = (
            (1 - report["semantic_verified"] / report["type_hypotheses"])
            if report["type_hypotheses"]
            else None
        )
        from .features_v5 import THRESHOLDS

        comparison = []
        for configuration in values["_learned_models"].manifest.get("relation_configurations", []):
            for threshold in THRESHOLDS:
                rows = []
                for doc in documents:
                    pred = predictions[doc["pmid"]]
                    hypotheses = [
                        {**h, "type_selected": h["priority_score"] >= threshold}
                        for h in pred["relation_comparison_hypotheses"]
                        if h["configuration"] == configuration
                    ]
                    relations, verified = project_hypotheses(
                        {"relation_hypotheses": hypotheses}, mapping, mode == "gold_entities"
                    )
                    g, p, gd, pd = relation_sets(labels[doc["pmid"]], {"relations": relations})
                    vg, vp, _, _ = relation_sets(labels[doc["pmid"]], {"relations": verified})
                    rows.append(
                        {
                            "pmid": doc["pmid"],
                            "relation": scoring._metrics(g, p),
                            "relation_direction": scoring._metrics(gd, pd),
                            "verified_relation": scoring._metrics(vg, vp),
                        }
                    )
                comparison.append(
                    {
                        "configuration": configuration,
                        "threshold": threshold,
                        "metrics": {
                            e: scoring._aggregate(rows, e)
                            for e in ("relation", "relation_direction", "verified_relation")
                        },
                    }
                )
        eligible = [c for c in comparison if (c["metrics"]["relation"]["precision"] or 0) >= 0.85]
        best = (
            max(
                eligible,
                key=lambda c: (
                    (c["metrics"]["relation"]["recall"] or 0),
                    (c["metrics"]["relation"]["f1"] or 0),
                ),
            )
            if eligible
            else None
        )
        report["relation_configuration_comparisons"] = comparison
        report["development_relation_selection"] = best
        report["development_relation_goal_met"] = bool(best and best["metrics"]["relation"]["recall"] >= 0.3)
    scoring._write_immutable(directory / "report.json", report)
    scoring._receipt(settings, "public_v5_development_experiment", report["id"], report)
    return report


def reports(settings, profile=None):
    items = []
    for path in sorted((settings.home / "experiments/v5").glob("*/report.json")):
        report = json.loads(path.read_bytes())
        if profile and report.get("profile") != profile:
            continue
        if scoring.verify_receipt(settings, "public_v5_development_experiment", report):
            items.append(report)
    return {"engine": "heuristic-v5", "items": items, "nutrition_accuracy": "unmeasured"}


def experiment_family(artifact):
    """Compare providers within one frozen science/model/threshold configuration."""
    learned = artifact.get("learned_models", {})
    return digest(
        dumps(
            {
                k: artifact.get(k)
                for k in (
                    "rules_version",
                    "clinical_models",
                    "gene_dictionary",
                    "dictionary_version",
                    "model_sha256",
                    "profile",
                    "dependencies",
                    "evaluator_sha256",
                )
            }
            | {
                "learned_manifest": learned.get("manifest_sha256"),
                "relation_configuration": learned.get("active_relation_configuration"),
                "relation_threshold": learned.get("relation_threshold"),
            }
        )
    )


def selection(items):
    from .registry_v5 import engine_rules_version

    current = [
        r for r in items if r.get("artifact", {}).get("rules_version") == engine_rules_version("heuristic-v5")
    ]
    latest = max(current, key=lambda r: r["at"], default=None)
    family = experiment_family(latest["artifact"]) if latest else None
    auto = [
        r
        for r in current
        if r["corpus"] == "bioredirect"
        and r["mode"] == "end_to_end"
        and r["ablation"] == "context"
        and r["papers"] == 100
        and experiment_family(r["artifact"]) == family
    ]
    eligible = [
        r
        for r in auto
        if (r["metrics"]["gene"]["precision"] or 0) >= 0.9 and (r["metrics"]["gene"]["recall"] or 0) >= 0.7
    ]
    selected = (
        max(eligible, key=lambda r: (r["metrics"]["gene_strict_ids"]["f1"], -r["elapsed_seconds"]))
        if eligible
        else None
    )
    return {
        "selected_provider": selected["provider"] if selected else None,
        "qualifying_gene_configuration": bool(selected),
        "experiment_family": family,
        "default_engine_unchanged": True,
        "reason": None if selected else "gene_precision_or_recall_goal_not_met",
    }


def compare(settings, items):
    """Pair only identical papers, modes and evaluation definitions."""
    comparisons = []
    for candidate in items:
        if candidate["corpus"] != "bioredirect":
            continue
        for baseline in items:
            if (
                baseline["corpus"] != candidate["corpus"]
                or baseline["mode"] != candidate["mode"]
                or baseline["provider"] != candidate["provider"]
                or experiment_family(baseline["artifact"]) != experiment_family(candidate["artifact"])
            ):
                continue
            if (baseline["ablation"], candidate["ablation"]) not in {
                ("entities", "relations"),
                ("relations", "context"),
            }:
                continue
            common = set(p["pmid"] for p in baseline["per_paper"]) & set(
                p["pmid"] for p in candidate["per_paper"]
            )
            if len(common) != baseline["papers"] or len(common) != candidate["papers"]:
                continue
            comparisons.append(
                {
                    "baseline": baseline["id"],
                    "candidate": candidate["id"],
                    "papers": len(common),
                    "endpoints": {
                        e: scoring.paired_bootstrap(
                            sorted(common),
                            {p["pmid"]: p for p in baseline["per_paper"]},
                            {p["pmid"]: p for p in candidate["per_paper"]},
                            e,
                            2000,
                            20261009,
                        )
                        for e in (
                            "gene",
                            "gene_strict_ids",
                            "relation",
                            "relation_direction",
                            "verified_relation",
                        )
                    },
                    "warning": "Entity-only ablation uses legacy effect-to-benchmark projection; type comparison has different label semantics",
                }
            )
    return comparisons


def freeze(settings, audit, out):
    from .operations_v5 import components, identity
    from .dictionaries import load_dictionaries

    summary = reports(settings)["items"]
    chosen = selection(summary)
    if not chosen["qualifying_gene_configuration"]:
        raise ValueError("V5 cannot freeze final selection before development gene goals pass")
    summary = [r for r in summary if experiment_family(r["artifact"]) == chosen["experiment_family"]]
    selected = [
        r
        for r in summary
        if r["corpus"] == "bioredirect"
        and r["ablation"] == "context"
        and r["papers"] == 100
        and (r["provider"] == chosen["selected_provider"] or r["mode"] == "gold_entities")
    ]
    for mode in ("end_to_end", "gold_entities"):
        values = [r for r in selected if r["mode"] == mode]
        if not values or any(
            (r["metrics"][k]["precision"] or 0) < 0.85 or (r["metrics"][k]["recall"] or 0) < 0.3
            for r in values
            for k in ("relation", "relation_direction", "verified_relation")
        ):
            raise ValueError("Relation type/role/verified development goals not met: " + mode)
    for level in ("abstract", "fulltext"):
        values = [r for r in summary if r["corpus"] == "bioscope" and r["source_level"] == level]
        if not values or any(
            (r["metrics"]["scopes_" + k]["f1"] or 0) < 0.85
            for r in values
            for k in ("negation", "speculation")
        ):
            raise ValueError("Scope development goals not met: " + level)
    record = json.loads(Path(audit).read_bytes())
    if record["train_heldout_overlap"] or record["bc8_annotation_labels_read"]:
        raise ValueError("Held-out audit failed")
    artifact = identity(
        settings,
        components(settings, chosen["selected_provider"]),
        load_dictionaries(settings, "heuristic-v2"),
    )
    if experiment_family(artifact) != chosen["experiment_family"]:
        raise ValueError("Current artifact differs from qualifying development experiments")
    value = {
        "id": artifact["artifact_identity"],
        "schema": "v5-final-freeze",
        "artifact": artifact,
        "audit_sha256": digest(Path(audit).read_bytes()),
        "reports": [r["id"] for r in summary],
        "report_hashes": [digest(dumps(r)) for r in summary],
        "created_at": now(),
    }
    scoring._write_immutable(Path(out), value)
    scoring._receipt(settings, "v5_final_freeze", artifact["artifact_identity"], value)
    return value


def final_evaluate(settings, spec, freeze_path):
    # Deliberately require an authentic freeze receipt before any test file access.
    record = json.loads(Path(freeze_path).read_bytes())
    if not scoring.verify_receipt(settings, "v5_final_freeze", record):
        raise ValueError("Verified qualifying final freeze is required")
    from .final_v5 import evaluate

    return evaluate(settings, spec, record)
