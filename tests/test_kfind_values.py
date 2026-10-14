from __future__ import annotations

import json
from pathlib import Path

import pytest

from nutrigenomics.kfind import (
    NUTRIENT_CROSSWALK,
    food_identity,
    parse_100g_basis,
    parse_source_value,
    validate_selection,
)


def test_source_values_preserve_zero_missing_and_below_detection() -> None:
    assert parse_source_value("0.000").status == "measured_zero"
    assert parse_source_value("1.25").amount == 1.25
    assert parse_source_value("").status == "not_measured"
    assert parse_source_value(None).status == "not_measured"
    assert parse_source_value("Tr").status == "below_detection"
    assert parse_source_value("<0.01").status == "below_detection"


def test_unexpected_and_negative_source_values_are_not_silently_coerced() -> None:
    assert parse_source_value("확인 필요").status == "unparsed_source_value"
    assert parse_source_value("-0.2").status == "source_conflict"


def test_only_mass_basis_can_be_scaled_by_gram_intake() -> None:
    assert parse_100g_basis("100g") == "source_100g"
    assert parse_100g_basis("100 ml") == "source_100ml"
    assert parse_100g_basis("1회 제공량") is None


def test_duplicate_provider_food_codes_are_disambiguated_by_food_name() -> None:
    first = food_identity("15100065", "R211-717273971-0000", "추젓")
    second = food_identity("15100065", "R211-717273971-0000", "육젓")
    assert first != second
    assert first == food_identity("15100065", "R211-717273971-0000", "추젓")


def test_frozen_korean_curation_has_50_unique_records() -> None:
    selection_path = Path(__file__).parents[1] / "config" / "kfind_food_selection.json"
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    foods = validate_selection(selection)
    assert len(foods) == 50
    assert {item["dataset_id"] for item in foods} == {"15100065", "15100070"}
    assert len(NUTRIENT_CROSSWALK) == 24


def test_selection_rejects_duplicate_source_identity() -> None:
    one = {
        "dataset_id": "15100065",
        "food_code": "R1",
        "food_name": "밥",
        "name": "밥",
        "name_en": "rice",
        "food_group": "곡류",
        "preparation_state": "조리",
    }
    with pytest.raises(ValueError, match="Duplicate selected food identity"):
        validate_selection({"foods": [one, dict(one)]})


def test_selection_can_disambiguate_different_names_sharing_a_provider_code() -> None:
    one = {
        "dataset_id": "15100065",
        "food_code": "R1",
        "food_name": "추젓",
        "name": "새우젓(추젓)",
        "name_en": "Salted shrimp (chujeot)",
        "food_group": "수산물",
        "preparation_state": "젓갈",
    }
    another = {**one, "food_name": "육젓", "name": "새우젓(육젓)"}
    assert validate_selection({"foods": [one, another]}) == [one, another]
