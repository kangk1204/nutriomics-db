"""Evaluate v6-r1 with frozen v6 scoring; preserve source-first predictions."""

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def save(path, value):
    from nutri_literature.benchmarks import _write_immutable

    _write_immutable(path, value)


def relation_rows(document, mentions, sentences, values, joint, manifest, oracle):
    from nutri_literature.extraction_v6 import native_event
    from nutri_literature.features_v5 import pair_candidates
    from nutri_literature.nlp_v2 import parser_for, validate_tokens
    from nutri_literature.embeddings_v6 import pooled_pair_features
    from nutri_literature.scope_v5 import predict as predict_scope
    from nutri_literature.refinement_v6 import selected_types, automatic_relation_eligible

    parser = parser_for(values)

    def parse(sentence):
        return validate_tokens(parser.parse(sentence), sentence)

    pairs = pair_candidates(sentences, mentions, parse)
    grouped = defaultdict(list)
    for pair in pairs:
        grouped[tuple(m["payload"]["mention_cluster_id"] for m in (pair["chemical"], pair["gene"]))].append(
            pair
        )
    bags = list(grouped.values())
    features = [pooled_pair_features(b, document["text"], values["_encoder"]) for b in bags]
    models = values["_v6_models"]
    old = models.predict(features)
    new = joint.predict(features)
    scopes = predict_scope(sentences, values["_learned_models"])
    hypotheses = {"baseline": [], "identity_gate": [], "joint": []}
    decisions = Counter()
    for bag, previous, fresh in zip(bags, old, new):
        directions = previous["direction"]
        role = max(directions, key=directions.get) if directions else "unknown"
        if directions.get(role, 0) < 0.7:
            role = "unknown"
        exists = previous["existence"] >= models.manifest["selections"]["existence"]["threshold"]
        chosen = set(selected_types(fresh, manifest["selection"]["threshold"]))
        for label, score in previous["types"].items():
            selected = exists and score >= models.manifest["selections"]["type_" + label]["threshold"]
            event, reasons, pair = None, ["type_or_existence_threshold"], bag[0]
            if selected or label in chosen:
                checked = [(p, *native_event(p, label, role, scopes, values, mentions)) for p in bag]
                pair, event, reasons = min(
                    checked, key=lambda x: (x[1] is None, len(x[2]), x[0]["sentence"]["start"])
                )
            row = {
                "chemical": pair["chemical"],
                "gene": pair["gene"],
                "benchmark_relation_type": label,
                "role_direction": role,
                "type_selected": selected,
                "semantic_verified": event is not None and not reasons,
                "priority_score": float(score),
                "existence_priority_score": previous["existence"],
                "reasons": reasons,
                "evidence": {
                    "start": pair["sentence"]["start"],
                    "end": pair["sentence"]["end"],
                    "quote": pair["sentence"]["text"],
                },
            }
            # For the baseline, unselected labels must use bag[0] exactly as v6
            # did, even if the joint classifier selects that label.
            baseline = deepcopy(row)
            if not selected:
                baseline.update(
                    chemical=bag[0]["chemical"],
                    gene=bag[0]["gene"],
                    semantic_verified=False,
                    reasons=["type_or_existence_threshold"],
                )
            hypotheses["baseline"].append(baseline)
            eligible, gate_reasons = automatic_relation_eligible(baseline, oracle)
            hypotheses["identity_gate"].append(
                {**baseline, "type_selected": selected and eligible, "automatic_gate_reasons": gate_reasons}
            )
            # Keep every candidate; eligibility changes the automatic decision.
            eligible, gate_reasons = automatic_relation_eligible(row, oracle, require_selected_span=True)
            hypotheses["joint"].append(
                {
                    **row,
                    "type_selected": label in chosen and eligible,
                    "automatic_gate_reasons": gate_reasons,
                    "joint_class_scores": fresh,
                }
            )
            if selected and not eligible:
                decisions.update(gate_reasons)
    return hypotheses, dict(decisions), pairs


def native_mapping(entities):
    mapping = {}
    for e in entities:
        identifier, native = e.get("normalized_id"), e.get("payload", {}).get("native_id")
        kind = "gene" if e["kind"] == "gene" else "component"
        if native and identifier:
            mapping[(kind, identifier)] = {str(native)}
        elif identifier and identifier.startswith(("MESH:", "NCBIGene:")):
            mapping[(kind, identifier)] = {identifier.split(":", 1)[1]}
    return mapping


def relation_metrics(gold, relations, verified):
    from nutri_literature import evaluation_v6 as scoring, benchmarks

    expected, predicted, directed, observed_direction = scoring.relation_sets(gold, {"relations": relations})
    _, checked, _, _ = scoring.relation_sets(gold, {"relations": verified})
    return {
        "relation": benchmarks._metrics(expected, predicted),
        "relation_direction": benchmarks._metrics(directed, observed_direction),
        "verified_relation": benchmarks._metrics(expected, checked),
    }


def relations(args, settings, output):
    from nutri_literature import benchmarks as scoring, evaluation_v6
    from nutri_literature.candidates_v6 import collect
    from nutri_literature.db import digest, dumps, now, uid
    from nutri_literature.dictionaries import load_dictionaries
    from nutri_literature.operations_v6 import components
    from nutri_literature.refinement_v6 import load_artifact
    from nutri_literature.text import sentence_spans

    heads, manifest = load_artifact(args.refinement / "joint-relation")
    joint = heads["joint-relation"]
    index = json.loads((settings.home / "public_benchmarks/v2-measurement-index.json").read_bytes())
    prepared = index["steps"]["prepare:development:bioredirect:abstract"]
    inputs, receipt, sha = scoring._load_inputs(settings, prepared["inputs_path"])
    assert inputs["split"] == "development"
    documents = sorted(inputs["documents"], key=lambda d: digest("v3-development-20261003:" + d["pmid"]))[
        :100
    ]
    assert not set(manifest["training_pmids"]) & {d["pmid"] for d in documents}
    oracle = {}
    if args.mode == "gold_entities":
        path = Path(prepared["inputs_path"]).with_name("oracle_entities.json")
        assert digest(path.read_bytes()) == receipt["oracle_entities_sha256"]
        oracle = {d["pmid"]: d for d in json.loads(path.read_bytes())["documents"]}
    values = components(settings, "union")
    dictionaries = load_dictionaries(settings, "heuristic-v2")
    output = output / args.mode
    output.mkdir(parents=True, exist_ok=True)
    identity = artifact(args, manifest, sha)
    identity["frozen_relation_manifest_sha256"] = values["_v6_models"].manifest["manifest_sha256"]
    identity["frozen_clinical_base_manifest_sha256"] = values["_clinical_models"].manifest["manifest_sha256"]
    identity["candidate_provider"] = values["_candidate_provider"].manifest
    identity["gene_dictionary_sha256"] = values["_gene_index"].manifest["records_sha256"]
    identity["chemical_dictionary_sha256"] = values["_chemical_lexicon"]["manifest"]["snapshot_sha256"]
    predictions = {}
    for doc in documents:
        check_sources(identity)
        path = output / ("paper-" + doc["pmid"] + ".json")
        if path.exists():
            pred = json.loads(path.read_bytes())
            assert pred["identity"] == identity and pred["source_hash"] == doc["source_hash"]
        else:
            metadata = {**values}
            if oracle:
                metadata["_gold_entities"] = [
                    e for e in oracle[doc["pmid"]]["entities"] if e["kind"] in {"gene", "component"}
                ]
            sentences = sentence_spans(doc["text"], doc["sections"])
            mentions, _, entity_hypotheses, clusters = collect(doc["text"], sentences, dictionaries, metadata)
            sentences = values["_clinical_models"].predict(sentences)
            hypotheses, decisions, pairs = relation_rows(
                doc, mentions, sentences, metadata, joint, manifest, bool(oracle)
            )
            mapping = native_mapping(mentions)
            outputs = {}
            for stage, rows in hypotheses.items():
                rels, verified = evaluation_v6.project_hypotheses(
                    {"relation_hypotheses": rows}, mapping, bool(oracle)
                )
                outputs[stage] = {
                    "relations": rels,
                    "verified_relations": verified,
                    "relation_hypotheses": rows,
                }
            pred = {
                "pmid": doc["pmid"],
                "source_hash": doc["source_hash"],
                "identity": identity,
                "entities": mentions,
                "entity_hypotheses": entity_hypotheses,
                "mention_clusters": clusters,
                "candidate_pairs": [[p["chemical"], p["gene"]] for p in pairs],
                "decisions": decisions,
                "outputs": outputs,
            }
            save(path, pred)
        predictions[doc["pmid"]] = pred
        print(
            dumps(
                {
                    "stage": "relation_predict",
                    "mode": args.mode,
                    "papers": len(predictions),
                    "total": len(documents),
                }
            ),
            flush=True,
        )
    # No development relation annotation is read before all source predictions exist.
    gold, _, gold_sha = scoring._load_gold(settings, prepared["gold_path"])
    labels = {d["pmid"]: d for d in gold["documents"]}
    rows = {stage: [] for stage in ("baseline", "identity_gate", "joint")}
    diagnostics = {}
    for doc in documents:
        pred, target = predictions[doc["pmid"]], labels[doc["pmid"]]
        assert digest(target["text"]) == pred["source_hash"]
        for stage, values in pred["outputs"].items():
            metrics = relation_metrics(target, values["relations"], values["verified_relations"])
            rows[stage].append({"pmid": doc["pmid"], **metrics})
            projected = {**pred, **values}
            errors = evaluation_v6.failures(target, projected, native_mapping(pred["entities"]), bool(oracle))
            diagnostics.setdefault(stage, {"fn": Counter(), "fp": Counter(), "examples": []})
            diagnostics[stage]["fn"].update(errors[0])
            diagnostics[stage]["fp"].update(errors[2])
            diagnostics[stage]["examples"].extend(errors[3])
    metrics = {
        stage: {
            key: scoring._aggregate(values, key)
            for key in ("relation", "relation_direction", "verified_relation")
        }
        for stage, values in rows.items()
    }
    expected = {"end_to_end": (11, 256, 143), "gold_entities": (46, 138, 108)}[args.mode]
    actual = metrics["baseline"]["relation"]
    identical_baseline_counts = tuple(actual[k] for k in ("tp", "fp", "fn")) == expected
    paired = {}
    baseline = {row["pmid"]: row for row in rows["baseline"]}
    for stage in ("identity_gate", "joint"):
        candidate = {row["pmid"]: row for row in rows[stage]}
        paired[stage] = scoring.paired_bootstrap(
            sorted(baseline), baseline, candidate, "relation", 2000, 20261010
        )
    report = {
        "id": uid(),
        "at": now(),
        "identity": identity,
        "schema": "v6-r1-relation-development-1",
        "papers": len(documents),
        "mode": args.mode,
        "metrics": metrics,
        "per_paper": rows,
        "input_sha256": sha,
        "gold_sha256": gold_sha,
        "paired": paired,
        "original_v6_baseline_counts_reproduced": identical_baseline_counts,
        "diagnostics": {
            k: {"fn": dict(v["fn"]), "fp": dict(v["fp"]), "examples": v["examples"][:50]}
            for k, v in diagnostics.items()
        },
        "full_engine_evaluation": False,
        "evaluation_path": "unchanged-v6-relation-DAG",
        "sealed_test_opened": False,
        "nutrition_accuracy": "unmeasured",
        "default_engine": "heuristic-v1",
    }
    save(output / "report.json", report)
    scoring._receipt(settings, "v6_r1_relation_development", report["id"], report)
    print(
        dumps(
            {
                "report": str(output / "report.json"),
                "metrics": metrics,
                "original_v6_baseline_counts_reproduced": identical_baseline_counts,
            }
        ),
        flush=True,
    )


def artifact(args, manifest, input_sha):
    from nutri_literature.db import digest
    from nutri_literature.extraction_v6 import source_manifest
    from nutri_literature import refinement_v6, clinical_refinement_v6, evaluation_v6, clinical_evaluation_v6

    return {
        "variant": "heuristic-v6-r1",
        "frozen_v6_sources": source_manifest()[1],
        "refinement_source": digest(Path(refinement_v6.__file__).read_bytes()),
        "clinical_refinement_source": digest(Path(clinical_refinement_v6.__file__).read_bytes()),
        "runner_source": digest(Path(__file__).read_bytes()),
        "relation_evaluator": digest(Path(evaluation_v6.__file__).read_bytes()),
        "clinical_evaluator": digest(Path(clinical_evaluation_v6.__file__).read_bytes()),
        "model_manifest": digest(json.dumps(manifest, sort_keys=True)),
        "input_sha256": input_sha,
    }


def check_sources(identity):
    from nutri_literature.extraction_v6 import source_manifest
    from nutri_literature.refinement_v6 import source_identity
    from nutri_literature import clinical_refinement_v6
    from nutri_literature.db import digest

    assert source_manifest()[1] == identity["frozen_v6_sources"]
    assert source_identity() == identity["refinement_source"]
    assert (
        digest(Path(clinical_refinement_v6.__file__).read_bytes()) == identity["clinical_refinement_source"]
    )
    assert digest(Path(__file__).read_bytes()) == identity["runner_source"]


def clinical(args, settings, output):
    from nutri_literature import benchmarks as scoring
    from nutri_literature.refinement_v6 import load_artifact
    from nutri_literature.clinical_refinement_v6 import comparisons
    from nutri_literature.clinical_evaluation_v6 import evidence_counts
    from nutri_literature.models_v6 import Models
    from nutri_literature.embeddings_v6 import FrozenEncoder
    from nutri_literature.db import digest, dumps, uid, now

    role_models, manifest = load_artifact(args.refinement / "clinical-roles")
    spec = json.loads(args.spec.read_bytes())
    assert spec["official_split"] == "development"
    raw = Path(spec["inputs_path"]).read_bytes()
    assert digest(raw) == spec["inputs_sha256"]
    inputs = json.loads(raw)
    assert not set(manifest["training_pmids"]) & {d["pmid"] for d in inputs["documents"]}
    assert all(not set(d) & {"comparisons", "labels", "evidence"} for d in inputs["documents"])
    models = Models(args.models / "learned-v6")
    models.load()
    encoder = FrozenEncoder(args.models / "gene/selected_model", settings.home / "embedding-cache-v6")
    encoder.load()
    head = models.heads["evidence"]
    threshold = models.manifest["clinical_heads"]["evidence"]["selection"]["threshold"]
    identity = artifact(args, manifest, digest(raw))
    identity["evidence_head_manifest_sha256"] = models.manifest["clinical_heads"]["evidence"][
        "manifest_sha256"
    ]
    assert (
        identity["evidence_head_manifest_sha256"]
        == "4c275cc8ba60ac9af3671de4acb65bd79b9168da699d8f71e05ce6907f3719b1"
    )
    predictions = {}
    output = output / "clinical"
    output.mkdir(parents=True, exist_ok=True)
    for doc in inputs["documents"]:
        check_sources(identity)
        path = output / ("paper-" + doc["pmid"] + ".json")
        if path.exists():
            pred = json.loads(path.read_bytes())
            assert pred["identity"] == identity and pred["source_hash"] == doc["source_hash"]
        else:
            assert digest(doc["text"]) == doc["source_hash"]
            automatic, trace = comparisons(doc, role_models, manifest, head, threshold, encoder)
            pred = {
                "pmid": doc["pmid"],
                "source_hash": doc["source_hash"],
                "identity": identity,
                "automatic_comparisons": automatic,
                "trace": trace,
            }
            save(path, pred)
        predictions[doc["pmid"]] = pred
        print(
            dumps(
                {
                    "stage": "clinical_predict",
                    "papers": len(predictions),
                    "comparisons": len(pred["automatic_comparisons"]),
                    "total": len(inputs["documents"]),
                }
            ),
            flush=True,
        )
    # Exact original evaluator; descriptions are never supplied from dev labels.
    raw_gold = Path(spec["gold_path"]).read_bytes()
    assert digest(raw_gold) == spec["gold_sha256"]
    gold = json.loads(raw_gold)
    assert {d["pmid"] for d in gold["documents"]} == set(predictions)
    baseline = json.loads(args.baseline_clinical.read_bytes())
    assert baseline["inputs_sha256"] == digest(raw) and baseline["gold_sha256"] == digest(raw_gold)
    rows = []
    for doc in gold["documents"]:
        pred = predictions[doc["pmid"]]
        assert pred["source_hash"] == digest(doc["text"])
        row = evidence_counts(doc, pred["automatic_comparisons"], [])
        rows.append(
            {
                "pmid": doc["pmid"],
                "automatic_ico_evidence_tokens": row["automatic_ico_evidence_tokens"],
                "ico_alignment": row["ico_alignment"],
            }
        )
    original = {d["pmid"]: d for d in baseline["per_paper"]}
    candidate = {d["pmid"]: d for d in rows}
    assert set(original) == set(candidate)
    key = "automatic_ico_evidence_tokens"
    assert all(
        original[p][key]["tp"] + original[p][key]["fn"] == candidate[p][key]["tp"] + candidate[p][key]["fn"]
        for p in original
    )
    metrics = {
        "baseline": scoring._aggregate(list(original.values()), key),
        "refinement": scoring._aggregate(rows, key),
    }
    alignment = Counter()
    for row in rows:
        alignment.update(row["ico_alignment"])
    report = {
        "id": uid(),
        "at": now(),
        "schema": "v6-r1-clinical-development-1",
        "identity": identity,
        "papers": len(rows),
        "metrics": metrics,
        "per_paper": rows,
        "alignment": dict(alignment),
        "paired": scoring.paired_bootstrap(sorted(original), original, candidate, key, 2000, 20261010),
        "input_sha256": digest(raw),
        "gold_sha256": digest(raw_gold),
        "baseline_report_sha256": baseline["original_report_sha256"],
        "mapping": "unchanged strict three-description identity",
        "full_engine_evaluation": False,
        "sealed_test_opened": False,
        "default_engine": "heuristic-v1",
        "nutrition_accuracy": "unmeasured",
    }
    save(output / "report.json", report)
    scoring._receipt(settings, "v6_r1_clinical_development", report["id"], report)
    print(
        dumps({"report": str(output / "report.json"), "metrics": metrics, "alignment": dict(alignment)}),
        flush=True,
    )


def main():
    from nutri_literature.operations_v6 import processing_slot
    from nutri_literature.resources_v6 import memory_budget
    from nutri_literature.settings import Settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--task", choices=("relations", "clinical"), required=True)
    parser.add_argument("--mode", choices=("end_to_end", "gold_entities"), default="end_to_end")
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--refinement", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--baseline-clinical", type=Path)
    args = parser.parse_args()
    os.environ["LITERATURE_V6_MODEL_HOME"] = str(args.models)
    settings = Settings(ROOT / "runtime", ROOT / "config/topics.json")
    with memory_budget(), processing_slot(settings):
        started = time.monotonic()
        function = relations if args.task == "relations" else clinical
        function(args, settings, args.output)
        print(json.dumps({"elapsed_seconds": time.monotonic() - started}), flush=True)


if __name__ == "__main__":
    for variable in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[variable] = "1"
    if os.name == "posix":
        os.nice(10)
        locks = Path("/home/keunsoo/projects/nutriomics-literature/runtime/locks")
        if not locks.exists():
            locks = ROOT / "runtime/locks"
        os.environ["LITERATURE_PROCESSING_LOCK_HOME"] = str(locks)
        os.environ["LITERATURE_NLP_LOCK"] = str(locks / "nlp-cpu.lock")
    main()
