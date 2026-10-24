"""Evaluate only an explicitly declared target; never infer its endpoint from scores."""

import hashlib
import json
import math
from pathlib import Path


def load_contract(path):
    raw = Path(path).read_bytes()
    contract = json.loads(raw)
    validate_contract(contract)
    return contract, hashlib.sha256(raw).hexdigest()


def validate_contract(contract):
    fields = {
        "schema_version", "declared_by", "decision_reference", "source_kind", "endpoint",
        "entity_kind", "interpretation", "precision_target", "recall_target",
    }
    if not isinstance(contract, dict) or set(contract) != fields:
        raise ValueError("Acceptance contract requires the complete explicit decision schema")
    if contract["schema_version"] != "literature-acceptance-v1":
        raise ValueError("Unsupported literature acceptance contract version")
    for field in ("declared_by", "decision_reference"):
        if not isinstance(contract[field], str) or not contract[field].strip():
            raise ValueError(f"Acceptance contract requires {field}")
    if contract["source_kind"] not in {"repository_plan", "original_external_contract"}:
        raise ValueError("Acceptance source must distinguish repository plan and original contract")
    if contract["endpoint"] not in {"detection", "normalization", "relation"}:
        raise ValueError("Acceptance endpoint must be detection, normalization, or relation")
    kind = contract["entity_kind"]
    if kind is not None and (not isinstance(kind, str) or not kind.strip()):
        raise ValueError("Entity kind must be a named kind or null for the aggregate endpoint")
    if kind is not None and contract["endpoint"] == "relation":
        raise ValueError("Relation endpoint has no entity-kind scoring dimension")
    if contract["interpretation"] not in {"point_estimate", "lower_ci95"}:
        raise ValueError("Acceptance interpretation must explicitly select point estimate or CI")
    for field in ("precision_target", "recall_target"):
        value = contract[field]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 <= value <= 1):
            raise ValueError(f"Invalid {field}")


def acceptance_status(evaluation, contract=None, contract_sha256=None):
    """A threshold decision is separate from source validity and clinical validation."""
    if contract is None:
        return {
            "status": "owner_decision_pending",
            "undecided": ["metric_endpoint", "entity_scope", "point_estimate_or_lower_ci95",
                          "original_contract_and_decision_reference"],
            "repository_plan_targets": {"precision": 0.90, "recall": 0.70},
            "note": "Repository targets are a plan; no threshold pass is inferred.",
        }
    validate_contract(contract)
    result = {
        "contract": contract, "contract_sha256": contract_sha256,
        "scope": "declared heldout threshold only; no clinical or external-contract completion claim",
    }
    if evaluation.get("status") != "measured_on_manually_reviewed_gold":
        return {**result, "status": "manual_gold_pending"}
    if "heldout" not in evaluation.get("splits", {}):
        return {**result, "status": "heldout_measurement_pending"}
    if (evaluation.get("cohort_documents") != 200
            or evaluation.get("cohort_split_documents") != {"development": 100, "heldout": 100}
            or evaluation.get("cohort_double_review_documents") != 40):
        return {**result, "status": "planned_cohort_requirements_not_met"}
    if (evaluation.get("cohort_reviewed_documents") != 200
            or evaluation.get("cohort_completed_gold_documents") != 200
            or evaluation.get("cohort_completed_double_review_documents") != 40):
        return {**result, "status": "complete_cohort_manual_gold_pending"}
    heldout = evaluation["splits"]["heldout"]
    if contract["entity_kind"] is None:
        metrics = heldout[contract["endpoint"]]
    else:
        metrics = heldout.get("by_kind", {}).get(contract["entity_kind"], {}).get(contract["endpoint"])
        if metrics is None:
            return {**result, "status": "selected_endpoint_not_measured"}
    values = {}
    for metric in ("precision", "recall"):
        if contract["interpretation"] == "point_estimate":
            values[metric] = metrics.get(metric)
        else:
            interval = metrics.get(metric + "_ci95")
            values[metric] = interval.get("lower") if interval else None
    if any(value is None for value in values.values()):
        return {**result, "status": "undefined_denominator", "metrics": metrics}
    met = all(values[metric] >= contract[metric + "_target"] for metric in values)
    return {
        **result, "status": "declared_threshold_met" if met else "declared_threshold_not_met",
        "criterion_met": met, "criterion_values": values, "metrics": metrics,
        "gold_sha256": evaluation.get("gold_sha256"),
        "prediction_sha256": evaluation.get("prediction_sha256"),
    }
