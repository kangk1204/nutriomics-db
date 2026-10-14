"""Machine-readable readiness for the still undefined personal diet-impact task."""

from __future__ import annotations


def personal_impact_status() -> dict:
    """Expose what an owner must supply without accepting health records or predicting."""
    return {
        "schema_version": "personal-diet-impact-readiness-v1",
        "status": "blocked_task_definition_and_participant_data",
        "estimator_available": False,
        "health_effect_estimate": None,
        "clinical_validation": "not_performed",
        "required_owner_specification": {
            "inputs": "Defined dietary dose and timing, omics assays, health and covariate fields",
            "endpoint": "Named measured health outcome, unit, and estimand",
            "time_horizon": "Explicit prediction or intervention follow-up interval",
            "target_population": "Eligibility, intended population, and application setting",
            "claim_type": "Association/prediction or an intervention-supported causal effect",
        },
        "required_evidence": [
            {
                "gate": "participant_linked_records",
                "status": "not_available_for_this_task",
                "artifact": "Original individual diet-dose/time, omics, outcome, and covariate records",
                "required_checks": "Verified participant crosswalk and repeated-measure time alignment",
            },
            {
                "gate": "research_permissions",
                "status": "not_provided_for_this_task",
                "artifact": "Applicable consent/IRB, data-use approval, and source reuse permissions",
            },
            {
                "gate": "frozen_estimator_and_validation",
                "status": "not_implemented",
                "artifact": "Prespecified estimator and participant-separated independent validation",
                "required_checks": "Calibration, uncertainty, missingness, leakage, and applicability",
            },
        ],
        "existing_capabilities": ["meal_composition_calculation", "study_level_evidence_retrieval"],
        "boundary": (
            "Meal composition, methylation classification scores, and graph paths do not estimate "
            "an individual's diet-induced health effect. No personal risk or effect is returned."
        ),
    }
