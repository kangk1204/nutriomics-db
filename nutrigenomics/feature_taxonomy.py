"""Conservative display taxonomy, independent of immutable source effect keys.

Identifier prefixes are observations about identifiers, not chemical/causal proof.
This module performs no database writes and creates no scientific approvals.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

SCHEMA_VERSION = "nutriomics-feature-taxonomy-v1"
KEY_FIELDS = ("series_accession", "feature_type", "feature_id", "contrast", "analysis_version")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CG = re.compile(r"^cg[0-9]+$")
_CH = re.compile(r"^ch\.[0-9XYM]+\.[0-9]+[FR]$")
_REGION = re.compile(r"^chr(?:[1-9]|1[0-9]|2[0-2]):([1-9][0-9]*)-([1-9][0-9]*)$")


@dataclass(frozen=True)
class FeatureClassification:
    source_feature_type: str
    normalized_feature_type: str
    identifier_class: str
    chemical_context: str
    classification_basis: str
    display_label_ko: str
    region_definition: str | None = None
    discovered_or_validated_dmr: bool = False


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be an exact nonempty source string")
    return value


def effect_key(
    record: Mapping[str, Any], *, series_accession: str | None = None,
    analysis_version: str | None = None,
) -> tuple[str, str, str, str, str]:
    """Return the five ORIGINAL source-key parts, rejecting ambiguous aliases."""
    series = record.get("series_accession", record.get("series", series_accession))
    for candidate in (record.get("series_accession"), record.get("series"), series_accession):
        if candidate is not None and candidate != series:
            raise ValueError("Conflicting source series identifiers")
    version = record.get("analysis_version", analysis_version)
    if analysis_version is not None and version != analysis_version:
        raise ValueError("Conflicting source analysis versions")
    return tuple(_text(value, name) for name, value in zip(KEY_FIELDS, (
        series, record.get("feature_type"), record.get("feature_id"),
        record.get("contrast"), version,
    ), strict=True))


def key_sha256(key: tuple[str, str, str, str, str]) -> str:
    return hashlib.sha256(json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def classify_feature(
    source_feature_type: str, feature_id: str, *,
    region_definition: Mapping[str, str] | None = None,
    analysis_version: str | None = None,
) -> FeatureClassification:
    """Classify a source identifier without changing it or asserting CG/CH chemistry.

    A fixed-region label needs BOTH an explicit hash-bound analysis declaration
    and matching fixed-bin coordinates. A name containing 'dmr' is insufficient.
    """
    source_type = _text(source_feature_type, "feature_type")
    feature_id = _text(feature_id, "feature_id")
    if source_type != "dmr" and region_definition is not None:
        raise ValueError("Region metadata cannot be attached to a non-region result")
    if source_type == "cpg":
        prefix = "cg" if _CG.fullmatch(feature_id) else "ch" if _CH.fullmatch(feature_id) else "unrecognized"
        labels = {"cg": "cg 접두사 메틸화 probe", "ch": "ch 접두사 메틸화 probe",
                  "unrecognized": "식별자 유형 미확인 메틸화 probe"}
        return FeatureClassification(source_type, "methylation_probe", prefix, "unconfirmed",
                                     "source_type_and_identifier_pattern_only", labels[prefix])
    if source_type == "dmr":
        definition = "unconfirmed"
        label = "영역 검정 · 경계 정의 미확인"
        if region_definition is not None:
            if (region_definition.get("kind") != "fixed_1000bp"
                    or region_definition.get("analysis_version") != analysis_version
                    or not _SHA256.fullmatch(region_definition.get("source_sha256", ""))):
                raise ValueError("Fixed-region definition is not bound to this source analysis")
            match = _REGION.fullmatch(feature_id)
            if not match or int(match[2]) - int(match[1]) != 999 or (int(match[1]) - 1) % 1000:
                raise ValueError("Source region does not match the declared fixed 1000-bp tiling")
            definition, label = "fixed_1000bp", "사전 지정 1 kb 영역 검정"
        return FeatureClassification(source_type, "tested_region", "unrecognized", "unconfirmed",
                                     "source_region_test_not_dmr_discovery", label, definition)
    # Gene and Reactome/pathway records keep their actual original kind.
    return FeatureClassification(source_type, source_type, "unrecognized", "not_applicable",
                                 "original_non_methylation_type_preserved", source_type)


def annotate_effect_record(
    record: Mapping[str, Any], *, series_accession: str | None = None,
    analysis_version: str | None = None, overlay_release_id: str | None = None,
    region_definition: Mapping[str, str] | None = None,
    annotation_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure API adapter: retain source fields/values; add a separate display layer.

    Optional annotation_identity is an exact-ID source binding, not a chemical
    context claim. Source permissions, statistics, NA/null and review are untouched.
    Calling this twice with identical context is safe; conflicting fields fail.
    """
    key = effect_key(record, series_accession=series_accession, analysis_version=analysis_version)
    classification = classify_feature(key[1], key[2], region_definition=region_definition,
                                      analysis_version=key[4])
    if annotation_identity is not None:
        if annotation_identity.get("feature_id") != key[2]:
            raise ValueError("Annotation identity does not match the original source feature")
        for field, value in (("series_accession", key[0]), ("analysis_version", key[4])):
            if annotation_identity.get(field, value) != value:
                raise ValueError("Annotation identity belongs to another source analysis")
    extra = asdict(classification)
    extra["feature_taxonomy"] = {
        "schema_version": SCHEMA_VERSION, "overlay_release_id": overlay_release_id,
        "source_key": list(key), "source_key_sha256": key_sha256(key),
        "annotation_identity": dict(annotation_identity) if annotation_identity is not None else {
            "status": "not_checked_for_this_record"},
        "review_status": "derived_candidate_not_human_approved",
        "health_or_causal_inference": "not_permitted_by_this_classification",
    }
    result = dict(record)
    for field, value in extra.items():
        if field in result and result[field] != value:
            raise ValueError(f"Refusing to overwrite existing source/derived field: {field}")
        result[field] = value
    if effect_key(result, series_accession=series_accession, analysis_version=analysis_version) != key:
        raise AssertionError("Original five-part effect key changed")
    return result


def source_types_for_normalized_type(normalized_type: str) -> tuple[str, ...]:
    """Translate a DISPLAY filter; existing URLs/cursors still use source types."""
    return {"methylation_probe": ("cpg",), "tested_region": ("dmr",)}.get(
        normalized_type, (_text(normalized_type, "normalized_feature_type"),))
