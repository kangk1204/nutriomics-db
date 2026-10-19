import hashlib
import json
import zipfile

import pytest

from scripts.validate_public_foods import validate_accounting, read_sources


def test_dropped_nutrient_does_not_pass_as_a_fast_success():
    result = {"components": [{"source_records": [{"measurement_id": "a"}]}]}
    assert validate_accounting(result, {"a", "b"}) == ["source_measurements_silently_missing_or_added"]


def test_explicit_exclusion_and_zero_source_are_accounted_separately():
    result = {"components": [{"amount": 0, "source_records": [{"measurement_id": "zero"}]}],
              "excluded_measurements": [{"measurement_id": "missing", "reasons": ["not_measured"]}]}
    assert validate_accounting(result, {"zero", "missing"}) == []
    result["excluded_measurements"][0]["reasons"] = []
    assert "excluded_source_measurement_without_reason" in validate_accounting(result, {"zero", "missing"})


def test_no_calculations_and_duplicate_rows_are_failures():
    assert validate_accounting({"components": []}, set()) == ["no_calculated_components_for_selected_reference_food"]
    result = {"components": [{"source_records": [{"measurement_id": "a"}, {"measurement_id": "a"}]}]}
    assert "duplicate_or_overlapping_measurement_accounting" in validate_accounting(result, {"a"})


def write_source(tmp_path, payload, name="source.zip"):
    raw = json.dumps(payload, ensure_ascii=False).encode()
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("foods.json", raw)
    return path, raw


def test_original_null_food_slots_are_explicitly_audited_with_both_hashes(tmp_path):
    payload = {"FoundationFoods": [{"fdcId": 17, "description": "source β", "foodNutrients": []}, None, None,
                                   {"fdcId": 18, "foodNutrients": []}]}
    path, raw = write_source(tmp_path, payload)
    records, sources = read_sources(tmp_path)
    assert list(records) == ["USDA-FDC:17", "USDA-FDC:18"]
    assert records["USDA-FDC:17"] == payload["FoundationFoods"][0]
    source = sources[0]
    assert source["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert source["member_sha256"] == hashlib.sha256(raw).hexdigest()
    assert source["original_array_entries"] == 4 and source["valid_unique_food_records_added"] == 2
    assert source["skipped_entries"] == 2 and source["skip_reason_counts"] == {"null_food_record": 2}
    assert source["skipped_entry_locations"] == [{"index": 1, "reason": "null_food_record"}, {"index": 2, "reason": "null_food_record"}]


def test_root_metadata_is_not_taken_as_foods_and_invalid_elements_keep_positions(tmp_path):
    write_source(tmp_path, {"metadata": ["not a food"], "SurveyFoods": [None, 5, {}, {"fdcId": True}, {"fdcId": "bad"}, {"fdcId": 20}]})
    records, sources = read_sources(tmp_path)
    assert list(records) == ["USDA-FDC:20"]
    assert sources[0]["ignored_root_fields"] == ["metadata"]
    assert sources[0]["skip_reason_counts"] == {"null_food_record": 1, "non_object_food_record": 1, "missing_fdc_id": 1, "invalid_fdc_id": 2}
    assert [item["index"] for item in sources[0]["skipped_entry_locations"]] == [0, 1, 2, 3, 4]


def test_identical_duplicates_are_audited_but_conflicting_source_values_fail(tmp_path):
    record = {"fdcId": 20, "foodNutrients": [{"amount": 0}]}
    write_source(tmp_path, {"SurveyFoods": [record, record]})
    records, sources = read_sources(tmp_path)
    assert len(records) == 1
    assert sources[0]["skip_reason_counts"] == {"identical_duplicate_food_record": 1}
    write_source(tmp_path, {"SurveyFoods": [{**record, "foodNutrients": [{"amount": 1}]}]}, "z-conflict.zip")
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        read_sources(tmp_path)


def test_ambiguous_food_collections_fail_instead_of_silently_changing_selection(tmp_path):
    write_source(tmp_path, {"FoundationFoods": [{"fdcId": 1}], "SurveyFoods": [{"fdcId": 2}]})
    with pytest.raises(ValueError, match="exactly one recognized"):
        read_sources(tmp_path)
