from __future__ import annotations

import math

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nutrigenomics.composition import analyze_meal, convert_unit
from nutrigenomics.db import Base
from nutrigenomics.models import (
    Component,
    CompositionMeasurement,
    Food,
    Recipe,
    RecipeIngredient,
    Resource,
)
from nutrigenomics.schemas import MealAnalysisInput


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Resource(id="R01", name="test", layer="composition"))
        db.flush()
        yield db
    engine.dispose()


def add_food(session: Session, code: str, name: str, group: str = "ingredient") -> Food:
    food = Food(source_id="R01", source_code=code, name=name, food_group=group)
    session.add(food)
    session.flush()
    return food


def add_measurement(
    session: Session,
    food: Food,
    key: str,
    name: str,
    value: float | None,
    unit: str | None,
    status: str = "measured",
    canonical_unit: str | None = None,
    basis: str = "edible_portion_100g",
) -> None:
    component = session.scalar(select_component(key))
    if component is None:
        component = Component(
            component_key=key,
            name=name,
            category="nutrient",
            canonical_unit=canonical_unit or unit,
        )
        session.add(component)
        session.flush()
    session.add(
        CompositionMeasurement(
            food_id=food.id,
            component_id=component.id,
            source_id="R01",
            source_ref=f"{food.source_code}:{key}",
            release="10.4",
            amount_per_100g=value,
            unit=unit,
            basis=basis,
            status=status,
        )
    )
    session.flush()


def select_component(key: str):
    from sqlalchemy import select

    return select(Component).where(Component.component_key == key)


def test_intake_scales_per_100g_and_converts_units(session: Session):
    rice = add_food(session, "rice-cooked", "밥", "staple")
    add_measurement(session, rice, "protein", "단백질", 3.0, "g")
    add_measurement(session, rice, "vitamin-c", "비타민 C", 1000.0, "ug", canonical_unit="mg")

    result = analyze_meal(session, [{"food_id": rice.id, "amount_g": 150.0}])
    values = {row["name"]: row for row in result["components"]}

    assert result["status"] == "measured"
    assert values["단백질"]["amount"] == pytest.approx(4.5)
    assert values["단백질"]["unit"] == "g"
    assert values["비타민 C"]["amount"] == pytest.approx(1.5)
    assert values["비타민 C"]["unit"] == "mg"


def test_measured_zero_is_kept_and_below_detection_is_not_zero(session: Session):
    food = add_food(session, "sample", "식품")
    add_measurement(session, food, "zero", "측정된 0", 0.0, "mg", "measured_zero")
    add_measurement(session, food, "below", "검출한계 미만", None, "mg", "below_lod")

    result = analyze_meal(session, [{"food_id": food.id, "amount_g": 100.0}])
    values = {row["name"]: row["amount"] for row in result["components"]}

    assert values["측정된 0"] == 0
    assert "검출한계 미만" not in values
    assert result["status"] == "partial"
    assert result["unquantified"][0]["status"] == "below_lod"


def test_direct_food_measurement_is_not_added_to_recipe_estimate(session: Session):
    composite = add_food(session, "dish", "혼합 음식", "dish")
    ingredient = add_food(session, "ingredient", "재료")
    add_measurement(session, composite, "sodium", "나트륨", 100.0, "mg")
    add_measurement(session, ingredient, "sodium", "나트륨", 1000.0, "mg")
    recipe = Recipe(food_id=composite.id, source_ref="recipe-1", release="v1", yield_weight_g=100.0)
    session.add(recipe)
    session.flush()
    session.add(
        RecipeIngredient(recipe_id=recipe.id, food_id=ingredient.id, amount_g=100.0, retention_factor=1.0)
    )
    session.flush()

    result = analyze_meal(session, [{"food_id": composite.id, "amount_g": 50.0}])

    assert result["items"][0]["calculation_mode"] == "direct"
    assert result["components"][0]["amount"] == pytest.approx(50.0)


def test_recipe_fallback_uses_cooked_yield_and_retention(session: Session):
    dish = add_food(session, "soup", "국", "dish")
    ingredient = add_food(session, "salted-water", "양념 재료")
    add_measurement(session, ingredient, "sodium", "나트륨", 1000.0, "mg")
    recipe = Recipe(food_id=dish.id, source_ref="recipe-2", release="v1", yield_weight_g=400.0)
    session.add(recipe)
    session.flush()
    session.add(
        RecipeIngredient(recipe_id=recipe.id, food_id=ingredient.id, amount_g=20.0, retention_factor=0.8)
    )
    session.flush()

    result = analyze_meal(session, [{"food_id": dish.id, "amount_g": 100.0}])

    assert result["items"][0]["calculation_mode"] == "recipe"
    assert result["components"][0]["amount"] == pytest.approx(40.0)


def test_unknown_food_and_invalid_amount_are_rejected(session: Session):
    with pytest.raises(ValueError, match="Unknown food_id"):
        analyze_meal(session, [{"food_id": 9999, "amount_g": 20.0}])
    with pytest.raises(ValidationError):
        MealAnalysisInput(items=[{"food_id": 1, "amount_g": math.inf}])
    with pytest.raises(ValidationError):
        MealAnalysisInput(items=[{"food_id": 1, "amount_g": 0}])


def test_convert_unit_refuses_incompatible_units():
    assert convert_unit(1000.0, "ug", "mg") == pytest.approx(1.0)
    assert convert_unit(1000.0, "UG", "MG") == pytest.approx(1.0)
    assert convert_unit(1.0, "g", "kcal") is None


def test_only_active_food_composition_release_is_used(session: Session):
    food = add_food(session, "release-sample", "재개정 식품")
    food.active_release = "2026"
    add_measurement(session, food, "fiber", "식이섬유", 10.0, "g")
    current = session.query(CompositionMeasurement).filter_by(food_id=food.id).one()
    current.release = "2025"
    session.add(
        CompositionMeasurement(
            food_id=food.id,
            component_id=current.component_id,
            source_id="R01",
            source_ref="release-sample:fiber-2026",
            release="2026",
            amount_per_100g=4.0,
            unit="g",
            basis="edible_portion_100g",
            status="measured",
        )
    )
    result = analyze_meal(session, [{"food_id": food.id, "amount_g": 100.0}])

    assert result["components"][0]["amount"] == pytest.approx(4.0)


@pytest.mark.parametrize("basis", ["edible_portion_100g", "per_100g_edible_portion", "source_100g"])
def test_verified_mass_bases_remain_usable(session: Session, basis: str):
    food = add_food(session, "basis-test", "Food")
    add_measurement(session, food, "protein", "Protein", 2.0, "g", basis=basis)
    result = analyze_meal(session, [{"food_id": food.id, "amount_g": 50.0}])
    assert result["components"][0]["amount"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("basis", "reason"),
    [("source_100ml", "volume_to_mass_conversion_unverified"),
     ("dry_weight_100g", "basis_unsupported_or_unverified")],
)
def test_unsupported_basis_is_retained_without_grams_scaling(session: Session, basis, reason):
    food = add_food(session, "liquid", "Liquid")
    add_measurement(session, food, "protein", "Protein", 2.0, "g", basis=basis)
    result = analyze_meal(session, [{"food_id": food.id, "amount_g": 150.0}])
    assert result["components"] == []
    measurement = next(row for row in result["unquantified"] if row.get("component_key"))
    assert measurement["status"] == "measured"
    assert measurement["source_amount"] == 2.0
    assert measurement["basis"] == basis
    assert measurement["release"] == "10.4"
    assert reason in measurement["exclusion_reasons"]


def test_nonfinite_missing_and_unverified_records_are_all_retained(session: Session):
    food = add_food(session, "incomplete", "Incomplete")
    add_measurement(session, food, "nonfinite", "Nonfinite", math.inf, "mg")
    add_measurement(session, food, "missing", "Missing", None, "mg", "not_measured")
    add_measurement(session, food, "unitless", "Unitless", 3.0, "", "unverified")
    add_measurement(session, food, "basis-unverified", "Basis unverified", 4.0, "mg")
    row = session.query(CompositionMeasurement).filter_by(food_id=food.id).all()[-1]
    row.metadata_json = {"basis_verified": False}
    result = analyze_meal(session, [{"food_id": food.id, "amount_g": 100.0}])
    unknown = {row["component_key"]: row for row in result["unquantified"]
               if row.get("component_key")}
    assert set(unknown) == {"nonfinite", "missing", "unitless", "basis-unverified"}
    assert "amount_nonfinite" in unknown["nonfinite"]["exclusion_reasons"]
    assert unknown["nonfinite"]["source_amount"] is None
    assert unknown["nonfinite"]["source_value_text"] == "inf"
    assert "amount_missing" in unknown["missing"]["exclusion_reasons"]
    assert "unit_missing" in unknown["unitless"]["exclusion_reasons"]
    assert "basis_explicitly_unverified" in unknown["basis-unverified"]["exclusion_reasons"]
    assert result["components"] == []


def test_recipe_fallback_keeps_rejected_direct_records(session: Session):
    dish = add_food(session, "recipe-with-volume", "Dish")
    ingredient = add_food(session, "valid-ingredient", "Ingredient")
    add_measurement(session, dish, "protein", "Protein", 8.0, "g", basis="source_100ml")
    add_measurement(session, ingredient, "protein", "Protein", 2.0, "g")
    recipe = Recipe(food_id=dish.id, source_ref="recipe-verified", yield_weight_g=100.0)
    session.add(recipe)
    session.flush()
    session.add(RecipeIngredient(recipe_id=recipe.id, food_id=ingredient.id,
                                 amount_g=100.0, retention_factor=1.0))
    session.flush()
    result = analyze_meal(session, [{"food_id": dish.id, "amount_g": 100.0}])
    assert result["items"][0]["calculation_mode"] == "recipe"
    assert result["components"][0]["amount"] == pytest.approx(2.0)
    assert any("volume_to_mass_conversion_unverified" in row.get("exclusion_reasons", [])
               for row in result["unquantified"])

