"""Independent rescoring, source alignment, artifact and cohort verification."""
from collections import Counter
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def main():
    from nutri_literature import benchmarks as scoring, clinical_evaluation_v6, evaluation_v6
    from nutri_literature.db import digest, dumps, now
    from nutri_literature.settings import Settings
    from nutri_literature.extraction_v6 import source_manifest
    from evaluate_v6_refinement import relation_metrics
    settings = Settings(ROOT / "runtime", ROOT / "config/topics.json")
    index = json.loads((settings.home / "public_benchmarks/v2-measurement-index.json").read_bytes())
    prepared = index["steps"]["prepare:development:bioredirect:abstract"]
    relation_source = {d["pmid"]: d for d in json.loads(Path(prepared["inputs_path"]).read_bytes())["documents"]}
    relation_gold = {d["pmid"]: d for d in json.loads(Path(prepared["gold_path"]).read_bytes())["documents"]}
    spec = json.loads((ROOT / "runtime/v6/training-data/recovery/inference-official-booleans/development-confirmed/spec.json").read_bytes())
    clinical_source = {d["pmid"]: d for d in json.loads(Path(spec["inputs_path"]).read_bytes())["documents"]}
    clinical_gold = {d["pmid"]: d for d in json.loads(Path(spec["gold_path"]).read_bytes())["documents"]}
    reports, span_counts, conditions = {}, Counter(), Counter()
    sources = {**relation_source, **clinical_source}
    identity_run = "runtime/v6/refinement-r5" if (ROOT / "runtime/v6/refinement-r5/relations-end_to_end/report.json").exists() else "runtime/v6/refinement-r5-fast"

    def inspect(value, text):
        if isinstance(value, dict):
            if "start" in value and "end" in value:
                surface = value.get("quote", value.get("text"))
                if isinstance(surface, str):
                    assert 0 <= value["start"] < value["end"] <= len(text)
                    assert text[value["start"]:value["end"]] == surface
                    span_counts["source_spans"] += 1
            for nested in value.values():
                inspect(nested, text)
        elif isinstance(value, list):
            for nested in value:
                inspect(nested, text)

    definitions = [
        ("runtime/v6/refinement-r4/relations-end_to_end", "v6_r4_automatic_relation_development", "relation"),
        ("runtime/v6/refinement-r4/relations-gold_entities", "v6_r4_automatic_relation_development", "relation"),
        (identity_run + "/relations-end_to_end", "v6_r4_automatic_relation_development", "relation"),
        ("runtime/v6/refinement-r4/clinical-development-b", "v6_r4_trial_linking_development", "clinical"),
        ("runtime/v6/refinement-r5/clinical-development", "v6_r5_trial_recovery_development", "clinical"),
        ("runtime/v6/refinement-r6/clinical-development", "v6_r6_trial_active_development", "clinical"),
        ("runtime/v6/refinement-r7/clinical-development", "v6_r7_trial_composition_development", "clinical"),
    ]
    for relative, kind, task in definitions:
        directory = ROOT / relative
        report_raw = (directory / "report.json").read_bytes()
        report = json.loads(report_raw)
        assert scoring.verify_receipt(settings, kind, report)
        predictions = {}
        for path in directory.glob("paper-*.json"):
            pred = json.loads(path.read_bytes())
            assert pred["pmid"] not in predictions
            assert pred["source_hash"] == digest(sources[pred["pmid"]]["text"])
            assert pred["identity"] == report["identity"]
            inspect(pred, sources[pred["pmid"]]["text"])
            predictions[pred["pmid"]] = pred
        assert len(predictions) == report["papers"]
        if task == "relation":
            assert report["original_v6_baseline_counts_reproduced"]
            assert report["identity"]["frozen_source"] == source_manifest()[1]
            rows = {key: [] for key in report["metrics"]}
            for pmid, pred in predictions.items():
                for stage in rows:
                    output = pred if stage == "automatic_noise" else pred[stage] if stage in {"identity_recovery_only", "recall_ensemble"} else pred["baseline_outputs"][stage]
                    rows[stage].append({"pmid": pmid, **relation_metrics(relation_gold[pmid], output["relations"], output["verified_relations"])})
            for stage, metrics in report["metrics"].items():
                for name, metric in metrics.items():
                    assert scoring._aggregate(rows[stage], name) == metric
        else:
            key = "automatic_ico_evidence_tokens"
            rows = []
            for pmid, pred in predictions.items():
                counts = clinical_evaluation_v6.evidence_counts(clinical_gold[pmid], pred["automatic_comparisons"], [])
                rows.append({"pmid": pmid, key: counts[key]})
                for observation in pred["trace"]["observations"]:
                    binding = observation["conditions"]
                    conditions[relative + ":observations"] += 1
                    conditions[relative + ":with_dose"] += bool(binding["intervention_doses"] or binding["comparator_doses"])
                    conditions[relative + ":with_timepoint"] += binding["timepoint"] is not None
                    conditions[relative + ":ambiguous_timepoint"] += bool(binding["hold_reasons"])
            assert scoring._aggregate(rows, key) == report["metrics"]["trial_linking"]
        reports[relative] = {"report_sha256": digest(report_raw), "receipt_verified": True, "rescored": True,
                             "papers": len(predictions), "metrics": report["metrics"]}
    model_checks = {}
    protected = set(relation_source) | set(clinical_source)
    for relative in ("runtime/v6/refinement-r4/relation-head", identity_run + "/relation-head",
                     identity_run + "/identity-head", "runtime/v6/refinement-r5/clinical-roles-final"):
        directory = ROOT / relative
        manifest = json.loads((directory / "manifest.json").read_bytes())
        assert manifest["training_split"] == "train" and manifest["test_used_for_selection"] is False
        assert not protected & set(manifest["training_pmids"])
        for filename, sha in manifest["files"].items():
            assert Path(filename).name == filename and digest((directory / filename).read_bytes()) == sha
        module = "automatic_models_v6.py" if "relation-head" in relative else "automatic_identity_v6.py" if "identity-head" in relative else "trial_roles_v6.py"
        assert manifest["source_sha256"] == digest((ROOT / "nutri_literature" / module).read_bytes())
        model_checks[relative] = {"manifest_sha256": digest((directory / "manifest.json").read_bytes()),
                                  "weights_verified": True, "development_pmid_overlap": 0}
    feature_checks = {}
    for relative in ("runtime/v6/refinement-r4/automatic-features", identity_run + "/automatic-features-oof"):
        directory = ROOT / relative
        manifest = json.loads((directory / "manifest.json").read_bytes())
        assert manifest["training_split"] == "train"
        for file in manifest["files"]:
            assert Path(file["path"]).name == file["path"]
            assert digest((directory / file["path"]).read_bytes()) == file["sha256"]
        feature_checks[relative] = {"manifest_sha256": digest((directory / "manifest.json").read_bytes()),
                                    "files_verified": len(manifest["files"])}
    structured = ROOT / "runtime/v6/refinement-r8/structured-trial-records"
    structured_manifest = json.loads((structured / "manifest.json").read_bytes())
    for f in structured_manifest["files"]:
        raw = (structured / f["path"]).read_bytes()
        assert digest(raw) == f["sha256"]
        pred = json.loads(raw)
        assert pred["identity"] == structured_manifest["identity"]
        assert pred["source_hash"] == digest(clinical_source[pred["pmid"]]["text"])
        ids = [a["arm_instance_id"] for a in pred["arm_registry"]]
        assert len(ids) == len(set(ids))
        for link in pred["result_links"]:
            assert set(link["arm_instance_ids"]) <= set(link["arm_candidates"]) <= set(ids)
            if not link["arm_instance_ids"]:
                assert "multiple_regimens_or_unknown_conditions" in link["hold_reasons"]
        inspect(pred, clinical_source[pred["pmid"]]["text"])
    result = {"at": now(), "passed": True, "reports": reports, "models": model_checks,
              "training_features": feature_checks,
              "identity_run": identity_run, "structured_trial_records": structured_manifest["counts"],
              "source_alignment": dict(span_counts), "condition_counts": dict(conditions),
              "source_sha256": {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in (ROOT / "nutri_literature").glob("*v6.py")},
              "sealed_test_opened": False, "default_engine": "heuristic-v1",
              "condition_accuracy": "unmeasured", "nutrition_accuracy": "unmeasured"}
    result["oof_scope"] = "New heads are paper-held-out; legacy upstream models fixed, ID and relation CV not fully nested. Development is exposed; not independent final performance."
    output = ROOT / "runtime/v6/refinement-r5/final-verification.json"
    scoring._write_immutable(output, result)
    print(dumps({"path": str(output), "passed": True, "spans": dict(span_counts), "conditions": dict(conditions)}))


if __name__ == "__main__":
    main()
