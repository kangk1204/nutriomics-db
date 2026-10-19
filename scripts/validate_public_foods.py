"""Validate a fixed, category-stratified public food set against original USDA JSON.

This checks source values, units, labels, calculation and explicit unknown states;
it does not infer expert approval of food identity or biological applicability.
"""
import argparse
import hashlib
import json
import math
import sqlite3
import urllib.request
import zipfile
from collections import Counter, defaultdict, deque
from pathlib import Path

from nutrigenomics.explorer_build import sha256
from nutrigenomics.source_updates import atomic_json


def validate_accounting(result, measurement_ids):
    """A fast response may not silently drop source rows or all calculations."""
    included = [s.get("measurement_id") for c in result["components"] for s in c.get("source_records", [])]
    excluded = result.get("excluded_measurements", [])
    excluded_ids = [r.get("measurement_id") for r in excluded]
    problems = []
    if not result["components"]:
        problems.append("no_calculated_components_for_selected_reference_food")
    if (len(included) != len(set(included)) or len(excluded_ids) != len(set(excluded_ids))
            or set(included).intersection(excluded_ids)):
        problems.append("duplicate_or_overlapping_measurement_accounting")
    if set(included).union(excluded_ids) != set(measurement_ids):
        problems.append("source_measurements_silently_missing_or_added")
    if any(not row.get("reasons") for row in excluded):
        problems.append("excluded_source_measurement_without_reason")
    return problems


def read_sources(directory):
    records, sources = {}, []
    for path in sorted(Path(directory).glob("*.zip")):
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.endswith(".json")]
            if len(names) != 1:
                raise ValueError("Expected exactly one source JSON member")
            raw = archive.read(names[0])
            data = json.loads(raw)
            known = {"FoundationFoods", "SurveyFoods", "BrandedFoods", "SRLegacyFoods", "ExperimentalFoods"}
            arrays = [(key, value) for key, value in data.items() if key in known and isinstance(value, list)] if isinstance(data, dict) else []
            if len(arrays) != 1:
                raise ValueError("Expected exactly one recognized FoodData Central food collection")
            collection, foods = arrays[0]
            skipped, added = [], 0
            for index, food in enumerate(foods):
                reason = None
                if food is None:
                    reason = "null_food_record"
                elif not isinstance(food, dict):
                    reason = "non_object_food_record"
                elif "fdcId" not in food:
                    reason = "missing_fdc_id"
                elif (isinstance(food["fdcId"], bool) or not isinstance(food["fdcId"], (int, str))
                      or not str(food["fdcId"]).isdigit() or int(food["fdcId"]) <= 0):
                    reason = "invalid_fdc_id"
                if reason:
                    skipped.append({"index": index, "reason": reason})
                    continue
                identifier = "USDA-FDC:" + str(food["fdcId"])
                if identifier in records:
                    if records[identifier] != food:
                        raise ValueError("Conflicting duplicate original FoodData Central identifier: " + identifier)
                    skipped.append({"index": index, "reason": "identical_duplicate_food_record", "id": identifier})
                    continue
                records[identifier] = food
                added += 1
        sources.append({"file": path.name, "sha256": sha256(path), "member": names[0],
            "member_sha256": hashlib.sha256(raw).hexdigest(), "food_collection": collection,
            "original_array_entries": len(foods), "valid_unique_food_records_added": added,
            "skipped_entries": len(skipped), "skip_reason_counts": dict(Counter(item["reason"] for item in skipped)),
            "skipped_entry_locations": skipped,
            "ignored_root_fields": [key for key in data if key != collection]})
    return records, sources


def validate(database, raw_directory, base_url, output, selection=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    raw, sources = read_sources(raw_directory)
    db = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    before = sha256(database)
    groups = defaultdict(deque)
    for row in db.execute("SELECT id,label,metadata_json FROM node WHERE kind='food' ORDER BY label,id"):
        original = raw.get(row["id"])
        if not original or not original.get("foodNutrients"):
            continue
        nutrients = original["foodNutrients"]
        if len({x["nutrient"]["id"] for x in nutrients}) != len(nutrients):
            continue  # Retain ambiguous foods in the DB; exclude from this fixed reference set.
        metadata = json.loads(row["metadata_json"])
        category = original.get("foodCategory", {}).get("description") or original.get("wweiaFoodCategory", {}).get("wweiaFoodCategoryDescription") or "source_category_unknown"
        groups[category].append({"id": row["id"], "name": row["label"], "category": category,
            "preparation_state": metadata.get("preparation_state"),
            "edible_state": metadata.get("edible_state"), "source_description": original["description"]})
    if selection:
        selected = json.loads(Path(selection).read_text(encoding="utf-8"))["foods"]
    else:
        selected = []
        while len(selected) < 50 and any(groups.values()):
            for category in sorted(groups):
                if groups[category] and len(selected) < 50:
                    selected.append(groups[category].popleft())
    if len(selected) != 50 or len({r["id"] for r in selected}) != 50:
        raise ValueError("Exactly fifty distinct public foods are required")
    atomic_json(output / "food-selection.json", {"foods": selected, "sources": sources,
        "selection_policy": "Sorted round-robin across original source food categories; excludes duplicate nutrient identities from this reference set only"})
    differences, checks = [], []
    checked = 0
    for food in selected:
        original = raw[food["id"]]
        values = {str(x["nutrient"]["id"]): x for x in original["foodNutrients"]}
        rows = list(db.execute("SELECT * FROM measurement WHERE food=?", (food["id"],)))
        for row in rows:
            nutrient_id = row["component"].split(":")[-1]
            native = values.get(nutrient_id)
            if not native:
                differences.append({"food": food["id"], "component": row["component"], "reason": "not_in_raw_source"})
                continue
            expected = native.get("amount")
            expected_unit = native["nutrient"]["unitName"]
            if row["amount"] != expected or row["unit"].casefold() != expected_unit.casefold() or row["basis"] != "per_100g_edible_portion":
                differences.append({"food": food["id"], "component": row["component"], "reason": "source_value_unit_basis_mismatch"})
            checked += 1
        for amount in (100, 150):
            request = urllib.request.Request(base_url.rstrip("/") + "/api/v1/meals/analyze",
                data=json.dumps({"items": [{"food_id": food["id"], "amount_g": amount}]}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.load(response)
            for reason in validate_accounting(result, {r["id"] for r in rows}):
                differences.append({"food": food["id"], "amount_g": amount, "reason": reason})
            compared = 0
            for component in result["components"]:
                nid = component["component_key"].split(":")[-1]
                native = values[nid]
                expected = native.get("amount")
                if expected is None or not math.isclose(component["amount"], expected * amount / 100, rel_tol=1e-12, abs_tol=1e-12):
                    differences.append({"food": food["id"], "amount_g": amount, "nutrient": nid, "reason": "intake_calculation_mismatch"})
                compared += 1
            checks.append({"food_id": food["id"], "amount_g": amount, "http_status": 200,
                "components_compared": compared, "excluded": result.get("excluded_measurements"),
                "release_id": result.get("release_id"), "individual_prediction": result.get("individual_prediction")})
        atomic_json(output / "checkpoints.json", {"completed_foods": len(checks) // 2, "api_checks": checks, "differences": differences})
    db.close()
    unchanged = before == sha256(database)
    report = {"status": "passed" if not differences and unchanged else "failed", "food_count": len(selected),
        "source_values_compared": checked, "api_requests": len(checks), "differences": differences,
        "database_sha256": before, "database_unchanged": unchanged, "sources": sources,
        "scope": "Original USDA numerical composition, units, source labels, 100g basis and two intake calculations; expert edible/preparation interpretation pending",
        "preparation_unreported": sum(x["preparation_state"] is None for x in selected),
        "expert_review_status": "pending", "category_count": len({x["category"] for x in selected})}
    atomic_json(output / "validation.json", report)
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("database", "raw-directory", "output"):
        p.add_argument("--" + key, type=Path, required=True)
    p.add_argument("--base-url", required=True)
    p.add_argument("--selection", type=Path)
    a = p.parse_args()
    result = validate(a.database, a.raw_directory, a.base_url, a.output, a.selection)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["status"] == "passed" else 1)
