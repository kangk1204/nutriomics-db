"""Submission evidence contracts. Hash integrity alone does not prove a check passed."""
from __future__ import annotations

import math
import re
from datetime import datetime

REQUIRED_TECHNICAL = ("food50_composition1200", "effect_preservation", "provenance",
                      "interpretation_boundaries", "warm100", "concurrent4", "browser80",
                      "offline_html", "exports", "clean_reproduction", "rollback")


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def valid_receipt(report, name, release_id, database_sha256, effect_count, code_sha256=None):
    if not isinstance(report, dict) or not re.fullmatch(r"[a-f0-9]{64}", database_sha256 or ""):
        return False
    if any(report.get(k) != v for k, v in {
        "schema_version": 1, "check_type": name, "release_id": release_id,
        "database_sha256": database_sha256, "passed": True,
    }.items()):
        return False
    if not report.get("verified_at") or not re.fullmatch(r"[a-f0-9]{64}", report.get("code_sha256", "")):
        return False
    if code_sha256 is None or report["code_sha256"] != code_sha256:
        return False
    try:
        if datetime.fromisoformat(report["verified_at"]).tzinfo is None:
            return False
    except (ValueError, TypeError):
        return False
    m = report.get("metrics", {})
    if not isinstance(m, dict):
        return False
    zero = lambda k: type(m.get(k)) is int and m[k] == 0
    count = lambda k, n: type(m.get(k)) is int and m[k] >= n
    bounded = lambda v, limit: finite(v) and 0 <= v <= limit
    if name == "food50_composition1200":
        return all(count(k, n) for k, n in {"internal_foods": 50, "internal_compositions": 1200,
                                           "public_foods": 50, "public_compositions": 1200}.items()) and zero("mismatches")
    if name == "effect_preservation":
        return type(m.get("checked_rows")) is int and m["checked_rows"] == effect_count and zero("mismatches") and m.get("source_compared") is True
    if name == "provenance":
        return count("checked_records", 1) and zero("missing_required_references") and zero("invalid_references")
    if name == "interpretation_boundaries":
        required = {"animal_to_human", "cell_to_intake", "acute_to_chronic", "mixture_to_compound",
                    "association_to_causality", "drug_to_food", "missing_to_zero", "version_double_count"}
        return required <= set(m.get("tested_categories", [])) and count("cases", len(required)) and zero("failures")
    if name in {"warm100", "concurrent4"}:
        rows = m.get("cases", [])
        if not rows or not re.fullmatch(r"[a-f0-9]{64}", report.get("case_sha256", "")):
            return False
        if name == "concurrent4" and m.get("concurrency") != 4:
            return False
        for row in rows:
            samples = row.get("samples", [])
            if len(samples) != 100 or row.get("kind") not in {"general", "maximum_graph"}:
                return False
            if any(s.get("status") != 200 or s.get("release_id") != release_id or s.get("error")
                   or not finite(s.get("seconds")) or s["seconds"] < 0 for s in samples):
                return False
            limit = (3 if row["kind"] == "maximum_graph" else 1) if name == "warm100" else (5 if row["kind"] == "maximum_graph" else 2)
            if sorted(s["seconds"] for s in samples)[94] > limit:
                return False
        return len({r.get("id") for r in rows}) == len(rows) and m.get("all_frozen_cases_present") is True
    if name == "browser80":
        return (m.get("actual_browser") is True and m.get("nodes") == 80
                and bounded(m.get("data_to_frame_ms"), 2000)
                and bool(re.fullmatch(r"[a-f0-9]{64}", m.get("screenshot_sha256", ""))))
    if name == "offline_html":
        return m.get("actual_browser") is True and m.get("network_blocked") is True and zero("external_requests") and all(m.get(k) is True for k in ("search", "selection", "zoom", "evidence"))
    if name == "exports":
        formats = m.get("formats", {})
        return (m.get("maximum_selection_verified") is True and zero("validation_errors")
                and set(formats) == {"html", "csv", "json"}
                and all(r.get("status") == 200 and bounded(r.get("seconds"), 10) for r in formats.values()))
    if name == "clean_reproduction":
        runs = m.get("analyses", [])
        return (m.get("public_files_only_service_build") is True and len(runs) == 14
                and len({(r.get("series_accession"), r.get("analysis_version")) for r in runs}) == 14
                and all(r.get("executed") is True and r.get("comparison_passed") is True
                        and r.get("unexplained_differences") == 0 for r in runs))
    if name == "rollback":
        return all(m.get(k) is True for k in ("activated", "restored", "restored_hash_matches", "api_verified"))
    return False
