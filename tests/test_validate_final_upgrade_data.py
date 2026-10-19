"""Typed preservation, exact clinical additions and checkpointed source arithmetic."""
import json
import hashlib
import sqlite3
import struct
from collections import defaultdict
from pathlib import Path

import pytest

from nutrigenomics.explorer_build import canonical, sha256
from nutrigenomics.kfind import NUTRIENT_CROSSWALK, food_identity
from scripts import validate_final_upgrade_data as validation
from scripts.build_final_upgrade_release import build
from test_final_upgrade_release import inputs as inputs, parent_release  # noqa: F401 -- shared small real SQLite fixtures


def update_manifest(parent, database):
    path = parent / "manifest.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["database_sha256"] = sha256(database)
    path.write_text(canonical(value), encoding="utf-8")


def add_exposure(parent, database):
    with sqlite3.connect(database) as db:
        value = {"id": "e1", "study_id": "S03", "component_keys": [], "food_terms": []}
        db.execute("INSERT INTO science_record VALUES('e1','exposure',?)", (canonical(value),))
        db.execute("INSERT INTO node VALUES(?,?,?,?,?,?,?,?)", ("Exposure:e1", "diet", "Original exposure", "Original exposure",
            "[]", '{"original_metadata":true}', '["lipids"]', 1))
        db.execute("INSERT INTO node_domain VALUES('Exposure:e1','lipids')")
        db.execute("INSERT INTO edge VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("exposure-link", "food", "Exposure:e1", "candidate_food_match",
            "candidate", "pending", None, "{}", '["own"]', "[]", "[]"))
    update_manifest(parent, database)


@pytest.mark.parametrize("public", [False, True])
def test_real_additive_release_preserves_original_statistics_and_exact_new_values(inputs, public):
    root, config, _ = inputs
    parent, final = root / "parent", root / "final"
    original, _ = parent_release(parent, public)
    add_exposure(parent, original)
    parent_sha = sha256(original)
    build(parent, final, "final-r", config, public=public)
    output = root / "validation.json"
    result = validation.validate(parent, final, output, public=public)
    assert result["passed"], result
    assert result["all_original_values_and_allowed_additions_match"]
    assert result["table_comparisons"]["effect"]["equal"]
    assert result["table_comparisons"]["measurement"]["equal"]
    assert result["table_comparisons"]["science_record"]["expected_additions"]["rows"] == 2
    assert result["table_comparisons"]["edge"]["expected_additions"]["rows"] == 2
    assert result["table_comparisons"]["node"]["expected_additions"]["rows"] == 2
    assert result["full_internal_validation_complete"] is False
    assert sha256(original) == parent_sha
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "passed"


@pytest.mark.parametrize("sql,table", [
    ("UPDATE effect SET value=0 WHERE id='y'", "effect"),
    ("UPDATE effect SET q_value=0 WHERE id='x'", "effect"),
    ("UPDATE science_record SET metadata_json='{}' WHERE kind='study'", "science_record"),
    ("UPDATE source SET metadata_json=json_set(metadata_json,'$.export_allowed',json('false')) WHERE id='own'", "source"),
    ("UPDATE node SET label='changed' WHERE id='food'", "node"),
    ("UPDATE science_record SET metadata_json=json_set(metadata_json,'$.n_people',999) WHERE kind='clinical_endpoint_result'", "science_record"),
    ("UPDATE edge SET review_status='approved' WHERE id LIKE 'ClinicalResultEdge:%'", "edge"),
])
def test_preservation_detects_null_zero_fdr_source_permission_and_clinical_mutations(inputs, sql, table):
    root, config, _ = inputs
    parent, final = root / "parent", root / "final"
    parent_release(parent)
    build(parent, final, "final-r", config)
    database = final / "explorer.sqlite"
    with sqlite3.connect(database) as db:
        db.execute(sql)
    update_manifest(final, database)
    result = validation.validate(parent, final, root / "validation.json")
    assert not result["passed"] and not result["table_comparisons"][table]["equal"]


def test_changed_degree_is_detected_independently_of_projected_node_digest(inputs):
    root, config, _ = inputs
    parent, final = root / "parent", root / "final"
    parent_release(parent)
    build(parent, final, "final-r", config)
    with sqlite3.connect(final / "explorer.sqlite") as db:
        db.execute("UPDATE node SET degree=999 WHERE id='food'")
    update_manifest(final, final / "explorer.sqlite")
    result = validation.validate(parent, final, root / "validation.json")
    assert not result["passed"]
    assert any(r["code"] == "node_degree_not_recomputed_from_actual_edges" for r in result["differences"])


def test_binary_digest_matches_documented_scalar_framing_and_physical_scan():
    values = (None, -7, 1.25, "β 한글", b"\x00\xff")
    text = values[3].encode("utf-8")
    expected_row = (b"N" + b"I" + struct.pack(">q", -7) + b"F" + struct.pack(">d", 1.25)
                    + b"S" + struct.pack(">Q", len(text)) + text
                    + b"B" + struct.pack(">Q", 2) + values[4])
    expected = hashlib.sha256(hashlib.sha256(expected_row).digest()).hexdigest()
    result = validation._digest(["a", "b", "c", "d", "e"], [values])
    assert result["sha256"] == expected
    assert result["algorithm"] == "sorted-canonical-binary-sqlite-row-sha256-v3"
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE x(a,b,c,d,e)")
        db.execute("INSERT INTO x VALUES(?,?,?,?,?)", values)
        assert validation._scan_digest(db, "x") == result


@pytest.mark.parametrize("left,right", [
    (1, 1.0), (None, 0), (0, ""), ("", b""),
    ("β", "β".encode("utf-8")), ("é", "e\u0301"),
    (b"\x00\xff", b"\x00\xfe"), (0.0, -0.0),
])
def test_binary_digest_preserves_native_types_exact_unicode_blob_and_real_bits(left, right):
    assert validation._digest(["value"], [(left,)]) != validation._digest(["value"], [(right,)])


def test_binary_digest_preserves_duplicate_multiplicity_and_column_order():
    rows = [("x", None, b"a"), ("x", 0, b""), ("x", None, b"a")]
    result = validation._digest(["a", "b", "c"], rows)
    assert result == validation._digest(["a", "b", "c"], reversed(rows))
    assert result != validation._digest(["a", "b", "c"], rows[:2])
    assert result != validation._digest(["c", "b", "a"], rows)
    assert result != validation._digest(["a", "b", "c"], [tuple(reversed(row)) for row in rows])
    assert validation._digest(["a"], [("ab",), ("c",)]) != validation._digest(["a"], [("a",), ("bc",)])


def test_fixed_10000_row_microbenchmark_records_actual_timings_and_improvement():
    result = validation.digest_microbenchmark()
    assert result["rows"] == 10000 and result["columns"] == 16 and result["repeats"] == 3
    assert result["stable_within_each_algorithm"]
    assert all(len(timings) == 3 and all(t > 0 for t in timings) for timings in result["seconds"].values())
    assert result["actual_improvement_observed"], result
    assert result["speedup"] > 1


def populate_fixed_foods(db):
    selection = json.loads((Path(__file__).resolve().parents[1] / "config/kfind_food_selection.json").read_text(encoding="utf-8"))
    tokens = {}
    for field, info in NUTRIENT_CROSSWALK.items():
        identifier = "FDCNutrient:" + info["fdc_id"]
        db.execute("INSERT INTO node VALUES(?,?,?,?,?,?,?,?)", (identifier, "nutrient", field, field, "[]",
            canonical({"component_key": "FDC-NUTRIENT:" + info["fdc_id"], "canonical_unit": info["unit"]}), "[]", 0))
    for i, item in enumerate(selection["foods"]):
        food = food_identity(str(item["dataset_id"]), item["food_code"], item["food_name"])[0]
        db.execute("INSERT INTO node VALUES(?,?,?,?,?,?,?,?)", (food, "food", item["name"], item["name_en"], "[]",
            canonical({"core_food_id": i + 1, "core_source_id": "R02"}), "[]", 0))
        for j, (field, info) in enumerate(NUTRIENT_CROSSWALK.items()):
            token = "0" if j == 1 else None if i == 0 and j == 2 else "Tr" if i == 1 and j == 3 else str(i + j / 10)
            status = "not_measured" if token is None else "below_detection" if token == "Tr" else "measured_zero" if float(token) == 0 else "measured"
            amount = None if token in {None, "Tr"} else float(token)
            tokens[(i + 1, field)] = token
            raw = {"provider_nutrient_field": field, "provider_value_raw": token,
                "source_ref": food + ":nutrient:" + field, "release": "fixed-source-v1"}
            db.execute("INSERT INTO measurement VALUES(?,?,?,?,?,?,?,?,?)", (f"fixed:{i}:{j}", food,
                "FDCNutrient:" + info["fdc_id"], amount, info["unit"], "source_100g", status, "own", canonical(raw)))
    return selection, tokens


def mock_meals(selection, tokens, calls):
    source_ids = {i + 1: food_identity(str(item["dataset_id"]), item["food_code"], item["food_name"])[0]
                  for i, item in enumerate(selection["foods"])}
    def response(base, items):
        calls.append(items)
        totals, sources, unknown = defaultdict(float), defaultdict(list), []
        for item in items:
            food_id, grams = item["food_id"], item["amount_g"]
            for field, info in NUTRIENT_CROSSWALK.items():
                token = tokens[(food_id, field)]
                key = ("FDC-NUTRIENT:" + info["fdc_id"], info["unit"])
                if token in {None, "Tr"}:
                    unknown.append({"food_id": food_id, "component_key": key[0], "status": "not_measured" if token is None else "below_detection"})
                    continue
                totals[key] += float(token) * grams / 100
                sources[key].append({"source_id": "R02", "source_ref": source_ids[food_id] + ":nutrient:" + field,
                                     "release": "fixed-source-v1", "basis": "source_100g"})
        value = {"items": items, "individual_prediction": False, "unquantified": unknown,
                 "components": [{"component_key": key[0], "unit": key[1], "amount": round(amount, 9), "source_records": sources[key]}
                                for key, amount in totals.items()]}
        return value, {"http_status": 200, "items": len(items), "seconds": .001}
    return response


def test_fixed_raw_values_zero_missing_and_50_single_plus_batch_requests_checkpoint(inputs, monkeypatch):
    root, config, _ = inputs
    parent, final = root / "parent", root / "final"
    original, _ = parent_release(parent)
    with sqlite3.connect(original) as db:
        selection, tokens = populate_fixed_foods(db)
    update_manifest(parent, original)
    build(parent, final, "final-r", config)
    calls = []
    monkeypatch.setattr(validation, "_api_meta", lambda *args: None)
    monkeypatch.setattr(validation, "_post_meal", mock_meals(selection, tokens, calls))
    result = validation.validate(parent, final, root / "validation.json", base="fixture")
    assert result["passed"] and result["full_internal_validation_complete"], result
    food = result["food_validation"]
    assert food["food_count"] == 50 and food["composition_count"] == 1200
    assert food["completed_foods"] == 50 and len(calls) == len(food["api_checks"]) == 51
    assert [len(items) for items in calls] == [1] * 50 + [50]
    assert food["all50_batch_passed"] is True


def test_http_failure_keeps_completed_table_and_1200_source_value_proof(inputs, monkeypatch):
    root, config, _ = inputs
    parent, final = root / "parent", root / "final"
    original, _ = parent_release(parent)
    with sqlite3.connect(original) as db:
        populate_fixed_foods(db)
    update_manifest(parent, original)
    build(parent, final, "final-r", config)
    monkeypatch.setattr(validation, "_api_meta", lambda *args: None)
    def fail(*args):
        raise TimeoutError("controlled HTTP failure")
    monkeypatch.setattr(validation, "_post_meal", fail)
    path = root / "validation.json"
    result = validation.validate(parent, final, path, base="fixture")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == saved["status"] == "failed"
    assert saved["all_original_values_and_allowed_additions_match"] is True
    assert all(r["equal"] for r in saved["table_comparisons"].values())
    assert saved["food_validation"]["composition_count"] == 1200
    assert saved["food_validation"]["source_values_match"] is True
    assert saved["food_validation"]["error_type"] == "TimeoutError"
    assert saved["food_validation"]["completed_foods"] == 0
