"""Source-bound food description checks, with no inferred human approval."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

SCHEMA = "nutriomics-food-semantic-precheck-v1"
DESCRIPTOR_TERMS = re.compile(
    r"\b(?:raw|cooked|boiled|steamed|fried|roasted|baked|dried|dehydrated|canned|frozen)\b"
    r"|생것|날것|삶은|데친|구운|볶은|말린|찌거나|찐것|찐 것", re.IGNORECASE)


def record_hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def prepare_food_checks(review, panels):
    """Verify the original panel bytes and preserve source-description uncertainty.

    Lexical spans are candidate interpretation aids, never an ontology mapping,
    original preparation field, edible-portion validation, or expert decision.
    """
    if not isinstance(review.get("records"), list) or not review["records"]:
        raise ValueError("Food review records are required")
    lookup, panel_proofs = {}, {}
    for scope, path in panels.items():
        path = Path(path)
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        data = json.loads(raw)
        values = data.get("foods")
        if not isinstance(values, list):
            raise ValueError("Panel requires food records")
        mapped = {}
        for value in values:
            key = record_hash(value)
            if key in mapped:
                raise ValueError("Duplicate panel record")
            mapped[key] = value
        lookup[scope] = mapped
        panel_proofs[scope] = {"sha256": sha, "records": len(values), "name": path.name}
    rows, seen = [], set()
    for original in review["records"]:
        scope, food_id = original.get("scope"), original.get("food_id")
        if (scope, food_id) in seen or not food_id:
            raise ValueError("Duplicate or missing food identity")
        seen.add((scope, food_id))
        if scope not in lookup or original.get("panel_file_sha256") != panel_proofs[scope]["sha256"]:
            raise ValueError("Stale or unbound food panel")
        source = lookup[scope].get(original.get("record_sha256"))
        if source is None:
            raise ValueError("Food source record hash differs")
        description = source.get("source_description", source.get("food_name"))
        if description != original.get("original_food_description"):
            raise ValueError("Original descriptor was changed in the review worksheet")
        if scope == "public" and source.get("id") != food_id:
            raise ValueError("Public food identifier differs")
        if scope == "internal" and ":" + str(source.get("food_code")) + ":" not in food_id:
            raise ValueError("Internal food code differs")
        spans = [{"text": m.group(), "start": m.start(), "end": m.end()}
                 for m in DESCRIPTOR_TERMS.finditer(description or "")]
        recorded_preparation = source.get("preparation_state")
        project_only = original.get("preparation_field_origin") == "project_selection_curation"
        rows.append({
            "scope": scope, "food_id": food_id, "display_name": original.get("display_name"),
            "source_record_sha256": original["record_sha256"],
            "panel_sha256": original["panel_file_sha256"],
            "source_original_description": description,
            "description_candidate_spans": spans,
            "recorded_preparation": recorded_preparation,
            "recorded_preparation_origin": original.get("preparation_field_origin"),
            "explicit_source_preparation_field_confirmed": False,
            "edible_state_as_selected": source.get("edible_state"),
            "edible_portion_source_definition_confirmed": False,
            "basis_definition_status": "requires_original_source_definition",
            "source_identity_check": "passed",
            "numerical_validation_scope": original.get("numerical_validation"),
            "automatic_findings": [
                "Original panel bytes, record hash, food identifier and descriptor agree.",
                "Descriptor spans are interpretation candidates, not verified preparation fields.",
                "Project selection preparation is not a newly source-verified field." if project_only
                else "No original preparation field is established by this selection.",
                "A selected edible-portion label is not independent evidence of the original measurement basis.",
            ],
            "proposed_handling": "Preserve unspecified fields as unknown; do not infer preparation or density conversion from the display name.",
            "human_questions": [
                "Does the selected source food match the intended food and cooking state, or should its state remain unspecified?",
                "Does the source define the per-100g edible basis required for gram calculation? Exclude incompatible volume/dry-weight records unless a documented conversion exists.",
                "Is any mixed-food source used once, without adding a second recipe-derived total?",
            ],
            "review_status": "pending", "reviewer": None, "reviewer_attestation": False,
            "expert_verified": False,
        })
    return {"schema_version": SCHEMA, "tasks": len(rows), "human_approved": 0,
        "by_scope": dict(Counter(r["scope"] for r in rows)), "panel_proofs": panel_proofs,
        "records_with_descriptor_candidates": sum(bool(r["description_candidate_spans"]) for r in rows),
        "source_preparation_field_confirmed": 0, "food_identity_hash_checks_passed": len(rows),
        "records": rows,
        "limits": ["Source integrity and descriptor hints do not constitute food-state, edible-basis or recipe expert review.",
                   "No automatic preparation ontology, density conversion, approval or database update is performed."]}
