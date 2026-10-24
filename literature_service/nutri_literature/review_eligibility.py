"""Strict assertion eligibility shared by review and read-only projections."""

import json

EVENT_ENUMS = {
    "outcome_type": {"expression", "activity", "methylation", "concentration", "health_outcome", "unknown"},
    "direction": {"increase", "decrease", "association", "change", "no_effect"},
    "significance": {"significant", "not_significant", "unknown"},
    "species": {"human", "animal", "mixed", "unknown"},
    "study_model": {"clinical", "cell", "animal", "mixed", "unknown"},
}
EVENT_CORRECTION_FIELDS = frozenset(EVENT_ENUMS) | {"negation", "speculation", "conditions"}
V5_CORRECTION_FIELDS = {"benchmark_relation_type", "role_direction"}
V5_TYPES = {
    "Association",
    "Positive_Correlation",
    "Negative_Correlation",
    "Bind",
    "Comparison",
    "Cotreatment",
    "Conversion",
    "Drug_Interaction",
}


def v5_correction_reasons(correction, payload):
    reasons = []
    if "benchmark_relation_type" in correction and correction["benchmark_relation_type"] not in V5_TYPES:
        reasons.append("invalid_benchmark_relation_type")
    if "role_direction" in correction:
        event = payload.get("event", {})
        kinds = {m.get("text"): m.get("kind") for m in (event.get("controller", {}), event.get("theme", {}))}
        kinds.update(
            {
                m.get("normalized_id"): m.get("kind")
                for m in (event.get("controller", {}), event.get("theme", {}))
            }
        )
        for mention in (event.get("controller", {}), event.get("theme", {})):
            kinds.update(
                {
                    i: mention.get("kind")
                    for i in mention.get("payload", {}).get("taxon_compatible_candidate_ids", [])
                }
            )
        subject = kinds.get(correction.get("subject"))
        expected = (
            "gene_to_chemical"
            if subject == "gene"
            else "chemical_to_gene"
            if subject in {"component", "nutrient"}
            else None
        )
        if correction["role_direction"] != expected:
            reasons.append("corrected_role_endpoint_conflict")
    return reasons


def corrected_v5_event(payload, correction):
    """Human corrections affect the effective view; original hypotheses stay immutable."""
    from copy import deepcopy

    event = deepcopy(payload.get("event", {}))
    fields = EVENT_CORRECTION_FIELDS | V5_CORRECTION_FIELDS | {"subject", "object"}
    event.update({k: v for k, v in correction.items() if k in fields})
    mentions = [payload.get("event", {}).get("controller", {}), payload.get("event", {}).get("theme", {})]
    for role, endpoint in (("controller", "subject"), ("theme", "object")):
        for mention in mentions:
            ids = {
                mention.get("text"),
                mention.get("normalized_id"),
                *mention.get("payload", {}).get("taxon_compatible_candidate_ids", []),
            }
            if event.get(endpoint) in ids:
                event[role] = deepcopy(mention)
                if event[endpoint] != mention.get("text"):
                    event[role]["normalized_id"] = event[endpoint]
                break
    if event.get("assertion_state") == "observed":
        event["status"] = "candidate"
    event["statistical_significance"] = event.get("significance", "unknown")
    event["polarity"] = (
        "null"
        if event.get("direction") == "no_effect"
        else "negated"
        if event.get("negation")
        else "positive"
    )
    return event


def _anchored(value, text):
    if not isinstance(value, dict):
        return False
    start, end = value.get("start"), value.get("end")
    quote = value.get("quote", value.get("text"))
    return (
        type(start) is int
        and type(end) is int
        and isinstance(quote, str)
        and 0 <= start < end <= len(text)
        and text[start:end] == quote
    )


def event_review_reasons(payload, text, engine):
    """Validate stored v2 semantic fields and every cited span without inference.

    Legacy v1 assertions continue to use their explicit-relation flag. Unknown
    context slots remain visible; an abstained frame cannot be approved.
    """
    if engine == "heuristic-v6":
        from .review_v6 import eligibility
        return eligibility(payload, text)
    if engine not in {"heuristic-v2", "heuristic-v4", "heuristic-v5"}:
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("event"), dict):
        return ["event_missing"]
    event = payload["event"]
    reasons = []
    if engine in {"heuristic-v4", "heuristic-v5"}:
        if event.get("assertion_state") != "observed":
            reasons.append("observation_unconfirmed")
        for role in ("controller", "theme"):
            mention = event.get(role, {})
            if mention.get("kind") == "gene" and not mention.get("normalized_id"):
                reasons.append("gene_identity_unresolved")
    if event.get("status") != "candidate" or event.get("abstention_reasons"):
        reasons.append("event_abstained")
    for field, choices in EVENT_ENUMS.items():
        if not isinstance(event.get(field), str) or event[field] not in choices:
            reasons.append("invalid_" + field)
    for field in ("negation", "speculation"):
        if type(event.get(field)) is not bool:
            reasons.append("invalid_" + field)
    for role, endpoint in (("controller", "subject"), ("theme", "object")):
        mention = event.get(role)
        if not _anchored(mention, text):
            reasons.append("unanchored_" + role)
        elif event.get(endpoint) not in (mention.get("normalized_id"), mention.get("text")):
            reasons.append("invalid_" + role + "_identity")
    if not _anchored(event.get("trigger"), text):
        reasons.append("unanchored_trigger")
    quotes = event.get("evidence_quotes")
    if not isinstance(quotes, list) or not quotes or any(not _anchored(q, text) for q in quotes):
        reasons.append("unanchored_evidence")
    else:
        arguments = [event.get(role) for role in ("controller", "theme", "trigger")]
        if all(_anchored(value, text) for value in arguments) and not any(
            all(quote["start"] <= value["start"] < value["end"] <= quote["end"] for value in arguments)
            for quote in quotes
        ):
            reasons.append("roles_outside_shared_evidence")
    expected_polarity = (
        "null"
        if event.get("direction") == "no_effect"
        else ("negated" if event.get("negation") is True else "positive")
    )
    if event.get("polarity") != expected_polarity:
        reasons.append("invalid_polarity")
    if event.get("statistical_significance") != event.get("significance"):
        reasons.append("significance_conflict")
    conditions = event.get("conditions")
    if not isinstance(conditions, dict):
        reasons.append("invalid_conditions")
    else:
        for field in ("comparator", "timepoint"):
            if conditions.get(field) is not None and not _anchored(conditions[field], text):
                reasons.append("unanchored_" + field)
        quantities = conditions.get("quantities", [])
        if not isinstance(quantities, list) or any(not _anchored(value, text) for value in quantities):
            reasons.append("unanchored_quantities")
        if engine in {"heuristic-v4", "heuristic-v5"}:
            if conditions.get("arm_id") and not _anchored(conditions.get("arm_evidence"), text):
                reasons.append("unanchored_arm_identity")
            for field in ("statistics", "unassigned_quantities"):
                values = conditions.get(field, [])
                if not isinstance(values, list) or any(not _anchored(v, text) for v in values):
                    reasons.append("unanchored_" + field)
    return reasons


def event_correction_reasons(correction, payload, text):
    """Corrections may edit meaning but never replace evidence or model metadata."""
    reasons = []
    for field, choices in EVENT_ENUMS.items():
        if field in correction and (
            not isinstance(correction[field], str) or correction[field] not in choices
        ):
            reasons.append("invalid_" + field)
    for field in ("negation", "speculation"):
        if field in correction and type(correction[field]) is not bool:
            reasons.append("invalid_" + field)
    if "conditions" in correction:
        conditions = correction["conditions"]
        original = payload.get("event", {}).get("conditions", {})
        allowed = {"arm_id", "comparator", "timepoint", "quantities"}
        if payload.get("event", {}).get("assertion_state") is not None:
            allowed |= {"statistics", "unassigned_quantities", "arm_evidence"}
        if not isinstance(conditions, dict) or set(conditions) - allowed:
            reasons.append("invalid_conditions")
        else:
            if conditions.get("arm_id") != original.get("arm_id"):
                reasons.append("unanchored_arm_identity")
            if conditions.get("arm_evidence") is not None and (
                not _anchored(conditions["arm_evidence"], text)
                or conditions["arm_evidence"] != original.get("arm_evidence")
            ):
                reasons.append("unanchored_arm_identity")
            for field in ("comparator", "timepoint"):
                if conditions.get(field) is not None and not _anchored(conditions[field], text):
                    reasons.append("unanchored_" + field)
            quantities = conditions.get("quantities", [])
            if not isinstance(quantities, list) or any(not _anchored(q, text) for q in quantities):
                reasons.append("unanchored_quantities")
            for field in ("statistics", "unassigned_quantities"):
                values = conditions.get(field, [])
                if not isinstance(values, list) or any(not _anchored(v, text) for v in values):
                    reasons.append("unanchored_" + field)
    taxon = payload.get("event", {}).get("taxon_id")
    if (
        taxon
        and "species" in correction
        and correction["species"] != ("human" if taxon == 9606 else "animal")
    ):
        reasons.append("species_identity_conflict")
    return reasons


def has_explicit_relation(payload: object) -> bool:
    """Only a decoded object with literal boolean True qualifies."""
    return isinstance(payload, dict) and payload.get("explicit_relation") is True


def has_explicit_relation_json(encoded: str) -> bool:
    """Decode saved JSON exactly once; malformed/non-object payloads fail closed.

    Other approval and source checks remain the responsibility of the caller.
    A JSON string containing another object is not an assertion object.
    """
    try:
        payload = json.loads(encoded)
    except (TypeError, ValueError):
        return False
    return has_explicit_relation(payload)
