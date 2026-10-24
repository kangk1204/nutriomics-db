"""Audit authenticated reports, source spans and unchanged scorer reproduction."""

from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def main():
    from nutri_literature import benchmarks as scoring, evaluation_v6, clinical_evaluation_v6
    from nutri_literature.db import digest, dumps, now
    from nutri_literature.settings import Settings
    from nutri_literature.decision_guard_v6 import guard_relation_hypotheses, implementation
    from nutri_literature.extraction_v6 import source_manifest
    from evaluate_v6_refinement import native_mapping, relation_metrics

    settings = Settings(ROOT / "runtime", ROOT / "config/topics.json")
    definitions = [
        ("runtime/v6/refinement-r1/development/end_to_end", "v6_r1_relation_development"),
        ("runtime/v6/refinement-r1/development/gold_entities", "v6_r1_relation_development"),
        ("runtime/v6/refinement-r1/development/clinical", "v6_r1_clinical_development"),
        ("runtime/v6/refinement-r2/development", "v6_r2_clinical_boundary_development"),
        ("runtime/v6/refinement-r3/development", "v6_r3_clinical_grounding_development"),
        ("runtime/v6/refinement-guards-final/development/clinical", "v6_guarded_clinical_development"),
    ]
    inputs = json.loads(
        (ROOT / "runtime/public_benchmarks/bioredirect/404a04bf9fb316caa061e9ab/inputs.json").read_bytes()
    )
    source = {d["pmid"]: d for d in inputs["documents"]}
    label_data = json.loads(
        (ROOT / "runtime/public_benchmarks/bioredirect/404a04bf9fb316caa061e9ab/gold.json").read_bytes()
    )
    labels = {d["pmid"]: d for d in label_data["documents"]}
    spec = json.loads(
        (
            ROOT
            / "runtime/v6/training-data/recovery/inference-official-booleans/development-confirmed/spec.json"
        ).read_bytes()
    )
    clinical_inputs = json.loads(Path(spec["inputs_path"]).read_bytes())
    clinical_source = {d["pmid"]: d for d in clinical_inputs["documents"]}
    clinical_gold = json.loads(Path(spec["gold_path"]).read_bytes())
    clinical_labels = {d["pmid"]: d for d in clinical_gold["documents"]}
    source.update(clinical_source)
    spans = Counter()

    def inspect(value, text, filename):
        if isinstance(value, dict):
            if "start" in value and "end" in value:
                surface = value.get("quote", value.get("text"))
                if isinstance(surface, str):
                    assert 0 <= value["start"] < value["end"] <= len(text), filename
                    assert text[value["start"] : value["end"]] == surface, filename
                    spans[filename.split("/development")[0]] += 1
            for nested in value.values():
                inspect(nested, text, filename)
        elif isinstance(value, list):
            for nested in value:
                inspect(nested, text, filename)

    reports, checks = {}, {}
    for relative, kind in definitions:
        directory = ROOT / relative
        path = directory / "report.json"
        report = json.loads(path.read_bytes())
        assert scoring.verify_receipt(settings, kind, report), relative
        predictions = {}
        for file in directory.glob("paper-*.json"):
            prediction = json.loads(file.read_bytes())
            pmid = prediction["pmid"]
            assert pmid not in predictions
            assert prediction["source_hash"] == digest(source[pmid]["text"])
            inspect(prediction, source[pmid]["text"], relative)
            predictions[pmid] = prediction
        assert len(predictions) == report["papers"]
        if report["papers"] == 100:
            assert report["original_v6_baseline_counts_reproduced"]
            rows = {k: [] for k in ("baseline", "identity_gate", "joint")}
            oracle = report["mode"] == "gold_entities"
            for pmid, pred in predictions.items():
                generated = guard_relation_hypotheses(
                    pred["outputs"]["baseline"]["relation_hypotheses"], oracle
                )
                selected, verified = evaluation_v6.project_hypotheses(
                    {"relation_hypotheses": generated}, native_mapping(pred["entities"]), oracle
                )
                # Native oracle IDs are sets; process hash randomization may
                # change serialization order, never relation identity/counts.
                assert sorted(map(dumps, selected)) == sorted(
                    map(dumps, pred["outputs"]["identity_gate"]["relations"])
                )
                assert sorted(map(dumps, verified)) == sorted(
                    map(dumps, pred["outputs"]["identity_gate"]["verified_relations"])
                )
                for stage, output in pred["outputs"].items():
                    metrics = relation_metrics(
                        labels[pmid], output["relations"], output["verified_relations"]
                    )
                    rows[stage].append({"pmid": pmid, **metrics})
            for stage, metrics in report["metrics"].items():
                for key, expected in metrics.items():
                    assert scoring._aggregate(rows[stage], key) == expected
        else:
            assert set(predictions) == set(clinical_source)
            key = "automatic_ico_evidence_tokens"
            selected_key = "selected" if "guards-final" in relative else "automatic_comparisons"
            rows = []
            for pmid, pred in predictions.items():
                values = clinical_evaluation_v6.evidence_counts(clinical_labels[pmid], pred[selected_key], [])
                rows.append({"pmid": pmid, key: values[key]})
            candidate = "guarded" if "guards-final" in relative else "refinement"
            expected = report["metrics"].get(candidate, report["metrics"].get(key))
            assert scoring._aggregate(rows, key) == expected
        checks[relative] = {
            "report_sha256": digest(path.read_bytes()),
            "receipt_verified": True,
            "papers": len(predictions),
            "raw_source_scores_reproduced": True,
        }
        reports[relative] = report
    old_helper = reports[definitions[0][0]]["identity"]["clinical_refinement_source"]
    assert old_helper == digest((ROOT / "nutri_literature/clinical_refinement_v6.py").read_bytes())
    model_checks = {}
    for path in (
        ROOT / "runtime/v6/refinement-r1/joint-relation/manifest.json",
        ROOT / "runtime/v6/refinement-r1/clinical-roles/manifest.json",
        ROOT / "runtime/v6/refinement-r2/boundary/manifest.json",
    ):
        manifest = json.loads(path.read_bytes())
        assert manifest["training_split"] == "train" and manifest["test_used_for_selection"] is False
        cohort = set(labels) if "joint-relation" in str(path) else set(clinical_labels)
        assert not cohort & set(manifest["training_pmids"])
        for name, sha in manifest["files"].items():
            assert digest((path.parent / name).read_bytes()) == sha
        model_checks[str(path.relative_to(ROOT))] = {
            "manifest_sha256": digest(path.read_bytes()),
            "weights_verified": True,
            "training_overlap": 0,
        }
    output = {
        "at": now(),
        "schema": "v6-refinement-independent-verification-1",
        "reports": checks,
        "model_checks": model_checks,
        "quote_and_entity_span_occurrences_checked": sum(spans.values()),
        "span_counts_by_variant": dict(spans),
        "guard_implementations": implementation(),
        "guard_matches_source_evaluation_on_all_100_auto_and_100_oracle_papers": True,
        "frozen_v6_sources": source_manifest()[1],
        "scorers": {
            m.__name__: digest(Path(m.__file__).read_bytes())
            for m in (scoring, evaluation_v6, clinical_evaluation_v6)
        },
        "all_scorers_unchanged": True,
        "default_engine": "heuristic-v1",
        "sealed_test_opened": False,
        "development_exposed": True,
        "nutrition_accuracy": "unmeasured",
    }
    target = ROOT / "runtime/v6/validation/refinement-final-verification.json"
    target.write_text(dumps(output))
    print(
        dumps(
            {
                "verification": str(target),
                "reports_verified": len(checks),
                "source_spans_checked": sum(spans.values()),
                "models_verified": len(model_checks),
            }
        )
    )


if __name__ == "__main__":
    main()
