from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class ParsedSourceValue:
    amount: Decimal | None
    status: str
    raw_value: str | None


# Crosswalk to USDA FoodData Central nutrient IDs allows source-specific measurements
# to share nutrient concepts while preserving the K-FIND source and original field.
NUTRIENT_CROSSWALK: dict[str, dict[str, str]] = {
    "ENERC": {"fdc_id": "1008", "name": "에너지", "name_en": "Energy", "unit": "kcal"},
    "WATER": {"fdc_id": "1051", "name": "수분", "name_en": "Water", "unit": "g"},
    "PROT": {"fdc_id": "1003", "name": "단백질", "name_en": "Protein", "unit": "g"},
    "FATCE": {"fdc_id": "1004", "name": "총 지방", "name_en": "Total lipid (fat)", "unit": "g"},
    "ASH": {"fdc_id": "1007", "name": "회분", "name_en": "Ash", "unit": "g"},
    "CHOCDF": {
        "fdc_id": "1005",
        "name": "탄수화물",
        "name_en": "Carbohydrate, by difference",
        "unit": "g",
    },
    "SUGAR": {"fdc_id": "2000", "name": "당류", "name_en": "Total sugars", "unit": "g"},
    "FIBTG": {
        "fdc_id": "1079",
        "name": "식이섬유",
        "name_en": "Fiber, total dietary",
        "unit": "g",
    },
    "CA": {"fdc_id": "1087", "name": "칼슘", "name_en": "Calcium", "unit": "mg"},
    "FE": {"fdc_id": "1089", "name": "철", "name_en": "Iron", "unit": "mg"},
    "P": {"fdc_id": "1091", "name": "인", "name_en": "Phosphorus", "unit": "mg"},
    "K": {"fdc_id": "1092", "name": "칼륨", "name_en": "Potassium", "unit": "mg"},
    "NAT": {"fdc_id": "1093", "name": "나트륨", "name_en": "Sodium", "unit": "mg"},
    "VITA_RAE": {"fdc_id": "1106", "name": "비타민 A (RAE)", "name_en": "Vitamin A, RAE", "unit": "ug"},
    "RETOL": {"fdc_id": "1105", "name": "레티놀", "name_en": "Retinol", "unit": "ug"},
    "CARTB": {"fdc_id": "1107", "name": "베타카로틴", "name_en": "Carotene, beta", "unit": "ug"},
    "THIA": {"fdc_id": "1165", "name": "티아민", "name_en": "Thiamin", "unit": "mg"},
    "RIBF": {"fdc_id": "1166", "name": "리보플라빈", "name_en": "Riboflavin", "unit": "mg"},
    "NIA": {"fdc_id": "1167", "name": "나이아신", "name_en": "Niacin", "unit": "mg"},
    "VITC": {
        "fdc_id": "1162",
        "name": "비타민 C",
        "name_en": "Vitamin C, total ascorbic acid",
        "unit": "mg",
    },
    "VITD": {"fdc_id": "1114", "name": "비타민 D", "name_en": "Vitamin D (D2 + D3)", "unit": "ug"},
    "CHOLE": {"fdc_id": "1253", "name": "콜레스테롤", "name_en": "Cholesterol", "unit": "mg"},
    "FASAT": {
        "fdc_id": "1258",
        "name": "포화지방산",
        "name_en": "Fatty acids, total saturated",
        "unit": "g",
    },
    "FATRN": {
        "fdc_id": "1257",
        "name": "트랜스지방산",
        "name_en": "Fatty acids, total trans",
        "unit": "g",
    },
}

_BELOW_DETECTION = {"tr", "trace", "미량", "불검출", "미검출", "<lod", "<loq", "nd"}
_NOT_MEASURED = {"na", "n/a", "미측정", "미분석", "해당없음", "-", ""}
_LESS_THAN_VALUE = re.compile(r"^\s*<\s*([0-9]+(?:\.[0-9]+)?)\s*$")


def parse_source_value(value: Any) -> ParsedSourceValue:
    """Parse a nutrient value without collapsing blanks, traces, or true zeroes."""
    if value is None:
        return ParsedSourceValue(None, "not_measured", None)
    raw = str(value)
    token = raw.strip()
    folded = token.casefold()
    if folded in _NOT_MEASURED:
        return ParsedSourceValue(None, "not_measured", raw)
    if folded in _BELOW_DETECTION or _LESS_THAN_VALUE.fullmatch(token):
        return ParsedSourceValue(None, "below_detection", raw)
    try:
        amount = Decimal(token.replace(",", ""))
    except InvalidOperation:
        return ParsedSourceValue(None, "unparsed_source_value", raw)
    if not amount.is_finite():
        return ParsedSourceValue(None, "unparsed_source_value", raw)
    if amount < 0:
        return ParsedSourceValue(None, "source_conflict", raw)
    status = "measured_zero" if amount == 0 else "measured"
    return ParsedSourceValue(amount, status, raw)


def parse_100g_basis(value: Any) -> str | None:
    """Return a normalized mass/volume basis; volume records cannot be scaled by grams."""
    token = str(value or "").strip().casefold().replace(" ", "")
    if token in {"100g", "100.0g"}:
        return "source_100g"
    if token in {"100ml", "100.0ml"}:
        return "source_100ml"
    return None


def food_identity(dataset_id: str, food_code: str, food_name: str) -> tuple[str, str]:
    """Create stable local keys while disambiguating repeated provider food codes."""
    normalized_name = unicodedata.normalize("NFKC", food_name).strip()
    if not dataset_id or not food_code.strip() or not normalized_name:
        raise ValueError("dataset ID, food code, and food name are required")
    suffix = hashlib.sha256(normalized_name.encode("utf-8")).hexdigest()[:12]
    key = f"K-FIND:{dataset_id}:{food_code}:{suffix}"
    return key, key


def validate_selection(selection: dict[str, Any]) -> list[dict[str, Any]]:
    foods = selection.get("foods")
    if not isinstance(foods, list):
        raise TypeError("Selection foods must be a list")
    if not foods:
        raise ValueError("Selection must include a non-empty foods list")
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(foods):
        if not isinstance(item, dict):
            raise TypeError(f"Selection item {index} must be an object")
        required = (
            "dataset_id",
            "food_code",
            "food_name",
            "name",
            "name_en",
            "food_group",
            "preparation_state",
        )
        missing = [key for key in required if not str(item.get(key, "")).strip()]
        if missing:
            raise ValueError(f"Selection item {index} is missing: {', '.join(missing)}")
        key = (
            str(item["dataset_id"]),
            str(item["food_code"]),
            str(item["food_name"]),
        )
        if key in seen:
            raise ValueError(f"Duplicate selected food identity: {key}")
        seen.add(key)
        result.append(item)
    return result
