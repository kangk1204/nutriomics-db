from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator


class FoodItemInput(BaseModel):
    food_id: int = Field(gt=0)
    amount_g: Annotated[FiniteFloat, Field(gt=0, le=20_000)]


class ReferenceContext(BaseModel):
    """Requested research context; absent dimensions remain unknown."""

    model_config = ConfigDict(extra="forbid")

    tissue: Annotated[str | None, Field(max_length=160)] = None
    response_time_h: Annotated[FiniteFloat | None, Field(ge=0)] = None
    regimen_duration_days: Annotated[FiniteFloat | None, Field(ge=0)] = None

    @field_validator("tissue")
    @classmethod
    def normalize_tissue(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class MealAnalysisInput(BaseModel):
    items: Annotated[list[FoodItemInput], Field(min_length=1, max_length=50)]
    consumed_at: datetime | None = None
    note: Annotated[str | None, Field(max_length=500)] = None
    reference_context: ReferenceContext | None = None


def exportable_payload(payload: dict | list) -> dict | list:
    """Copy an export, omitting records whose source explicitly forbids export.

    Composite results and ordinary calculation values remain intact. The marker
    belongs on evidence/source records, not on a composite response. Disclosure
    counts prevent a restricted row from disappearing without explanation.
    """
    excluded = object()
    withheld = 0

    def visit(value):
        nonlocal withheld
        if isinstance(value, dict):
            if value.get("export_allowed") is False:
                withheld += 1
                return excluded
            result = {}
            for key, child in value.items():
                exported = visit(child)
                if exported is not excluded:
                    result[key] = exported
            return result
        if isinstance(value, list):
            result = []
            for child in value:
                exported = visit(child)
                if exported is not excluded:
                    result.append(exported)
            return result
        return value

    result = visit(payload)
    if result is excluded:
        return {"export_withheld_records": withheld}
    if isinstance(result, dict):
        result["export_withheld_records"] = withheld
    return result


class MealItemResult(BaseModel):
    food_id: int
    food_name: str
    preparation_state: str | None
    amount_g: float
    calculation_mode: Literal["direct", "recipe", "unavailable"]


class FoodResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: str
    source_code: str
    name: str
    name_en: str | None
    food_group: str | None
    edible_state: str | None
    preparation_state: str | None


class DateRangeInput(BaseModel):
    start: date | None = None
    end: date | None = None

    @field_validator("end")
    @classmethod
    def end_not_before_start(cls, value: date | None, info):  # type: ignore[no-untyped-def]
        start = info.data.get("start")
        if value is not None and start is not None and value < start:
            raise ValueError("end must be the same as or later than start")
        return value

