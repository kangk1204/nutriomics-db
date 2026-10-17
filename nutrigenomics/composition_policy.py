"""Shared intake eligibility. Preserve source status separately from method and use."""
from __future__ import annotations

import math

POLICY_VERSION = "composition-eligibility-20261017-v1"
MASS_BASES = {"edible_portion_100g", "per_100g_edible_portion", "source_100g"}
UNIT_ALIASES = {"g": "g", "mg": "mg", "ug": "ug", "kcal": "kcal", "μg": "ug",
                "µg": "ug", "mcg": "ug", "μg/100g": "ug", "µg/100g": "ug",
                "mcg/100g": "ug", "ug/100g": "ug", "mg/100g": "mg", "g/100g": "g",
                "kj": "kJ", "kcal/100g": "kcal"}
UNIT_FAMILY = {"g": ("mass", 1.0), "mg": ("mass", .001), "ug": ("mass", .000001),
               "kcal": ("energy", 1.0), "kJ": ("energy", 1 / 4.184)}


def normalize_unit(unit):
    if unit is None:
        return None
    value = str(unit).strip().replace(" ", "")
    return UNIT_ALIASES.get(value.casefold(), value) if value else None


def convert_unit(value, source, target):
    a, b = normalize_unit(source), normalize_unit(target) or normalize_unit(source)
    if a is None or b is None:
        return None
    if a == b:
        return value
    x, y = UNIT_FAMILY.get(a), UNIT_FAMILY.get(b)
    return value * x[1] / y[1] if x and y and x[0] == y[0] else None


def assess(*, status, amount, unit, basis, metadata=None, food_metadata=None, source_id=None):
    """Never relabel derived/imputed/assumed-zero records as measured values.

    Official FNDDS values are published reference estimates. Their individual
    derivation may be unavailable; this is stated, rather than fabricated.
    Other derived records require an explicit verified method or FDC derivation.
    """
    raw, food = metadata or {}, food_metadata or {}
    if not isinstance(raw, dict):
        raw = {}
    context = raw.get("source_metadata") or raw
    if not isinstance(context, dict):
        context = raw
    method, accepted, zero = "unknown", False, "not_zero"
    if status in {"measured", "measured_zero", "quantified"}:
        method, accepted = "measured" if status != "quantified" else "source_quantified", True
        if amount == 0:
            zero = "reported_measured_zero"
    elif status in {"derived", "imputed"}:
        declaration = context.get("derivation_method")
        if context.get("derivation_verified") is True and declaration in {
            "summed", "calculated", "imputed", "assumed_zero", "recipe_estimate"
        }:
            method, accepted = declaration, True
        else:
            data_type = food.get("data_type", food.get("dataType"))
            provenance = str(source_id or "").startswith("USDA-FDC:") or source_id == "R03"
            derivation = context.get("foodNutrientDerivation") or context.get("source_derivation") or {}
            code = str(derivation.get("code", "")).upper()
            description = str(derivation.get("description", "")).casefold()
            if provenance and data_type == "Survey (FNDDS)":
                method, accepted = "published_reference_estimate", True
            elif provenance and data_type in {"Foundation", "SR Legacy"} and code and description:
                method = ("assumed_zero" if "assum" in description and "zero" in description else
                          "summed" if "sum" in description else
                          "imputed" if "imput" in description else
                          "calculated" if "calculat" in description else "source_derived")
                accepted = True
        if amount == 0:
            zero = "assumed_zero" if method == "assumed_zero" else "reported_estimated_zero"
    reasons = []
    if not accepted:
        reasons.append("derivation_unverified" if status in {"derived", "imputed"} else "status_not_quantified")
    if amount is None:
        reasons.append("amount_missing")
    elif not isinstance(amount, (int, float)) or not math.isfinite(amount):
        reasons.append("amount_nonfinite")
    elif amount < 0:
        reasons.append("amount_negative")
    if normalize_unit(unit) is None:
        reasons.append("unit_missing")
    if basis not in MASS_BASES:
        reasons.append("volume_to_mass_conversion_unverified" if basis in {
            "source_100ml", "per_100ml", "100ml"
        } else "basis_unsupported_or_unverified")
    if raw.get("basis_verified") is False or context.get("basis_verified") is False:
        reasons.append("basis_explicitly_unverified")
    return {"policy_version": POLICY_VERSION, "source_status": status,
            "derivation_method": method, "zero_interpretation": zero,
            "eligible": not reasons, "exclusion_reasons": reasons}


def select_unique(records):
    """Select one eligible record per analyte/unit family, never sum replicates.

    Callers provide row/key/assessment/unit/id. Deterministic quality ordering
    avoids choosing a record by its amount. Unconvertible units stay separate.
    """
    rank = {"measured": 0, "source_quantified": 1, "summed": 2, "calculated": 3,
            "published_reference_estimate": 4, "source_derived": 5,
            "recipe_estimate": 6, "imputed": 7, "assumed_zero": 8}
    chosen, excluded = {}, []
    for entry in sorted(records, key=lambda r: (
        rank.get(r["assessment"]["derivation_method"], 99), str(r["id"])
    )):
        if not entry["assessment"]["eligible"]:
            excluded.append((entry, entry["assessment"]["exclusion_reasons"]))
            continue
        unit = normalize_unit(entry["unit"])
        family = UNIT_FAMILY[unit][0] if unit in UNIT_FAMILY else unit
        key = (entry["key"], family)
        if key in chosen:
            excluded.append((entry, ["alternative_measurement_not_summed"]))
        else:
            chosen[key] = entry
    return list(chosen.values()), excluded
