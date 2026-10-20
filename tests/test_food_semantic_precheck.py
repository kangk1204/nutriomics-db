import hashlib
import json

import pytest

from nutrigenomics.food_semantic_precheck import prepare_food_checks, record_hash


def fixture_data(tmp_path):
    value = {"id": "USDA-FDC:1", "name": "Cooked apple", "source_description": "Apples, raw",
             "preparation_state": None, "edible_state": "edible_portion"}
    panel = tmp_path / "panel.json"
    panel.write_text(json.dumps({"foods": [value]}), encoding="utf-8")
    row = {"scope": "public", "food_id": value["id"], "display_name": value["name"],
        "original_food_description": value["source_description"], "record_sha256": record_hash(value),
        "panel_file_sha256": hashlib.sha256(panel.read_bytes()).hexdigest(),
        "preparation_field_origin": "public_selection_metadata"}
    return {"records": [row]}, {"public": panel}


def test_descriptor_is_source_hint_and_never_approval(tmp_path):
    review, panels = fixture_data(tmp_path)
    result = prepare_food_checks(review, panels)
    row = result["records"][0]
    assert [r["text"] for r in row["description_candidate_spans"]] == ["raw"]
    assert row["recorded_preparation"] is None
    assert not row["explicit_source_preparation_field_confirmed"]
    assert not row["edible_portion_source_definition_confirmed"]
    assert row["review_status"] == "pending" and row["reviewer"] is None
    assert result["human_approved"] == 0


@pytest.mark.parametrize("mutation", ["panel", "record", "descriptor", "identifier", "duplicate"])
def test_stale_identity_and_modified_review_source_are_rejected(tmp_path, mutation):
    review, panels = fixture_data(tmp_path)
    row = review["records"][0]
    if mutation == "panel":
        panels["public"].write_text('{}', encoding="utf-8")
    elif mutation == "record":
        row["record_sha256"] = "0" * 64
    elif mutation == "descriptor":
        row["original_food_description"] = "Apples, cooked"
    elif mutation == "identifier":
        row["food_id"] = "USDA-FDC:2"
    else:
        review["records"].append(dict(row))
    with pytest.raises(ValueError):
        prepare_food_checks(review, panels)
