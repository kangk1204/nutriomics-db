"""Synthetic contract checks are software tests, never measured gold accuracy."""

import copy
import pytest

from nutri_literature.acceptance import acceptance_status, validate_contract


def contract():
    return {
        "schema_version": "literature-acceptance-v1", "declared_by": "fixture-owner",
        "decision_reference": "fixture decision, not an actual external contract",
        "source_kind": "repository_plan", "endpoint": "detection", "entity_kind": "gene",
        "interpretation": "point_estimate", "precision_target": 0.90, "recall_target": 0.70,
    }


def evaluation():
    metrics = {"tp": 90, "fp": 10, "fn": 10, "precision": .90, "recall": .90,
               "precision_ci95": {"lower": .8256}, "recall_ci95": {"lower": .8256}}
    return {
        "status": "measured_on_manually_reviewed_gold", "cohort_documents": 200,
        "cohort_split_documents": {"development": 100, "heldout": 100},
        "cohort_double_review_documents": 40, "gold_sha256": "fixture-gold",
        "cohort_reviewed_documents": 200, "cohort_completed_gold_documents": 200,
        "cohort_completed_double_review_documents": 40,
        "prediction_sha256": "fixture-predictions",
        "splits": {"heldout": {"detection": metrics,
                                "by_kind": {"gene": {"detection": metrics}}}},
    }


def test_default_has_no_endpoint_or_pass_inferred_from_metrics():
    result = acceptance_status(evaluation())
    assert result["status"] == "owner_decision_pending"
    assert "criterion_met" not in result
    assert "contract" not in result


def test_explicit_point_and_ci_decisions_have_different_results():
    declared = contract()
    measured = evaluation()
    assert acceptance_status(measured, declared)["criterion_met"] is True
    declared["interpretation"] = "lower_ci95"
    assert acceptance_status(measured, declared)["criterion_met"] is False
    assert acceptance_status(measured, declared)["criterion_values"]["precision"] == .8256


@pytest.mark.parametrize("change,status", [
    ("manual", "manual_gold_pending"), ("development_only", "heldout_measurement_pending"),
    ("cohort", "planned_cohort_requirements_not_met"),
    ("undefined", "undefined_denominator"), ("kind", "selected_endpoint_not_measured"),
    ("pending_global_gold", "complete_cohort_manual_gold_pending"),
])
def test_incomplete_evidence_cannot_be_a_threshold_pass(change, status):
    measured, declared = evaluation(), contract()
    if change == "manual":
        measured["status"] = "pending_manual_review"
    elif change == "development_only":
        measured["splits"] = {"development": measured["splits"]["heldout"]}
    elif change == "cohort":
        measured["cohort_double_review_documents"] = 39
    elif change == "undefined":
        measured["splits"]["heldout"]["detection"]["precision"] = None
    elif change == "pending_global_gold":
        measured["cohort_completed_double_review_documents"] = 20
    else:
        declared["entity_kind"] = "absent-kind"
    result = acceptance_status(measured, declared)
    assert result["status"] == status and "criterion_met" not in result


@pytest.mark.parametrize("field,value", [
    ("endpoint", None), ("interpretation", None), ("declared_by", ""),
    ("decision_reference", ""), ("source_kind", "public_access"),
    ("precision_target", True), ("recall_target", float("nan")),
    ("precision_target", 1.1),
])
def test_missing_or_invalid_owner_decision_is_refused(field, value):
    declared = copy.deepcopy(contract())
    declared[field] = value
    with pytest.raises(ValueError):
        validate_contract(declared)


def test_relation_contract_cannot_select_entity_kind():
    declared = contract()
    declared["endpoint"] = "relation"
    with pytest.raises(ValueError, match="entity-kind"):
        validate_contract(declared)
