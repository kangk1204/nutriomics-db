"""Same frozen relation metric/cohort; new train-only automatic-noise head."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def main():
    from nutri_literature import benchmarks as scoring, evaluation_v6
    from nutri_literature.automatic_models_v6 import load, source_sha as model_source
    from nutri_literature.automatic_features_v6 import bags_for, source_sha as feature_source
    from nutri_literature.operations_v6 import components, processing_slot
    from nutri_literature.resources_v6 import memory_budget
    from nutri_literature.settings import Settings
    from nutri_literature.extraction_v6 import source_manifest, native_event
    from nutri_literature.scope_v5 import predict as predict_scope
    from nutri_literature.text import sentence_spans
    from nutri_literature.db import digest, dumps, uid, now
    from evaluate_v6_refinement import native_mapping, relation_metrics

    p = argparse.ArgumentParser()
    for key in ("models", "head", "baseline-predictions", "output"):
        p.add_argument("--" + key, type=Path, required=True)
    p.add_argument("--mode", choices=("end_to_end", "gold_entities"), required=True)
    p.add_argument("--normalizer", type=Path)
    args = p.parse_args()
    os.environ["LITERATURE_V6_MODEL_HOME"] = str(args.models.resolve())
    settings = Settings(ROOT / "runtime", ROOT / "config/topics.json")
    with memory_budget() as resources, processing_slot(settings):
        head, manifest = load(args.head)
        index = json.loads((settings.home / "public_benchmarks/v2-measurement-index.json").read_bytes())
        prepared = index["steps"]["prepare:development:bioredirect:abstract"]
        inputs, _, input_sha = scoring._load_inputs(settings, prepared["inputs_path"])
        docs = sorted(inputs["documents"], key=lambda d: digest("v3-development-20261003:" + d["pmid"]))[:100]
        assert not set(manifest["training_pmids"]) & {d["pmid"] for d in docs}
        values = components(settings, "union")
        identity_head, identity_manifest = None, None
        if args.normalizer:
            from nutri_literature.automatic_identity_v6 import load as load_identity
            identity_head, identity_manifest = load_identity(args.normalizer)
            assert not set(identity_manifest["training_pmids"]) & {d["pmid"] for d in docs}
            assert identity_manifest["gene_dictionary_sha256"] == values["_gene_index"].manifest["records_sha256"]
        identity = {"feature_source": feature_source(), "model_source": model_source(),
                    "frozen_source": source_manifest()[1], "head_manifest": digest((args.head / "manifest.json").read_bytes()),
                    "runner_sha256": digest(Path(__file__).read_bytes()), "input_sha256": input_sha,
                    "mode": args.mode, "variant": "automatic-noise-v6-r4"}
        identity["absolute_identity_manifest"] = digest((args.normalizer / "manifest.json").read_bytes()) if args.normalizer else None
        args.output.mkdir(parents=True, exist_ok=True)
        predictions = {}
        for i, doc in enumerate(docs):
            assert model_source() == identity["model_source"] and feature_source() == identity["feature_source"]
            assert digest(Path(__file__).read_bytes()) == identity["runner_sha256"]
            path = args.output / ("paper-" + doc["pmid"] + ".json")
            if path.exists():
                pred = json.loads(path.read_bytes())
                assert pred["identity"] == identity and pred["source_hash"] == doc["source_hash"]
            else:
                raw = (args.baseline_predictions / path.name).read_bytes()
                original = json.loads(raw)
                assert original["source_hash"] == doc["source_hash"]
                assert original["identity"]["frozen_v6_sources"] == identity["frozen_source"]
                mentions = original["entities"]
                sentences = values["_clinical_models"].predict(sentence_spans(doc["text"], doc["sections"]))
                identity_trace = []
                if identity_head is not None and args.mode == "end_to_end":
                    from nutri_literature.automatic_identity_v6 import apply as apply_identity
                    mentions, identity_trace = apply_identity(mentions, doc["text"], sentences, values["_id_search"], identity_head, identity_manifest)
                bags = bags_for(doc, mentions, sentences, values)
                scores = head.predict([b["features"] for b in bags])
                old_scores = values["_v6_models"].predict([b["features"] for b in bags])
                scopes = predict_scope(sentences, values["_learned_models"])
                hypotheses = []
                for bag, row, previous in zip(bags, scores, old_scores):
                    direction = previous["direction"]
                    role = max(direction, key=direction.get) if direction else "unknown"
                    if direction.get(role, 0) < .7:
                        role = "unknown"
                    for label, score in row.items():
                        pair = bag["pairs"][0]
                        event, reasons = None, ["classification_threshold"]
                        proposed = score >= manifest["selection"]["threshold"]
                        if proposed:
                            checked = [(p, *native_event(p, label, role, scopes, values, mentions)) for p in bag["pairs"]]
                            pair, event, reasons = min(checked, key=lambda x: (x[1] is None, len(x[2]), x[0]["sentence"]["start"]))
                        resolved = args.mode == "gold_entities" or all(pair[k].get("normalized_id") for k in ("chemical", "gene"))
                        hypotheses.append({"chemical": pair["chemical"], "gene": pair["gene"],
                            "benchmark_relation_type": label, "role_direction": role,
                            "type_selected": proposed and resolved, "review_proposed": proposed,
                            "semantic_verified": event is not None and not reasons,
                            "priority_score": score, "reasons": reasons,
                            "automatic_gate_reasons": [] if resolved else ["endpoint_identity_unresolved"],
                            "evidence": {"start": pair["sentence"]["start"], "end": pair["sentence"]["end"], "quote": pair["sentence"]["text"]}})
                rels, verified = evaluation_v6.project_hypotheses({"relation_hypotheses": hypotheses}, native_mapping(mentions), args.mode == "gold_entities")
                repaired_hypotheses = deepcopy(original["outputs"]["baseline"]["relation_hypotheses"])
                by_span = {(m["kind"], m["start"], m["end"]): m for m in mentions}
                for h in repaired_hypotheses:
                    changed = False
                    for role_name in ("chemical", "gene"):
                        old_entity = h[role_name]
                        replacement = by_span[(old_entity["kind"], old_entity["start"], old_entity["end"])]
                        changed |= old_entity.get("normalized_id") != replacement.get("normalized_id")
                        h[role_name] = replacement
                    h["type_selected"] &= args.mode == "gold_entities" or all(h[k].get("normalized_id") for k in ("chemical", "gene"))
                    # This ablation changes only identity; it does not claim a
                    # freshly verified semantic event for a repaired endpoint.
                    if changed:
                        h["semantic_verified"] = False
                repaired_relations, repaired_verified = evaluation_v6.project_hypotheses(
                    {"relation_hypotheses": repaired_hypotheses}, native_mapping(mentions), args.mode == "gold_entities")
                pred = {"pmid": doc["pmid"], "source_hash": doc["source_hash"], "identity": identity,
                        "baseline_prediction_sha256": digest(raw), "entities": mentions,
                        "identity_trace": identity_trace,
                        "identity_recovery_only": {"relations": repaired_relations, "verified_relations": repaired_verified,
                                                   "relation_hypotheses": repaired_hypotheses},
                        "recall_ensemble": {
                            "relations": [{**r, "candidate_source": "identity_recovered_original_model"} for r in repaired_relations]
                                         + [{**r, "candidate_source": "automatic_noise_trained_model"} for r in rels],
                            "verified_relations": repaired_verified + verified,
                            "policy": "fixed union; no extra threshold, no development selection; all false positives scored"},
                        "relation_hypotheses": hypotheses, "relations": rels, "verified_relations": verified,
                        "baseline_outputs": original["outputs"],
                        "candidate_pairs": original["candidate_pairs"]}
                scoring._write_immutable(path, pred)
            predictions[doc["pmid"]] = pred
            print(dumps({"stage": "automatic_relation_predict", "papers": i + 1, "total": len(docs), "mode": args.mode}), flush=True)
        # Gold opened only after every source prediction has been saved.
        gold, _, gold_sha = scoring._load_gold(settings, prepared["gold_path"])
        by_pmid = {d["pmid"]: d for d in gold["documents"]}
        stages = {k: [] for k in ("baseline", "identity_gate", "joint", "identity_recovery_only", "automatic_noise", "recall_ensemble")}
        for doc in docs:
            pred, target = predictions[doc["pmid"]], by_pmid[doc["pmid"]]
            for key in stages:
                value = pred if key == "automatic_noise" else pred[key] if key in {"identity_recovery_only", "recall_ensemble"} else pred["baseline_outputs"][key]
                stages[key].append({"pmid": doc["pmid"], **relation_metrics(target, value["relations"], value["verified_relations"])})
        metrics = {key: {m: scoring._aggregate(rows, m) for m in ("relation", "relation_direction", "verified_relation")} for key, rows in stages.items()}
        expected = {"end_to_end": (11, 256, 143), "gold_entities": (46, 138, 108)}[args.mode]
        assert tuple(metrics["baseline"]["relation"][k] for k in ("tp", "fp", "fn")) == expected
        id_rows = []
        for doc in docs:
            pred = predictions[doc["pmid"]]
            id_rows.append({"pmid": doc["pmid"], "strict_gene_id": evaluation_v6.strict_gene_counts(
                by_pmid[doc["pmid"]], pred, native_mapping(pred["entities"]), args.mode == "gold_entities")})
        base = {r["pmid"]: r for r in stages["identity_gate"]}
        new = {r["pmid"]: r for r in stages["automatic_noise"]}
        report = {"id": uid(), "at": now(), "identity": identity, "papers": len(docs),
                  "schema": "automatic-noise-v6-r4-development-1", "mode": args.mode,
                  "input_sha256": input_sha, "gold_sha256": gold_sha, "metrics": metrics, "per_paper": stages,
                  "paired_vs_identity_gate": scoring.paired_bootstrap(sorted(base), base, new, "relation", 2000, 20261010),
                  "paired_identity_recovery_only": scoring.paired_bootstrap(sorted(base), base,
                       {r["pmid"]: r for r in stages["identity_recovery_only"]}, "relation", 2000, 20261010),
                  "paired_recall_ensemble": scoring.paired_bootstrap(sorted(base), base,
                       {r["pmid"]: r for r in stages["recall_ensemble"]}, "relation", 2000, 20261010),
                  "strict_gene_id": scoring._aggregate(id_rows, "strict_gene_id"), "strict_gene_id_per_paper": id_rows,
                  "restored_gene_mentions": sum(bool(r["selected"]) for p in predictions.values() for r in p["identity_trace"]),
                  "original_v6_baseline_counts_reproduced": True,
                  "resources": resources, "sealed_test_opened": False, "default_engine": "heuristic-v1",
                  "full_engine_evaluation": False, "nutrition_accuracy": "unmeasured"}
        scoring._write_immutable(args.output / "report.json", report)
        scoring._receipt(settings, "v6_r4_automatic_relation_development", report["id"], report)
        print(dumps({"report": str(args.output / "report.json"), "metrics": metrics}), flush=True)


if __name__ == "__main__":
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = "1"
    if os.name == "posix":
        os.nice(10)
    main()
