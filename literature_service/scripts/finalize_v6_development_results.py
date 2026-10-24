"""Consolidate preserved, authenticated development results without model tuning."""

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATION = ROOT / "runtime/v6/validation"


def read(path):
    return json.loads(path.read_bytes())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    core_path = VALIDATION / "v5-v6-four-core-completed.json"
    clinical_path = VALIDATION / "clinical-component-development-report.json"
    scope_path = VALIDATION / "scope-component-comparison.json"
    receipt_path = VALIDATION / "clinical-evidence-recovery-receipt.json"
    check_path = VALIDATION / "clinical-saved-score-verification.json"
    model_path = ROOT / "runtime/v6/recovered-models/clinical-v6-evidence/manifest.json"
    core, clinical, scope, receipt, check, model = map(
        read, (core_path, clinical_path, scope_path, receipt_path, check_path, model_path)
    )
    assert sha(core_path) == "60b4fa8ab75d29b87ca3648cfe997d0a74cfb3266cc54d82b99c1e5155fef558"
    assert receipt["database_receipt_verified"]
    assert sha(clinical_path) == receipt["report_sha256"] == check["report_sha256"]
    assert sha(model_path) == receipt["head_manifest_sha256"]
    for name, expected in model["files"].items():
        assert sha(model_path.parent / name) == expected
    assert check["raw_saved_scores_reproduced"] and check["source_offsets_validated"]
    assert check["source_only_predictions_precede_provided"]
    archive_path = VALIDATION / "clinical-evidence-development-predictions-20261015.tar.gz"
    assert sha(archive_path) == receipt["archive_sha256"]
    assert len(core["comparisons"]) == 4
    assert len({tuple(c["configuration"]) for c in core["comparisons"]}) == 4
    assert {c["v6_artifact"]["artifact_identity"] for c in core["comparisons"]} == {
        "b086db14e7b72bf254dc83aed1791b8c3941d47b99c681b3a072b5023840f7ed"
    }
    assert clinical["papers"] == len(clinical["per_paper"]) == 92
    alignment = Counter()
    for paper in clinical["per_paper"]:
        alignment.update(paper["ico_alignment"])
    assert alignment["gold_prompts"] == 234
    selection = {k: v for k, v in model["selection"].items() if k != "grid"}
    summary = {
        "schema": "v6-development-continuation-results-1",
        "at_kst": datetime.now(timezone(timedelta(hours=9))).isoformat(),
        "status": "requested_training_and_development_measurements_completed_no_promotion",
        "default_engine": "heuristic-v1",
        "sealed_test_opened": False,
        "seven_day_operation_passed": False,
        "nutrition_accuracy": "unmeasured",
        "full_engine_all_gates_passed": False,
        "scientific_source": check["scientific_source"],
        "core_comparisons": [
            {
                **{
                    k: c[k]
                    for k in (
                        "configuration",
                        "papers",
                        "baseline_id",
                        "candidate_id",
                        "gold_sha256",
                        "input_sha256",
                        "metrics",
                    )
                },
                "v5_artifact_identity": c["v5_artifact"]["artifact_identity"],
                "v6_artifact_identity": c["v6_artifact"]["artifact_identity"],
            }
            for c in core["comparisons"]
        ],
        "core_comparison_missing_full_engine_reports": core["missing"],
        "scope_component": scope,
        "clinical_component": {k: v for k, v in clinical.items() if k not in ("artifact", "per_paper")},
        "clinical_artifact_identity": clinical["artifact"]["artifact_identity"],
        "clinical_alignment": dict(alignment),
        "clinical_v5_comparable_baseline_available": False,
        "clinical_head": {
            "created_at": model["created_at"],
            "training_papers_with_rows": len(model["training_pmids"]),
            "selection_train_oof_only": selection,
            "files": model["files"],
            "manifest_sha256": sha(model_path),
        },
        "verification": {"recovery": receipt, "saved_score_check": check},
        "sources": {
            str(p.relative_to(ROOT)).replace("\\", "/"): sha(p)
            for p in (core_path, clinical_path, scope_path, receipt_path, check_path, model_path)
        },
        "limitations": [
            "Four full-engine core reports use the outcome-head artifact, without the ICO head.",
            "Clinical ICO results use a separate complete-head observation DAG artifact; not a full-engine gate.",
            "Automatic ICO scoring requires strict three-description identity; 0/234 prompts matched.",
            "Scope evaluation reuses the unchanged v5 module; not a completed full-engine BioScope report.",
            "Development data, not sealed final performance; no nutrition accuracy claim.",
        ],
    }
    out = VALIDATION / "v6-development-results-20261015.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state_path = VALIDATION / "continuation-task-state.json"
    state = read(state_path)
    state.update(
        {
            "at": summary["at_kst"],
            "status": summary["status"],
            "primary_development_pid": None,
            "secondary_pipeline_pid": None,
            "secondary_evidence_training_pid": None,
            "secondary_clinical_controller_pid": None,
            "third_evidence_fit_pid": None,
            "third_clinical_controller_pid": None,
            "secondary_offline": False,
            "primary_current_variant": "completed outcome-head artifact; preserved without ICO head",
            "new_evidence_audit": "runtime/v6/training-data/recovery/inference-official-booleans/train-audited/audit.json",
            "clinical_evidence_complete": True,
            "clinical_evidence_manifest_sha256": sha(model_path),
            "clinical_evidence_weights_recovered_and_verified": True,
            "clinical_evidence_backup_locations": [
                "local runtime/v6/recovered-models/clinical-v6-evidence",
                "primary runtime/v6/recovered-clinical-evidence-4c275cc8",
                "secondary runtime/v6/models-evidence/clinical-v6-evidence",
            ],
            "clinical_component_development_complete": True,
            "clinical_component_full_engine": False,
            "primary_four_core_comparisons_complete": True,
            "primary_linux_full_passed": 533,
            "third_recovery_fit_status": "superseded_by_completed_original_model; stopped, checkpoints preserved",
            "next": "Requested training and development measurements complete. No promotion: relation/ICO/scope goals fail. Sealed final and 7-day gates remain unopened/uncompleted.",
            "report": "docs/v6-development-results-20261015.md",
            "results": str(out.relative_to(ROOT)).replace("\\", "/"),
        }
    )
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "results": str(out),
                "core_reports": 4,
                "clinical_papers": 92,
                "head_validated": True,
                "promoted": False,
            }
        )
    )


if __name__ == "__main__":
    main()
