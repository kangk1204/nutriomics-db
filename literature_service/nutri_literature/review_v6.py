"""Source-anchored v6 corrections, including undirected relationships."""

from copy import deepcopy

from .review_eligibility import _anchored, V5_TYPES, corrected_v5_event, v5_correction_reasons

FIELDS = {"benchmark_relation_type", "role_direction", "observation"}
DIRECTIONS = {"chemical_to_gene", "gene_to_chemical", "undirected", "unknown"}


def correction_reasons(correction, payload, text):
    reasons = []
    if "benchmark_relation_type" in correction and correction["benchmark_relation_type"] not in V5_TYPES:
        reasons.append("invalid_benchmark_relation_type")
    direction = correction.get("role_direction", payload.get("event", {}).get("role_direction"))
    if direction not in DIRECTIONS and direction is not None:
        reasons.append("invalid_role_direction")
    if direction == "undirected" and correction.get("predicate", "associated_with") != "associated_with":
        reasons.append("undirected_predicate_conflict")
    if direction in {"chemical_to_gene", "gene_to_chemical"}:
        effective = {
            **correction,
            "subject": correction.get("subject", payload.get("event", {}).get("subject")),
            "role_direction": direction,
        }
        reasons.extend(v5_correction_reasons(effective, payload))
    if "observation" in correction:
        observation = correction["observation"]
        original = payload.get("observation", {})
        if (
            not original
            or not isinstance(observation, dict)
            or set(observation)
            - {"intervention", "outcome", "comparator", "conditions", "significance", "null_interpretation"}
        ):
            reasons.append("invalid_observation_correction")
        else:
            for field in ("intervention", "outcome", "comparator"):
                if (
                    field in observation
                    and observation[field] is not None
                    and not _anchored(observation[field], text)
                ):
                    reasons.append("unanchored_observation_" + field)
            if observation.get("significance", "unknown") not in {
                "unknown",
                "significant",
                "not_significant",
            }:
                reasons.append("invalid_observation_significance")
            if observation.get("null_interpretation") not in {
                None,
                "no_effect_reported",
                "not_increased",
                "non_significant",
            }:
                reasons.append("invalid_null_interpretation")
            if "conditions" in observation:
                conditions = observation["conditions"]
                if not isinstance(conditions, dict) or set(conditions) - set(original.get("conditions", {})):
                    reasons.append("invalid_observation_conditions")
                else:
                    for field in ("comparator", "timepoint", "arm_evidence"):
                        if conditions.get(field) is not None and not _anchored(conditions[field], text):
                            reasons.append("unanchored_observation_" + field)
                    for field in ("quantities", "statistics", "unassigned_quantities"):
                        values = conditions.get(field, [])
                        if not isinstance(values, list) or any(not _anchored(v, text) for v in values):
                            reasons.append("unanchored_observation_" + field)
    if payload.get("observation"):
        effective = corrected_observation(payload, correction)
        intervention = effective.get("intervention") or {}
        outcome = effective.get("outcome") or {}
        if "subject" in correction and correction["subject"] not in {
            intervention.get("normalized_id"),
            intervention.get("text"),
        }:
            reasons.append("corrected_observation_subject_conflict")
        if "object" in correction and correction["object"] != outcome.get("text"):
            reasons.append("corrected_observation_outcome_conflict")
        if correction.get("predicate", "has_observation") not in {
            "has_observation", "affects", "associated_with"
        }:
            reasons.append("observation_effect_direction_unconfirmed")
    return reasons


def corrected_event(payload, correction):
    event = corrected_v5_event(payload, correction)
    if "direction" not in correction and "predicate" in correction:
        event["direction"] = {
            "increases": "increase",
            "decreases": "decrease",
            "associated_with": "association",
            "affects": "change",
            "binds": "change",
        }.get(correction["predicate"], event.get("direction"))
        event["polarity"] = (
            "null"
            if event.get("direction") == "no_effect"
            else "negated"
            if event.get("negation")
            else "positive"
        )
    if event.get("role_direction") == "undirected":
        event["endpoint_order_is_causal"] = False
    return event


def corrected_observation(payload, correction):
    value = deepcopy(payload.get("observation", {}))
    value.update(correction.get("observation", {}))
    return value


def eligibility(payload, text):
    from .review_eligibility import event_review_reasons

    if payload.get("observation"):
        observation = payload["observation"]
        reasons = []
        for field in ("intervention", "outcome"):
            if not _anchored(observation.get(field), text):
                reasons.append("unanchored_observation_" + field)
        if observation.get("assertion_state") != "observed":
            reasons.append("observation_unconfirmed")
        quotes = observation.get("evidence_quotes", [])
        if not quotes or any(not _anchored(q, text) for q in quotes):
            reasons.append("unanchored_observation_evidence")
        elif all(_anchored(observation.get(k), text) for k in ("intervention", "outcome")) and not any(
            all(
                q["start"] <= observation[k]["start"] < observation[k]["end"] <= q["end"]
                for k in ("intervention", "outcome")
            )
            for q in quotes
        ):
            reasons.append("observation_roles_outside_evidence")
        reasons.extend(
            correction_reasons(
                {
                    "observation": {
                        k: observation[k]
                        for k in ("conditions", "significance", "null_interpretation")
                        if k in observation
                    }
                },
                payload,
                text,
            )
        )
        return reasons
    # V4 validates the shared scientific fields without the V5 directed correction assumption.
    reasons = event_review_reasons(payload, text, "heuristic-v4")
    event = payload.get("event", {})
    reasons.extend(
        correction_reasons(
            {
                "role_direction": event.get("role_direction"),
                "subject": event.get("subject"),
                "predicate": "associated_with" if event.get("role_direction") == "undirected" else "affects",
            },
            payload,
            text,
        )
    )
    return sorted(set(reasons))
