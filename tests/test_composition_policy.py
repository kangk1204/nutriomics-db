import pytest

from nutrigenomics.composition_policy import assess, select_unique


def check(**overrides):
    fields = {"status": "derived", "amount": 3.33, "unit": "g", "basis": "source_100g",
              "food_metadata": {"data_type": "Survey (FNDDS)"}, "source_id": "USDA-FDC:source-hash"}
    return assess(**(fields | overrides))


def test_official_fndds_reference_is_eligible_without_fabricated_analytical_method():
    result = check()
    assert result["eligible"]
    assert result["derivation_method"] == "published_reference_estimate"
    assert result["source_status"] == "derived"
    assert not check(food_metadata={}, source_id="unverified")["eligible"]


@pytest.mark.parametrize("fields", [{"status": "below_detection", "amount": 0},
                                    {"basis": "source_100ml"}, {"amount": None},
                                    {"amount": -1}, {"amount": float("nan")},
                                    {"metadata": {"basis_verified": False}}])
def test_numeric_value_alone_does_not_allow_calculation(fields):
    assert not check(**fields)["eligible"]


def test_measured_and_assumed_zero_keep_distinct_interpretations():
    measured = check(status="measured_zero", amount=0)
    assumed = check(amount=0, metadata={"derivation_verified": True,
                                       "derivation_method": "assumed_zero"})
    assert measured["zero_interpretation"] == "reported_measured_zero"
    assert assumed["zero_interpretation"] == "assumed_zero"


def test_alternative_measurements_are_selected_without_looking_at_amount():
    rows = [{"row": "derived", "id": "a", "key": "fat", "unit": "mg", "assessment": check()},
            {"row": "measured", "id": "b", "key": "fat", "unit": "g",
             "assessment": check(status="measured")}]
    chosen, excluded = select_unique(rows)
    assert [r["row"] for r in chosen] == ["measured"]
    assert excluded[0][1] == ["alternative_measurement_not_summed"]
