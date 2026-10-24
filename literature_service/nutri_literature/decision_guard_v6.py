"""Conservative experimental v6 guards, preserving all review candidates.

These checks enforce representation invariants. They do not certify a relation,
infer a species/identifier, or convert model scores into scientific confidence.
"""

from pathlib import Path

from .clinical_refinement_v6 import distinct_arms
from .db import digest, dumps
from .refinement_v6 import automatic_relation_eligible

ENGINE = "heuristic-v6-guarded"
POLICY = {
    "resolved_relation_endpoints_required": True,
    "unresolved_candidates_preserved_for_review": True,
    "same_arm_comparisons_forbidden": True,
    "joint_classifier_adopted": False,
    "experimental_clinical_role_models_adopted": False,
    "confirmed_evidence_requires_human_review": True,
}


def implementation():
    return {
        name: digest((Path(__file__).parent / name).read_bytes())
        for name in ("decision_guard_v6.py", "refinement_v6.py", "clinical_refinement_v6.py")
    }


def guard_relation_hypotheses(hypotheses, oracle=False):
    rows = []
    for hypothesis in hypotheses:
        eligible, reasons = automatic_relation_eligible(hypothesis, oracle)
        proposed = hypothesis.get("raw_type_proposed", hypothesis["type_selected"])
        rows.append(
            {
                **hypothesis,
                "raw_type_proposed": proposed,
                "type_selected": bool(proposed and eligible),
                "automatic_identity_gate": "passed" if eligible else "deferred",
                "automatic_gate_reasons": reasons,
                "review_candidate_preserved": True,
            }
        )
    return rows


def guard_comparisons(comparisons):
    accepted, deferred = [], []
    for comparison in comparisons:
        if distinct_arms(comparison["intervention_description"], comparison["comparator_description"]):
            accepted.append({**comparison, "comparison_identity_check": "passed"})
        else:
            deferred.append(
                {
                    **comparison,
                    "status": "needs_review",
                    "comparison_identity_check": "failed_same_arm",
                    "automatic_selected": False,
                    "reasons": ["same_intervention_and_comparator"],
                }
            )
    return accepted, deferred


def apply(result, oracle=False):
    """Return a new variant; do not mutate the retained v6 result or evidence."""
    if result["engine_version"] == ENGINE:
        if result["manifest"].get("decision_guard", {}).get("source_hashes") != implementation():
            raise ValueError("Guard implementation changed; re-extract from the retained upstream source")
        return {**result}
    relations = guard_relation_hypotheses(result["relation_hypotheses"], oracle)
    observations = []
    for record in result["clinical_observations"]:
        intervention, comparator = record.get("intervention"), record.get("comparator")
        if intervention and comparator and not distinct_arms(intervention["text"], comparator["text"]):
            record = {
                **record,
                "status": "needs_review",
                "reasons": sorted(set(record.get("reasons", []) + ["same_intervention_and_comparator"])),
                "original_ico_evidence_priority_score": record.get("ico_evidence_priority_score"),
                "ico_evidence_priority_score": None,
                "verification": {
                    **record.get("verification", {}),
                    "condition_binding": "failed_same_arm",
                    "automatic_ico_evidence": "deferred",
                },
            }
        observations.append(record)
    automatic_assertions, deferred_assertions = [], []
    for assertion in result.get("assertions", []):
        payload = assertion.get("payload", {})
        endpoints = [payload.get("subject_mention", {}), payload.get("object_mention", {})]
        genes = [m for m in endpoints if m.get("kind") == "gene"]
        chemicals = [m for m in endpoints if m.get("kind") in {"component", "nutrient"}]
        reasons = []
        if genes and chemicals:
            _, reasons = automatic_relation_eligible({"gene": genes[0], "chemical": chemicals[0]}, oracle)
        observation = payload.get("observation", {})
        left, right = observation.get("intervention"), observation.get("comparator")
        if left and right and not distinct_arms(left["text"], right["text"]):
            reasons.append("same_intervention_and_comparator")
        if reasons:
            deferred_assertions.append({**assertion, "guard_reasons": reasons, "status": "needs_review"})
        else:
            automatic_assertions.append(assertion)
    hashes = implementation()
    upstream = result["manifest"].get("artifact_identity") or digest(dumps(result["manifest"]))
    identity = digest(dumps({"upstream_artifact_identity": upstream, "policy": POLICY, "sources": hashes}))
    manifest = {
        **result["manifest"],
        "engine_version": ENGINE,
        "upstream_engine_version": result["engine_version"],
        "upstream_rules_version": result["rules_version"],
        "upstream_artifact_identity": upstream,
        "artifact_identity": identity,
        "decision_guard": {"policy": POLICY, "source_hashes": hashes},
    }
    rules = "guarded-sha256:" + digest(dumps({"upstream": result["rules_version"], "sources": hashes}))
    manifest["rules_version"] = rules
    return {
        **result,
        "engine_version": ENGINE,
        "rules_version": rules,
        "manifest": manifest,
        "relation_hypotheses": relations,
        "clinical_observations": observations,
        "assertions": automatic_assertions,
        "review_assertions": deferred_assertions,
        "decision_guard_diagnostics": {
            "raw_relation_proposals": sum(bool(r["raw_type_proposed"]) for r in relations),
            "automatic_relation_candidates": sum(bool(r["type_selected"]) for r in relations),
            "deferred_relation_candidates": sum(
                r["raw_type_proposed"] and not r["type_selected"] for r in relations
            ),
            "same_arm_observations_deferred": sum(
                "same_intervention_and_comparator" in r.get("reasons", []) for r in observations
            ),
        },
    }


def extract(text, sections, dictionaries, metadata=None):
    from .extraction_v6 import extract as upstream

    metadata = metadata or {}
    result = upstream(text, sections, dictionaries, metadata)
    return apply(result, oracle=metadata.get("_gold_entities") is not None)
