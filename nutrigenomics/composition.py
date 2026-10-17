from __future__ import annotations

import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .composition_policy import assess, convert_unit, normalize_unit, select_unique
from .models import (
    CompositionMeasurement,
    Food,
    FoodCompoundMeasurement,
    Recipe,
    RecipeIngredient,
)

Measurement = CompositionMeasurement | FoodCompoundMeasurement


def _assessment(row: Measurement) -> dict:
    return assess(status=row.status, amount=row.amount_per_100g, unit=row.unit,
                  basis=row.basis, metadata=row.metadata_json,
                  food_metadata=row.food.metadata_json if row.food else {}, source_id=row.source_id)


def _exclusion_reasons(row: Measurement) -> list[str]:
    return _assessment(row)["exclusion_reasons"]


def _active_rows(food: Food) -> list[Measurement]:
    records: list[Measurement] = [
        row for row in food.compositions
        if food.active_release is None or row.release == food.active_release
    ]
    records.extend(
        row for row in food.compound_measurements
        if row.resource.release is None or row.release == row.resource.release
    )
    return records


def _select_rows(food):
    return select_unique([{"row": row, "key": row.component_id, "id": row.source_ref,
                           "unit": row.unit, "assessment": _assessment(row)}
                          for row in _active_rows(food)])


def _numeric_records(food: Food) -> list[CompositionMeasurement]:
    return [e["row"] for e in _select_rows(food)[0]
            if isinstance(e["row"], CompositionMeasurement)]


def _numeric_compound_records(food: Food) -> list[FoodCompoundMeasurement]:
    return [e["row"] for e in _select_rows(food)[0]
            if isinstance(e["row"], FoodCompoundMeasurement)]


def _append_scaled(
    totals: dict[tuple[int, str], dict[str, Any]],
    row: Measurement,
    amount_g: float,
    factor: float = 1.0,
) -> None:
    component = row.component
    source_unit = normalize_unit(row.unit)
    target_unit = normalize_unit(component.canonical_unit) or source_unit
    if source_unit is None or target_unit is None:
        return
    converted = convert_unit(row.amount_per_100g or 0.0, source_unit, target_unit)
    if converted is None:
        # Keep an unconvertible source unit as a distinct quantity; never add unlike units.
        target_unit = source_unit
        converted = row.amount_per_100g or 0.0
    key = (component.id, target_unit)
    item = totals.setdefault(
        key,
        {
            "component_id": component.id,
            "component_key": component.component_key,
            "name": component.name,
            "name_en": component.name_en,
            "category": component.category,
            "amount": 0.0,
            "unit": target_unit,
            "source_records": [],
        },
    )
    item["amount"] += converted * amount_g / 100.0 * factor
    source_record = {
        "source_id": row.source_id,
        "source_ref": row.source_ref,
        "release": row.release,
        "basis": row.basis,
        "composition_assessment": _assessment(row),
    }
    if row.minimum is not None:
        source_record["source_minimum_per_100g"] = row.minimum
    if row.maximum is not None:
        source_record["source_maximum_per_100g"] = row.maximum
    if row.metadata_json:
        source_record["source_metadata"] = row.metadata_json
    if isinstance(row, FoodCompoundMeasurement):
        source_record["measurement_kind"] = "source-reported food compound concentration"
    if source_record not in item["source_records"]:
        item["source_records"].append(source_record)


def _unknown_rows(food: Food, factor: float = 1.0) -> list[dict[str, Any]]:
    records = []
    for entry, reasons in _select_rows(food)[1]:
        row = entry["row"]
        value = row.amount_per_100g
        records.append({
            "component_id": row.component_id,
            "component_key": row.component.component_key,
            "name": row.component.name,
            "status": row.status,
            "unit": row.unit,
            "basis": row.basis,
            "release": row.release,
            "source_id": row.source_id,
            "source_ref": row.source_ref,
            "scaled_by": factor,
            "exclusion_reasons": reasons,
            "source_amount": value if value is not None and math.isfinite(value) else None,
            "source_value_text": str(value) if value is not None else None,
            "source_minimum": row.minimum,
            "source_maximum": row.maximum,
            "source_metadata": row.metadata_json or {},
        })
    return records


def _load_foods(session: Session, food_ids: set[int]) -> dict[int, Food]:
    stmt = (
        select(Food)
        .where(Food.id.in_(food_ids))
        .options(
            selectinload(Food.compositions).selectinload(CompositionMeasurement.component),
            selectinload(Food.compound_measurements).selectinload(
                FoodCompoundMeasurement.component
            ),
            selectinload(Food.compound_measurements).selectinload(
                FoodCompoundMeasurement.resource
            ),
            selectinload(Food.recipes)
            .selectinload(Recipe.ingredients)
            .selectinload(RecipeIngredient.food)
            .selectinload(Food.compositions)
            .selectinload(CompositionMeasurement.component),
            selectinload(Food.recipes)
            .selectinload(Recipe.ingredients)
            .selectinload(RecipeIngredient.food)
            .selectinload(Food.compound_measurements)
            .selectinload(FoodCompoundMeasurement.component),
            selectinload(Food.recipes)
            .selectinload(Recipe.ingredients)
            .selectinload(RecipeIngredient.food)
            .selectinload(Food.compound_measurements)
            .selectinload(FoodCompoundMeasurement.resource),
        )
    )
    return {food.id: food for food in session.scalars(stmt).unique().all()}


def analyze_meal(session: Session, items: list[dict[str, float | int]]) -> dict[str, Any]:
    """Calculate eligible source quantities; keep estimation methods visible."""
    ids = {int(item["food_id"]) for item in items}
    foods = _load_foods(session, ids)
    missing_ids = ids - foods.keys()
    if missing_ids:
        raise ValueError(f"Unknown food_id values: {sorted(missing_ids)}")

    totals: dict[tuple[int, str], dict[str, Any]] = {}
    item_results: list[dict[str, Any]] = []
    unquantified: list[dict[str, Any]] = []
    used_sources: set[str] = set()

    for request_item in items:
        food = foods[int(request_item["food_id"])]
        amount_g = float(request_item["amount_g"])
        if not math.isfinite(amount_g) or amount_g <= 0:
            raise ValueError("amount_g must be finite and positive")
        direct = _numeric_records(food)
        direct_compounds = _numeric_compound_records(food)
        item_unknown: list[dict[str, Any]] = _unknown_rows(food)
        mode = "direct"
        if direct or direct_compounds:
            for row in direct:
                _append_scaled(totals, row, amount_g)
                used_sources.add(row.source_id)
            for row in direct_compounds:
                _append_scaled(totals, row, amount_g)
                used_sources.add(row.source_id)
        else:
            recipes = sorted(food.recipes, key=lambda recipe: recipe.id)
            if recipes and math.isfinite(recipes[0].yield_weight_g) and recipes[0].yield_weight_g > 0:
                recipe = recipes[0]
                mode = "recipe"
                recipe_factor = amount_g / recipe.yield_weight_g
                for ingredient in recipe.ingredients:
                    ingredient = session.scalar(
                        select(RecipeIngredient)
                        .where(RecipeIngredient.id == ingredient.id)
                        .options(
                            selectinload(RecipeIngredient.food)
                            .selectinload(Food.compositions)
                            .selectinload(CompositionMeasurement.component)
                        )
                    ) or ingredient
                    component_food = ingredient.food
                    ingredient_amount = ingredient.amount_g * recipe_factor
                    records = _numeric_records(component_food)
                    compound_records = _numeric_compound_records(component_food)
                    for row in [*records, *compound_records]:
                        _append_scaled(
                            totals,
                            row,
                            ingredient_amount,
                            factor=ingredient.retention_factor,
                        )
                        used_sources.add(row.source_id)
                    if not records and not compound_records:
                        item_unknown.append(
                            {
                                "name": component_food.name,
                                "status": "ingredient_composition_missing",
                                "exclusion_reasons": ["ingredient_composition_missing"],
                                "source_id": component_food.source_id,
                                "source_ref": component_food.source_code,
                            }
                        )
                    item_unknown.extend(_unknown_rows(component_food, ingredient_amount / amount_g))
            else:
                mode = "unavailable"
                item_unknown.append(
                    {
                        "name": food.name,
                        "status": "composition_not_available",
                        "exclusion_reasons": [
                            "recipe_yield_invalid" if recipes else "composition_not_available"
                        ],
                        "source_id": food.source_id,
                        "source_ref": food.source_code,
                    }
                )

        for row in item_unknown:
            row["food_id"] = food.id
            row["food_name"] = food.name
        unquantified.extend(item_unknown)
        item_results.append(
            {
                "food_id": food.id,
                "food_name": food.name,
                "preparation_state": food.preparation_state,
                "amount_g": amount_g,
                "calculation_mode": mode,
            }
        )

    components = []
    for row in totals.values():
        row["amount"] = round(row["amount"], 9)
        components.append(row)
    components.sort(key=lambda row: (row["category"], row["name"].casefold(), row["unit"]))
    interpretation = (
        "계산된 값은 음식 섭취량에서 산출한 성분량입니다. 혈중 농도나 개인별 오믹스 반응을 뜻하지 않습니다."
    )
    if "R05" in used_sources:
        interpretation += (
            " FooDB orig_content는 문헌값 또는 출처에서 보고한 범위의 평균일 수 있어, "
            "원래 단위·범위·인용 정보를 출처 기록에서 확인하세요."
        )
    return {
        "status": "measured" if components and not unquantified else "partial" if components else "unavailable",
        "calculation_status": "available" if components and not unquantified else "partial" if components else "unavailable",
        "source_methods": sorted({s["composition_assessment"]["derivation_method"]
                                  for c in components for s in c["source_records"]
                                  if "composition_assessment" in s}),
        "items": item_results,
        "components": components,
        "unquantified": unquantified,
        "composition_source_ids": sorted(used_sources),
        "interpretation": interpretation,
    }

