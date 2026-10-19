"""Conservative scientific matching; identity, evidence and context stay separate.

No scores, causal promotion, imputation of missing effects, or dose-effect scaling.
The projection stores metadata only. Numerical effects remain in their source DB.
"""

from __future__ import annotations

import json
import re
import sqlite3
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlencode

from .effect_batch import read_rows_by_ids, verify_reference
from .explorer_performance import (
    CACHE,
    CURRENT,
    QueryUnavailable,
    canonical,
    fingerprint,
    has_index,
    indexed_source,
    logical_connection_path,
    read_connection,
    selector_key,
)

RULE_VERSION = "scientific-matching-2026-10-03-v1"
DIMENSIONS = ("tissue", "response_time_h", "regimen_duration_days", "frequency", "population")
TISSUES = {
    "pbmc": "pbmc",
    "peripheral blood mononuclear cells": "pbmc",
    "peripheral blood mononuclear cell": "pbmc",
    "whole blood": "whole_blood",
    "whole blood cells": "whole_blood",
    "전혈": "whole_blood",
}
FREQUENCIES = {"single_meal": "single", "single": "single", "per_day": "daily", "daily": "daily"}
LABELS = {
    "same_record": "동일한 실험 노출 기록",
    "same_analyte": "동일 조성 항목 대응",
    "related_food": "관련 식품군 · 제품 동일성 미확인",
    "related_component": "공유 성분 · 독립 효과 미확인",
    "candidate_label": "이름 대응 후보",
    "unknown": "대응 미확인",
}


def decoded(value, default=None):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value is not None else ({} if default is None else default)
    except (ValueError, TypeError):
        return {} if default is None else default


def _norm(value):
    return re.sub(r"[\s_-]+", " ", str(value).strip().casefold()) if value is not None else None


def _condition(dimension, given, observed, status, reason):
    return {
        "dimension": dimension,
        "status": status,
        "input": given,
        "study": observed,
        "reason": reason,
    }


def compare_context(reference: dict, study_context: dict) -> list[dict]:
    output = []
    for dimension in DIMENSIONS:
        given, observed = reference.get(dimension), study_context.get(dimension)
        status, reason = "unknown", "입력 또는 연구 조건이 미확인입니다."
        if given is not None and observed is not None:
            if dimension == "tissue":
                a, b = TISSUES.get(_norm(given)), TISSUES.get(_norm(observed))
                if a and b:
                    status = "match" if a == b else "mismatch"
                    reason = (
                        "같은 시료 유형입니다."
                        if a == b
                        else "전혈과 PBMC 등 서로 다른 시료 유형입니다."
                    )
                elif _norm(given) == _norm(observed) and _norm(given) not in {"blood", "혈액"}:
                    status, reason = (
                        "match",
                        "기록된 조직 명칭이 일치합니다; 세포 구성 일치는 별도 확인이 필요합니다.",
                    )
                else:
                    reason = "조직·세포 유형을 판정할 수 있는 정보가 부족합니다."
            elif dimension == "frequency":
                a, b = FREQUENCIES.get(given), FREQUENCIES.get(observed)
                if a and b:
                    status = "match" if a == b else "mismatch"
                    reason = "섭취 빈도 기록 비교입니다; 실제 순응도는 확인하지 않습니다."
            elif dimension in {"response_time_h", "regimen_duration_days"}:
                status = "match" if given == observed else "mismatch"
                reason = "기록된 시간·기간의 정확한 비교이며 생물학적 동등성 판정이 아닙니다."
            elif dimension == "population":
                if _norm(given) == _norm(observed):
                    status, reason = (
                        "match",
                        "대상군 기술이 일치합니다; 개인에게의 적용은 검증되지 않았습니다.",
                    )
                else:
                    reason = "자유 서술 대상군은 자동으로 동등하거나 다르다고 판정하지 않습니다."
        output.append(_condition(dimension, given, observed, status, reason))
    return output


def _component_key(identifier):
    if str(identifier).startswith("FDCNutrient:"):
        return "FDC-NUTRIENT:" + str(identifier).split(":", 1)[1]
    return str(identifier)


def _food_categories(seed):
    # Related category retrieval only; never infer a formulation or exact identity.
    text = " ".join(
        str(x) for x in [seed.get("label", ""), seed.get("label_en", ""), *seed.get("aliases", [])]
    ).casefold()
    patterns = {
        "yogurt": ("yogurt", "yoghurt", "요구르트", "요거트"),
        "acidified_milk": ("acidified milk", "산성화 우유"),
        "walnut": ("walnut", "호두"),
        "olive_oil": ("olive oil", "올리브유", "올리브 오일"),
        "fish_oil": ("fish oil", "어유"),
    }
    return [code for code, terms in patterns.items() if any(t in text for t in terms)]


def exposure_match_identity(seed: dict, exposure: dict, component_ids: list[str]) -> dict:
    status, method = "unknown", "no_verified_correspondence"
    metadata = seed.get("metadata", {})
    core_id = exposure.get("id", exposure.get("core_exposure_id"))
    if core_id and seed.get("id") == "Exposure:" + core_id:
        status, method = "same_record", "original_exposure_identifier"
    elif seed.get("kind") == "food":
        cats = set(_food_categories(seed))
        known = set(exposure.get("food_category_codes", []))
        terms = decoded(exposure.get("food_terms"), [])
        label = " ".join(
            [seed.get("label", ""), seed.get("label_en", ""), *seed.get("aliases", [])]
        ).casefold()
        if cats & known:
            status, method = "related_food", "food_category_correspondence"
        elif any(len(str(t)) >= 3 and str(t).casefold() in label for t in terms):
            status, method = "related_food", "food_term_correspondence"
    else:
        key = metadata.get("component_key", _component_key(seed.get("id")))
        keys = [_component_key(k) for k in component_ids]
        if key in keys:
            if exposure.get("kind") in {"compound", "component", "nutrient"} and len(keys) == 1:
                status, method = "same_analyte", "original_composition_identifier"
            else:
                status, method = "related_component", "component_of_multicomponent_intervention"
    # Food contains an analyte in a mixed intervention: not a matched product.
    if status == "unknown" and seed.get("kind") == "food":
        present = {_component_key(x) for x in metadata.get("available_component_ids", [])}
        if present & {_component_key(x) for x in component_ids}:
            status, method = "related_component", "shared_measured_component"
    return {
        "status": status,
        "label": "음식명·원료 관련성 · 제품 동일성 미확인"
        if method == "food_term_correspondence"
        else LABELS[status],
        "method": method,
        "quantity_transfer_allowed": status == "same_analyte"
        and metadata.get("analyte_basis_identity_verified") is True,
        "single_molecule_identity_verified": metadata.get("single_molecule_identity_verified")
        is True
        and status == "same_analyte",
        "note": "원본 기록·분석 항목의 대응이며 개인 반응 또는 독립 성분 효과를 증명하지 않습니다.",
    }


def gene_feature_identifiers(node: dict) -> list[str]:
    """Namespace-aware candidates; HGNC numeric suffixes are not Entrez IDs."""
    identifier = str(node["id"])
    values = [identifier, node.get("label")]
    namespace, _, suffix = identifier.partition(":")
    if namespace.lower() in {"ncbigene", "entrez", "ensembl", "hgncsymbol"}:
        values.append(suffix)
    values.extend(node.get("metadata", {}).get("verified_feature_ids", []))
    return list(dict.fromkeys(str(value) for value in values if value))[:100]


def feature_match_identity(node: dict, effect: dict) -> dict:
    """A label or untyped legacy verified_feature_ids never proves gene identity."""
    for proof in node.get("metadata", {}).get("feature_identifier_mappings", []):
        taxa = [proof.get(k) for k in ("taxon", "source_taxon", "target_taxon")]
        taxa += [
            node.get("taxon"),
            node.get("metadata", {}).get("taxon"),
            effect.get("taxon"),
            effect.get("metadata", {}).get("taxon"),
        ]
        if any(str(t) not in {"9606", "Homo sapiens"} for t in taxa if t is not None):
            continue
        if (
            proof.get("feature_id") == effect.get("feature_id")
            and proof.get("series_accession") == effect.get("series_accession")
            and proof.get("feature_type") == effect.get("feature_type")
            and proof.get("target_id", proof.get("target_node_id")) == node.get("id")
            and str(proof.get("taxon", proof.get("source_taxon", ""))) == "9606"
            and str(proof.get("target_taxon", proof.get("taxon", ""))) == "9606"
            and proof.get("source_id")
            and proof.get("source_version")
            and proof.get("namespace")
            and proof.get("verified") is True
        ):
            return {
                "status": "source_identifier_match",
                "label": "출처 식별자 대응",
                "proof": proof,
                "note": "플랫폼·원본 식별자 대응입니다; 발현 조절의 인과성은 별도입니다.",
            }
    return {
        "status": "candidate_label",
        "label": "이름·식별자 문자열 대응 후보",
        "note": "유전자 명칭 또는 미검증 식별자 문자열 조회입니다. 유전자 동일성 검증이 필요합니다.",
    }


def contrast_classification(contrast: dict) -> dict:
    name = contrast.get("contrast", "").casefold()
    series = contrast.get("series_accession", "")
    original = contrast.get("design", "unknown")
    if contrast.get("comparison_kind") == "within_arm_pre_post" or (
        series == "GSE107205" and name.endswith("_vs_baseline")
    ):
        classification, label = "within_arm_prepost", "단일 조건 내 전후 변화 · 대조군 간 효과 아님"
    elif series == "GSE127530":
        classification, label = (
            "pooled_meal_response",
            "방문을 합친 식후 변화 · 보충제별 효과 미분리",
        )
    elif (
        series in {"GSE54325", "GSE54643", "GSE219217"}
        or "prepost" in original
        or (series == "GSE56960" and name.startswith("post_"))
    ):
        classification, label = "within_arm_prepost", "단일 조건 내 전후 변화 · 대조군 간 효과 아님"
    elif "change" in name and ("_vs_" in name or "vs500" in name):
        classification, label = "between_condition_change", "조건 간 전후 변화 차이"
    elif series == "GSE27385" and "fish" in name and "placebo" in name:
        classification, label = "crossover_end_period", "개인 내 fish oil 대 placebo 기간말 비교"
    else:
        classification, label = "source_comparison", "원본 대비 · 설계 해석 확인 필요"
    return {
        "classification": classification,
        "label": label,
        "original_design": original,
        "expert_review_status": "pending",
        "component_causal_effect": False,
    }


def edge_scientific(edge: dict, subject=None, target=None) -> dict:
    context = edge.get("context", {}) or {}
    original = context.get("original_assertion", {})
    if isinstance(original, dict):
        nested = decoded(
            original.get("context", original.get("context_json", original.get("metadata_json")))
        )
        context = {**original, **nested, **context}
    relation, tier = edge.get("relation", ""), edge.get("tier", "unknown")
    if "correspondence" in relation or "cross_reference" in relation:
        identity = {
            "status": "candidate_mapping",
            "method": relation,
            "quantity_transfer_allowed": False,
        }
    elif tier in {"observed_composition", "composition"} or relation == "contains":
        identity = {
            "status": "source_composition_record",
            "method": relation,
            "quantity_transfer_allowed": False,
        }
    else:
        identity = {
            "status": "source_relationship",
            "method": relation,
            "quantity_transfer_allowed": False,
        }
    identity["label"] = {
        "candidate_mapping": "식별자·이름 대응 후보",
        "source_composition_record": "출처의 음식–조성 기록",
        "source_relationship": "출처 관계 · 분자 동일성 별도 확인",
    }[identity["status"]]
    contrast = context.get("contrast", {})
    if isinstance(contrast, str):
        contrast = {"contrast": contrast, "design": context.get("design", "unknown")}
    evidence = (
        contrast_classification(contrast)
        if contrast
        else {
            "classification": "automated_candidate"
            if tier == "extracted"
            else (
                "curated_mechanistic_relation"
                if tier in {"curated", "curated_mechanism"}
                else "source_relationship"
            ),
            "label": "관계 기록 · 사람 중재 여부 별도 확인",
            "expert_review_status": context.get("expert_review_status", "pending"),
            "component_causal_effect": False,
        }
    )
    evidence["source_review_status"] = edge.get("review_status", "unknown")
    if tier == 'predicted':
        evidence.update(classification='computational_prediction',
            label='계산 예측 · 식이 반응·임상 효과·실험 검증 아님',experimental_validation=False)
    elif tier == 'extracted':
        evidence.update(classification='automated_candidate',
            label='자동 문헌 추출 후보 · 의미·연구 조건 전문가 검수 대기',experimental_validation=None)
    if tier in {"human_randomized", "human_nonrandomized", "human_observational"}:
        evidence.update(
            classification="human_source_assertion",
            label="사람 연구의 출처 관계 · 원본 대비와 전체 식단 조건 확인",
        )
    conditions = {
        k: context.get(k)
        for k in ("tissue", "dose", "duration", "response_time_h", "study_id", "experiment_type")
    }
    conditions["taxon"] = edge.get("taxon")
    return {
        "mapping_identity": identity,
        "study_evidence": evidence,
        "conditions": conditions,
        "limitations": ["실선·출처 검수·사람 종 표시는 인과성 또는 임상 효능의 증거가 아닙니다."],
    }


def path_scientific(nodes, edges, node_ids, edge_ids) -> dict:
    by_id = {e["id"]: e for e in edges}
    constituents = [
        {"edge_id": identifier, **edge_scientific(by_id[identifier])}
        for identifier in edge_ids
        if identifier in by_id
    ]
    compatibility = []
    for dimension in (
        "taxon",
        "tissue",
        "dose",
        "duration",
        "response_time_h",
        "study_id",
        "experiment_type",
    ):
        values = [c["conditions"].get(dimension) for c in constituents]
        known = [v for v in values if v is not None]
        canonical = [
            TISSUES.get(_norm(v), _norm(v)) if dimension == "tissue" else str(v) for v in known
        ]
        status = "mismatch" if len(set(canonical)) > 1 else "unknown"
        if values and len(known) == len(values) and len(set(canonical)) == 1:
            status = "match"
        compatibility.append(
            {
                "dimension": dimension,
                "status": status,
                "values": values,
                "reason": "연결별 조건의 비교입니다; 일치해도 전체 경로의 사람 효과는 검증되지 않았습니다.",
            }
        )
    return {
        "classification": "composed_hypothesis",
        "causal_inference": False,
        "constituents": constituents,
        "compatibility": compatibility,
        "limitations": [
            "서로 다른 연구의 관계를 조합한 가설입니다.",
            "가장 가까운 유전자 주석·eQTM·표적 결합·발현 변화는 서로 다른 관계입니다.",
        ],
    }


def science_records(db, kind):
    budget = CURRENT.get()
    memo_key = ("science_records", logical_connection_path(db), kind)
    if budget and memo_key in budget.memo:
        return deepcopy(budget.memo[memo_key])
    try:
        rows = [
            decoded(r[0])
            for r in db.execute(
                "SELECT metadata_json FROM science_record WHERE kind=? ORDER BY id", (kind,)
            )
        ]
        if budget:
            budget.memo[memo_key] = rows
        return deepcopy(rows)
    except sqlite3.OperationalError as exc:
        if "no such table: science_record" in str(exc):
            return []
        raise QueryUnavailable("query_timeout" if budget and budget.expired() else "source_unavailable") from exc


def _merge_sample_scopes(sources):
    """Keep distinct count scopes; conflicting counts remain unresolved."""
    values = {}
    for source in sources:
        for key, value in source["values"].items():
            if value is not None and value not in values.setdefault(key, []):
                values[key].append(value)
            else:
                values.setdefault(key, [])
    conflicts = {key: entries for key, entries in values.items() if len(entries) > 1}
    return {
        "sample_scope": {
            key: entries[0] if len(entries) == 1 else None for key, entries in values.items()
        },
        "sample_scope_conflicts": conflicts,
        "sample_scope_sources": sources,
    }


def _run_context(runs, contrast):
    rows = [
        r
        for r in runs
        if r.get("series_accession") == contrast.get("series_accession")
        and r.get("analysis_version") == contrast.get("analysis_version")
    ]
    if not rows:
        return {
            "sample_scope": {},
            "multiple_testing": None,
            "note": "동일 분석 버전의 실행 metadata 없음; FDR 검정군·포함 시료 범위 미확인",
        }
    run = rows[0]
    meta = run.get("metadata", {})
    cm = contrast.get("metadata", {})
    return {
        **_merge_sample_scopes(
            [
                {"source": "exact_analysis_run", "values": meta.get("sample_scope", {})},
                {"source": "exact_contrast_record", "values": cm.get("sample_scope", {})},
            ]
        ),
        "multiple_testing": meta.get("multiple_testing"),
        "fdr_scope": meta.get("fdr_scope"),
        "analysis_status": run.get("status"),
        "source_script_sha256": run.get("source_script_sha256"),
        "environment_lock_sha256": run.get("environment_lock_sha256"),
        "version": contrast.get("analysis_version"),
        "expert_review_status": "pending",
        "statistics_note": "p값·대비 내 BH·연구 내 주대비의 BH는 별도의 검정군입니다. 미기록 범위는 추정하지 않습니다.",
    }


def _preview_key(identity, projection_identity, contrast, gene, limit):
    return ("effects", identity, projection_identity,
            canonical({k: contrast.get(k) for k in ("series_accession", "contrast", "analysis_version")}),
            canonical(gene) if gene else None, limit)


def _contrast_effects(core_path, contrast, gene=None, limit=6, projection=None, batch_results=None):
    if not Path(core_path).is_file():
        return [], None, "source_unavailable"
    projection_path = logical_connection_path(projection) if projection else None
    identity = fingerprint(core_path)
    key = _preview_key(identity, fingerprint(projection_path) if projection_path else None, contrast, gene, limit)
    if batch_results is not None and batch_results.get("_batch_sources") != (identity, key[2]):
        return [], None, "source_changed"
    if batch_results is not None and key in batch_results:
        return deepcopy(batch_results[key])
    cached = CACHE.get(key)
    if cached is not None:
        return json.loads(cached)
    result = _cached_effects(core_path, contrast, gene, limit, projection)
    if result[2] == "available" and fingerprint(core_path) == identity:
        CACHE.put(key, canonical(result).encode())
    return result


def _format_effect(row, gene):
    effect = dict(row)
    effect["metadata"] = decoded(effect.pop("metadata_json", None))
    effect["export_allowed"] = effect["metadata"].get("export_allowed") is True
    effect["feature_mapping"] = (feature_match_identity(gene, effect) if gene
                                 else {"status": "source_feature", "label": "연구의 원본 feature"})
    effect["key"] = {key: effect[key] for key in
                     ("series_accession", "feature_type", "feature_id", "contrast", "analysis_version")}
    return effect


def _batch_contrast_effects(core_path, contrasts, gene, projection, limit=6, *, store_cache=True):
    """Prime only matched selectors; one source snapshot and sorted PK fetches.

    Legacy/unindexed releases retain the scalar route. No preview matrix or
    persistent value copy is introduced; successful source-bound cache entries
    and all API payloads keep their original format.
    """
    if not contrasts or not Path(core_path).is_file():
        return None
    identity = fingerprint(core_path)
    if not indexed_source(projection, core_path):
        return None
    projection_path = logical_connection_path(projection)
    projection_identity = fingerprint(projection_path) if projection_path else None
    modes = [None, gene] if gene else [None]
    selected = {selector_key(contrast): contrast for contrast in contrasts}
    results, missing = {"_batch_sources": (identity, projection_identity)}, []
    for selector, contrast in selected.items():
        for mode in modes:
            key = _preview_key(identity, projection_identity, contrast, mode, limit)
            cached = CACHE.get(key)
            if cached is not None:
                results[key] = json.loads(cached)
            else:
                missing.append((selector, contrast, mode, key))
    if not missing:
        return results
    try:
        selectors = list(dict.fromkeys(item[0] for item in missing))
        placeholders = ",".join("?" for _ in selectors)
        summaries = {row["selector"]: row["total"] for row in projection.execute(
            "SELECT selector,total FROM perf_effect_selector WHERE selector IN (" + placeholders + ")", selectors)}
        # A partially indexed source is supported without changing its legacy
        # missing-selector semantics; it shares the one source connection.
        prepared, refs = {}, []
        for mode in modes:
            entries = [item for item in missing if item[2] is mode and item[0] in summaries]
            if not entries:
                continue
            ids = gene_feature_identifiers(mode) if mode else []
            feature_clause = (" AND feature_type IN ('gene_expression','expression','gene','transcript') AND feature_id IN (" +
                              ",".join("?" for _ in ids) + ")") if mode else ""
            mode_selectors = [item[0] for item in entries]
            totals = summaries
            if mode:
                totals = {row["selector"]: row["n"] for row in projection.execute(
                    "SELECT selector,count(*) n FROM perf_effect_ref WHERE selector IN (" +
                    ",".join("?" for _ in mode_selectors) + ")" + feature_clause + " GROUP BY selector", [*mode_selectors, *ids])}
            branch = ("SELECT ? origin,effect_id,feature_type,feature_id FROM (SELECT effect_id,feature_type,feature_id "
                      "FROM perf_effect_ref WHERE selector=?" + feature_clause + " ORDER BY ordinal_all LIMIT ?)")
            parameters = [value for selector in mode_selectors for value in (selector, selector, *ids, limit)]
            by_selector = {selector: [] for selector in mode_selectors}
            for row in projection.execute(" UNION ALL ".join(branch for _ in mode_selectors), parameters):
                by_selector[row["origin"]].append(dict(row))
                refs.append(row["effect_id"])
            for selector, contrast, _, key in entries:
                prepared[key] = (contrast, mode, by_selector[selector], totals.get(selector, 0))
        with read_connection(core_path) as core:
            core.execute("BEGIN")
            # Public releases use native selectors and a view; preserve their
            # scalar policy path rather than treating a missing public assay as 0.
            if core.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='effect_selector'").fetchone():
                return None
            by_id = read_rows_by_ids(core, refs)
            for key, (contrast, mode, references, total) in prepared.items():
                try:
                    rows = [verify_reference(by_id.get(ref["effect_id"]), contrast, ref) for ref in references]
                    results[key] = ([_format_effect(row, mode) for row in rows], total, "available")
                except QueryUnavailable as error:
                    results[key] = ([], None, error.status)
            for selector, contrast, mode, key in missing:
                if key in prepared:
                    continue
                conditions = "series_accession=? AND contrast=? AND analysis_version=?"
                args = [contrast[k] for k in ("series_accession", "contrast", "analysis_version")]
                if mode:
                    identifiers = gene_feature_identifiers(mode)
                    conditions += " AND feature_type IN ('gene_expression','expression','gene','transcript') AND feature_id IN (" + ",".join("?" for _ in identifiers) + ")"
                    args.extend(identifiers)
                total = core.execute("SELECT count(*) FROM study_effect WHERE " + conditions, args).fetchone()[0]
                rows = core.execute("SELECT * FROM study_effect WHERE " + conditions + " ORDER BY feature_type,feature_id LIMIT ?", [*args, limit]).fetchall()
                results[key] = ([_format_effect(row, mode) for row in rows], total, "available")
        if fingerprint(core_path) != identity or (fingerprint(projection_path) if projection_path else None) != projection_identity:
            return {**{key: ([], None, "source_changed") for key in results if key != "_batch_sources"},
                    **{key: ([], None, "source_changed") for _, _, _, key in missing},
                    "_batch_sources": (identity, projection_identity)}
        for _, _, _, key in missing:
            if store_cache and results[key][2] == "available":
                CACHE.put(key, canonical(results[key]).encode())
        return results
    except (QueryUnavailable, sqlite3.Error) as error:
        status = error.status if isinstance(error, QueryUnavailable) else (
            "query_timeout" if CURRENT.get() and CURRENT.get().expired() else "source_unavailable")
        results.update({key: ([], None, status) for _, _, _, key in missing})
        return results


def annotate_effect_context(db, effects):
    """Only the exact original analysis version may supply statistical context."""
    if not effects:
        return
    runs = science_records(db, "analysis_run")
    contrasts = {
        (c["series_accession"], c["contrast"], c["analysis_version"]): c
        for c in science_records(db, "contrast")
    }
    for effect in effects:
        key = tuple(effect.get(k) for k in ("series_accession", "contrast", "analysis_version"))
        contrast = contrasts.get(
            key, {k: effect.get(k) for k in ("series_accession", "contrast", "analysis_version")}
        )
        effect["analysis_context"] = _run_context(runs, contrast)


def _cached_effects(core_path, contrast, gene, limit, projection=None):
    conditions = "series_accession=? AND contrast=? AND analysis_version=?"
    args = [contrast[k] for k in ("series_accession", "contrast", "analysis_version")]
    if gene:
        identifiers = gene_feature_identifiers(gene)
        conditions += (
            " AND feature_type IN ('gene_expression','expression','gene','transcript') AND feature_id IN ("
            + ",".join("?" for _ in identifiers)
            + ")"
        )
        args += identifiers
    try:
        with read_connection(core_path) as core:
            public_total = None
            if core.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='effect_selector'").fetchone():
                public_total = core.execute(
                    "SELECT sum(total) FROM effect_selector WHERE series_accession=? AND contrast=? AND analysis_version=?",
                    [contrast[k] for k in ("series_accession", "contrast", "analysis_version")],
                ).fetchone()[0]
                if public_total is None:
                    # A curated trial can remain visible when its numeric data
                    # is withheld. This is not a confirmed zero-result analysis.
                    raise QueryUnavailable("not_in_public_release")
            selector = selector_key(contrast)
            indexed = projection is not None and indexed_source(projection, core_path)
            summary = projection.execute("SELECT total FROM perf_effect_selector WHERE selector=?", (selector,)).fetchone() if indexed else None
            if summary is not None:
                clause, ref_args = "selector=?", [selector]
                if gene:
                    clause += " AND feature_type IN ('gene_expression','expression','gene','transcript') AND feature_id IN (" + ",".join("?" for _ in identifiers) + ")"
                    ref_args.extend(identifiers)
                total = projection.execute("SELECT count(*) FROM perf_effect_ref WHERE " + clause, ref_args).fetchone()[0] if gene else summary[0]
                refs = projection.execute("SELECT effect_id,feature_type,feature_id FROM perf_effect_ref WHERE " + clause + " ORDER BY ordinal_all LIMIT ?", [*ref_args, limit]).fetchall()
                ids = [r[0] for r in refs]
                by_id = {r["id"]: r for r in core.execute("SELECT * FROM study_effect WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids)} if ids else {}
                rows = []
                for ref in refs:
                    r = by_id.get(ref[0])
                    if r is None or any(r[k] != contrast[k] for k in ("series_accession", "contrast", "analysis_version")) or (r["feature_type"], r["feature_id"]) != (ref[1], ref[2]):
                        raise QueryUnavailable("analysis_key_mismatch")
                    rows.append(r)
            else:
                total = public_total if public_total is not None and not gene else core.execute("SELECT count(*) FROM study_effect WHERE " + conditions, args).fetchone()[0]
                if not gene and total == 0 and projection is not None and has_index(projection, "perf_effect_selector"):
                    original_total = projection.execute("SELECT total FROM perf_effect_selector WHERE selector=?", (selector,)).fetchone()
                    if original_total is not None and original_total[0] > 0:
                        raise QueryUnavailable("analysis_key_mismatch")
                rows = core.execute("SELECT * FROM study_effect WHERE " + conditions + " ORDER BY feature_type,feature_id LIMIT ?", [*args, limit]).fetchall()
            effects = []
            for row in rows:
                effects.append(_format_effect(row, gene))
            return effects, total, "available"
    except QueryUnavailable as exc:
        return [], None, exc.status


def _dose_card(seed, exposure, identity, reference, dose):
    nominal = (
        seed.get("kind") == "food"
        and identity["status"] == "related_food"
        and identity.get("method") == "food_category_correspondence"
        and dose.get("unit") == "g"
        and "product" in str(dose.get("analyte_basis", "")).casefold()
    )
    food_label = (seed.get("label", "") + " " + seed.get("label_en", "")).casefold()
    if any(
        term in food_label
        for term in (
            "with yogurt",
            "blend",
            "melts",
            "dressing",
            "powder",
            "coated",
            "covered",
            "드레싱",
            "분말",
        )
    ):
        nominal = False
    given = reference.get("intake_g") if nominal else None
    amount = dose.get("amount")
    status = "unknown"
    reason = "제품·분석 항목·단위·빈도가 확인되지 않아 용량을 대응시키지 않습니다."
    if identity.get("method") == "food_term_correspondence":
        reason = "음식명·원료의 관련성만 확인됐습니다. 전체 시험 제품 중량의 대응은 미확인입니다."
    if (
        given is not None
        and amount is not None
        and FREQUENCIES.get(dose.get("frequency")) == "single"
    ):
        status = "match" if given == amount else "mismatch"
        reason = "관련 식품군의 명목 제품 무게 비교입니다. 제품 동등성·흡수량·효과량 비례는 의미하지 않습니다."
    if identity["status"] == "same_record":
        reason = (
            "실험 기록을 선택했으나 입력 섭취량은 없습니다. 실제 투여량은 연구 기록에만 표시합니다."
        )
    return _condition(
        "dose",
        given,
        {
            "amount": amount,
            "unit": dose.get("unit"),
            "basis": dose.get("analyte_basis"),
            "frequency": dose.get("frequency"),
        },
        status,
        reason,
    )


def _clinical_results(db, study_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workbench_evidence'").fetchone():
        return []
    from .explorer_workbench import record
    return [record(row, db) for row in db.execute(
        "SELECT * FROM workbench_evidence WHERE layer='clinical' AND study_id=? ORDER BY id LIMIT 50", (study_id,))]


def scientific_matches(db, seed, core_path, reference_context=None, gene=None):
    reference = dict(reference_context or {})
    reference["intake_g"] = reference.get("intake_g", 100) if seed.get("kind") == "food" else None
    records = {
        kind: science_records(db, kind)
        for kind in (
            "exposure",
            "contrast",
            "study",
            "arm",
            "analysis_run",
            "assertion",
            "bridge_source",
            "curation",
        )
    }
    curations = records["curation"][0] if records["curation"] else {}
    curated_exp = {r["core_exposure_id"]: r for r in curations.get("exposures", [])}
    curated_study = {r["study_id"]: r for r in curations.get("studies", [])}
    study_by_id = {r["id"]: r for r in records["study"]}
    sources = {r["id"]: r for r in records["bridge_source"]}
    live_sources = {r["id"]: r for r in curations.get("sources", [])}
    components = []
    if seed.get("kind") == "food":
        components = [
            r[0]
            for r in db.execute(
                "SELECT DISTINCT component FROM measurement WHERE food=? AND amount>0 AND status NOT IN ('missing','unverified','below_detection')",
                (seed["id"],),
            )
        ]
        seed = {
            **seed,
            "metadata": {**seed.get("metadata", {}), "available_component_ids": components},
        }
    evidence, gaps = [], []
    has_match_index = has_index(db, "perf_exposure_match")
    match_index = {r[0]: decoded(r[1]) for r in db.execute("SELECT exposure_id,identity_json FROM perf_exposure_match WHERE node_id=?", (seed["id"],))} if has_match_index else {}
    matched_exposures = []
    for original in records["exposure"]:
        curation = curated_exp.get(original["id"], {})
        exposure = {**original, **curation}
        keys = decoded(original.get("component_keys"), [])
        identity = match_index.get(original["id"], {"status": "unknown"}) if has_match_index else exposure_match_identity(seed, exposure, keys)
        if identity["status"] == "unknown":
            continue
        matched_exposures.append((original, curation, exposure, identity))
    matched_ids = {original["id"] for original, _, _, _ in matched_exposures}
    batch_results = _batch_contrast_effects(core_path,
        [contrast for contrast in records["contrast"] if matched_ids.intersection(contrast.get("exposure_ids", []))],
        gene, db)
    for original, curation, exposure, identity in matched_exposures:
        study = study_by_id.get(original["study_id"], {})
        study_cur = curated_study.get(original["study_id"], {})
        verified = study_cur.get("verified_fields", {})
        meta = original.get("metadata", {})
        dose = curation.get(
            "dose",
            {
                "amount": original.get("amount"),
                "unit": original.get("unit"),
                "duration_days": original.get("duration_days"),
                "frequency": meta.get("dose_frequency"),
                "analyte_basis": original.get("dose_basis"),
            },
        )
        contrasts = []
        applicable = []
        for contrast in records["contrast"]:
            if original["id"] not in contrast.get("exposure_ids", []):
                continue
            selector = next(
                (c for c in study_cur.get("contrast_selectors", []) if c["id"] == contrast["id"]),
                {},
            )
            contrast = {**contrast, "comparison_kind": selector.get("comparison_kind")}
            design = contrast_classification(contrast)
            effects, total, effects_status = _contrast_effects(core_path, contrast, gene, projection=db, batch_results=batch_results)
            source_effects = _contrast_effects(core_path, contrast, projection=db, batch_results=batch_results) if gene else (effects, total, effects_status)
            source_total = source_effects[1]
            context = _run_context(records["analysis_run"], contrast)
            if selector.get("sample_scope"):
                context.update(
                    _merge_sample_scopes(
                        [
                            *context.get("sample_scope_sources", []),
                            {
                                "source": "source_checked_contrast",
                                "values": selector["sample_scope"],
                            },
                        ]
                    )
                )
                context["sample_scope_provenance"] = selector.get("sample_scope_provenance", {})
            comparison_arms = [r for r in records["arm"] if r["id"] in contrast.get("arm_ids", [])]
            exposure_in_arms = [
                r["id"] for r in comparison_arms if original["id"] in r.get("exposure_ids", [])
            ]
            shared = len(comparison_arms) >= 2 and len(exposure_in_arms) == len(comparison_arms)
            studied = {
                "tissue": contrast.get("tissue"),
                "response_time_h": contrast.get("response_time_h"),
                "regimen_duration_days": dose.get("duration_days"),
                "frequency": dose.get("frequency"),
                "population": verified.get("population"),
            }
            cards = compare_context(reference, studied)
            applicable.extend([{**card, "contrast_id": contrast["id"]} for card in cards])
            for effect in effects:
                effect["analysis_context"] = context
            contrasts.append(
                {
                    **{
                        k: contrast.get(k)
                        for k in (
                            "id",
                            "series_accession",
                            "contrast",
                            "analysis_version",
                            "comparison",
                            "n_people",
                            "n_samples",
                            "tissue",
                            "response_time_h",
                            "exposure_ids",
                            "arm_ids",
                        )
                    },
                    "design_classification": design["classification"],
                    "label": design["label"],
                    "n_people_scope": "source contrast record; consult audited count scopes, parent cohort is separate",
                    "n_samples_scope": contrast.get("metadata", {}).get(
                        "n_samples_scope", "unknown; consult fitted and contributing assay scope"
                    ),
                    "analysis_context": context,
                    "effects": effects,
                    "effect_total": total,
                    "effects_status": effects_status,
                    "source_effects_status": source_effects[2],
                    "source_effect_total": source_total,
                    "gene_filter_applied": gene is not None,
                    "effect_preview": "lexical_not_significance_selected; maximum 6 source features",
                    "matched_exposure_role": "shared_in_both_arms"
                    if shared
                    else "present_in_comparison_context",
                    "comparison_exposures": [
                        {
                            "arm_id": r["id"],
                            "label": r.get("label"),
                            "exposure_ids": r.get("exposure_ids", []),
                        }
                        for r in comparison_arms
                    ],
                    "exposure_role_note": "양쪽 비교군에서 함께 섭취한 노출입니다. 이 대비는 해당 음식의 독립 효과를 검정하지 않습니다."
                    if shared
                    else "대비 맥락에 포함된 노출입니다. 식품·성분의 독립 효과는 별도 확인이 필요합니다.",
                    "applicability": cards,
                    "source_url": "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc="
                    + contrast["series_accession"]
                    if contrast["series_accession"].startswith("GSE")
                    else "https://www.ebi.ac.uk/biostudies/arrayexpress/studies/"
                    + contrast["series_accession"],
                    "all_results_url": "/api/v1/evidence?"
                    + urlencode(
                        {
                            "accession": contrast["series_accession"],
                            "contrast": contrast["contrast"],
                            "analysis_version": contrast["analysis_version"],
                        }
                    ),
                }
            )
        primary = sources.get(original.get("source_id"), {})
        provenance = (
            [
                {
                    **primary,
                    "hash_scope": "curation_manifest",
                    "access_level": "curator_paraphrase",
                    "expert_review_status": "pending",
                }
            ]
            if primary
            else []
        )
        for source_id in curation.get("source_ids", []):
            s = live_sources.get(source_id)
            if s:
                provenance.append(
                    {
                        **s,
                        "version": curations.get("version"),
                        "hash_scope": "retrieved_response"
                        if s.get("retrieved_response_sha256")
                        else "url_and_locator_only",
                        "source_location": "; ".join(s.get("source_locations", [])),
                        "export_allowed": True,
                    }
                )
        limitations = [
            "각 성분의 독립 효과와 개인 건강 효과를 추정하지 않습니다.",
            "출처 대조 완료와 전문가 검수 완료는 다릅니다.",
        ]
        limitations += curation.get("known_mismatches", [])
        if not curation:
            limitations.append(
                "이번 원문 대조 curation 범위 밖의 기존 metadata 기록입니다. 원 연구의 조건을 추가 확인해야 합니다."
            )
        for assertion in records["assertion"]:
            if assertion.get("exposure_id") == original["id"]:
                limitations.extend(decoded(assertion.get("limitations"), []))
        unresolved = original["study_id"] == "S01" and not contrasts
        evidence.append(
            {
                "id": "scientific:" + original["id"],
                "study_id": original["study_id"],
                "study_label": study.get("title", study.get("name", original["study_id"])),
                "exposure": {
                    **{k: original.get(k) for k in ("id", "kind", "formulation")},
                    "label": curation.get("display_label")
                    or (
                        original.get("formulation")
                        if "\ufffd" in original.get("label", "")
                        else original.get("label")
                    ),
                    "source_original_label": original.get("label"),
                    "node_id": "Exposure:" + original["id"],
                    "amount": dose.get("amount"),
                    "unit": dose.get("unit"),
                    "duration_days": dose.get("duration_days"),
                    "frequency": FREQUENCIES.get(dose.get("frequency"), dose.get("frequency")),
                    "dose_basis": dose.get("analyte_basis"),
                    "actual_product_description": curation.get("actual_product_description"),
                },
                "mapping_identity": identity,
                "evidence_group": "direct_context"
                if identity["status"] == "same_record"
                else ("same_analyte" if identity["status"] == "same_analyte" else "related"),
                "study_evidence": {
                    "classification": "human_intervention_context",
                    "label": "사람 연구의 실험 맥락 · 대비별 설계 확인",
                    "original_design": verified.get("study_design", "unknown"),
                    "allocation": "reported_in_source"
                    if verified.get("study_design")
                    else "unknown",
                    "source_review_status": "assistant_source_check"
                    if curation
                    else "metadata_import_only",
                    "population": verified.get("population"),
                    "species": "Homo sapiens",
                    "tissue": verified.get("tissue"),
                    "expert_review_status": "pending",
                    "count_scopes": {k: v for k, v in verified.items() if k.startswith("n_")},
                },
                "applicability": [
                    _condition(
                        "food_formulation",
                        seed.get("label"),
                        curation.get("actual_product_description", original.get("formulation")),
                        "unknown",
                        "식품군 또는 성분 공유로 정확한 제품 동일성을 판단하지 않습니다.",
                    ),
                    _dose_card(seed, exposure, identity, reference, dose),
                    *applicable,
                ],
                "contrasts": contrasts,
                "clinical_results": _clinical_results(db, original["study_id"]),
                "provenance": provenance,
                "limitations": list(dict.fromkeys(limitations)),
                "result_status": "unresolved_visit_mapping"
                if unresolved
                else (
                    "source_unavailable"
                    if any(c["effects_status"] != "available" or c["source_effects_status"] != "available" for c in contrasts)
                    else
                    "existing_analysis"
                    if any(c["source_effect_total"] for c in contrasts)
                    else "published_context_only"
                ),
                "export_allowed": bool(primary.get("export_allowed")),
                "causal_inference": False,
            }
        )
    evidence.sort(
        key=lambda e: (
            e["mapping_identity"]["status"] == "related_component",
            e["study_id"],
            e["id"],
        )
    )
    if not records["exposure"]:
        gaps.append(
            "이 release에 실험 맥락 색인이 없습니다. 그래프 연결을 사람 실험으로 대신 해석하지 않습니다."
        )
    if not evidence:
        gaps.append("선택 항목에 대응하는 기존 사람 중재 기록 없음; 그래프 가설과 구분됩니다.")
    if evidence:
        gaps.extend(
            [
                "관련 성분이 같아도 음식·제품·배정량·기간이 같다는 뜻은 아닙니다.",
                "현재 이 오믹스 중재 조회에는 수치형 임상 결과가 연결되어 있지 않습니다. 그래프의 별도 임상 근거와 구분되며 오믹스 변화의 건강상 이득·손해는 미확인입니다.",
            ]
        )
    return {
        "rule_version": RULE_VERSION,
        "seed_id": seed["id"],
        "reference_context": reference,
        "observed_evidence": evidence,
        "evidence_gaps": gaps,
        "note": "실험 조건을 보존한 근거 조회이며 개인 반응·질병 위험 예측이 아닙니다.",
    }
