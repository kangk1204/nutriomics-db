"""Context-aware retrieval of observed experiments, never individual response prediction."""
from __future__ import annotations

import math
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .bridge_models import BridgeSource, EvidenceAssertion, Exposure, InterventionArm, StudyContrast
from .composition import convert_unit
from .models import AnalysisRun, Component, Food, ResearchStudy, StudyEffect

REVIEWED = {"source_checked", "expert_verified"}
LABELS = {
    "direct_context_match": "직접 대응",
    "related_food_or_component": "관련 음식·성분",
    "mechanistic_hypothesis": "기전 가설",
    "insufficient": "자료 부족",
}


def _context_value(value):
    return " ".join(str(value).casefold().split()) if value is not None else None


def _compare(a, b) -> str:
    if a is None or b is None:
        return "unknown"
    return "match" if _context_value(a) == _context_value(b) else "mismatch"


def _compare_number(a, b) -> str:
    if a is None or b is None:
        return "unknown"
    return "match" if math.isclose(float(a), float(b), rel_tol=1e-8, abs_tol=1e-8) else "mismatch"


def _food_related(food: Food, exposure: Exposure) -> bool:
    # Related-food retrieval only: this is deliberately insufficient to verify formulation.
    tokens = [food.name, food.name_en or "", *(food.aliases or [])]
    tokens = [_context_value(x) for x in tokens]
    for term in exposure.food_terms:
        term = _context_value(term)
        for token in tokens:
            if not term or not token:
                continue
            if term == token or token.startswith(term + "("):
                return True
            if re.search(r"[a-z]", term) and re.search(r"\b" + re.escape(term) + r"s?\b", token):
                return True
    return False


def exposure_dict(row: Exposure) -> dict:
    return {
        "id": row.id, "label": row.label, "kind": row.kind,
        "formulation": row.formulation, "amount": row.amount, "unit": row.unit,
        "dose_basis": row.dose_basis, "duration_days": row.duration_days,
        "component_keys": row.component_keys, "food_terms": row.food_terms,
        "metadata": row.metadata_json,
    }


def _provenance(source: BridgeSource) -> dict:
    return {
        "id": source.id, "url": source.url, "source_url": source.url,
        "version": source.version, "sha256": source.sha256,
        "source_location": source.source_location, "license": source.license,
        "export_allowed": source.export_allowed, "metadata": source.metadata_json,
        "hash_scope": "curation_manifest" if source.metadata_json.get("curation_manifest") else "source_snapshot",
    }


def _effects(session: Session, contrast: StudyContrast, limit: int = 6) -> tuple[list, int]:
    if not contrast.analysis_version:
        return [], 0
    conditions = [
        StudyEffect.series_accession == contrast.series_accession,
        StudyEffect.contrast == contrast.contrast,
        StudyEffect.analysis_version == contrast.analysis_version,
    ]
    indexed = _indexed_effect_preview(session, contrast, limit)
    if indexed is None:
        count = session.scalar(select(func.count()).select_from(StudyEffect).where(*conditions)) or 0
        # A bounded preview is a UI convenience. All tested/null/opposing rows stay queryable.
        rows = session.scalars(
            select(StudyEffect).where(*conditions)
            .order_by(StudyEffect.feature_type, StudyEffect.feature_id).limit(limit)
        )
    else:
        rows, count = indexed
    result = []
    for row in rows:
        result.append({
            "feature_type": row.feature_type, "feature_id": row.feature_id,
            "feature_label": row.feature_label, "contrast": row.contrast,
            "analysis_version": row.analysis_version, "effect_estimate": row.effect_estimate,
            "standard_error": row.standard_error, "effect_unit": row.effect_unit,
            "p_value": row.p_value, "q_value": row.q_value, "status": row.status,
            "n_people": row.n_people, "n_samples": row.n_samples,
            "metadata": row.metadata_json or {},
        })
    return result, count


def _indexed_effect_preview(session: Session, contrast: StudyContrast, limit: int):
    """Use exact reference IDs only when the immutable index matches this database.

    No effect value is read from the projection. A stale/missing selector or
    mismatched reference falls back to the original query, never to zero results.
    """
    from .explorer_performance import indexed_source, selector_key, source_paths

    bind = session.get_bind()
    core = bind.url.database
    projection = source_paths()[0]
    if bind.dialect.name != "sqlite" or not core or core == ":memory:" or not projection.is_file():
        return None
    try:
        with sqlite3.connect(projection.resolve().as_uri() + "?mode=ro", uri=True) as db:
            if not indexed_source(db, Path(core)):
                return None
            key = selector_key({"series_accession": contrast.series_accession,
                                "contrast": contrast.contrast,
                                "analysis_version": contrast.analysis_version})
            selector = db.execute("SELECT total FROM perf_effect_selector WHERE selector=?", (key,)).fetchone()
            if selector is None:
                return None
            references = db.execute(
                "SELECT effect_id,feature_type,feature_id FROM perf_effect_ref "
                "WHERE selector=? ORDER BY ordinal_all LIMIT ?", (key, limit)
            ).fetchall()
        if len(references) != min(limit, selector[0]):
            return None
        values = {r.id: r for r in session.scalars(
            select(StudyEffect).where(StudyEffect.id.in_([r[0] for r in references]))
        )}
        rows = []
        for row_id, feature_type, feature_id in references:
            row = values.get(row_id)
            if row is None or (row.series_accession, row.contrast, row.analysis_version,
                               row.feature_type, row.feature_id) != (
                    contrast.series_accession, contrast.contrast, contrast.analysis_version,
                    feature_type, feature_id):
                return None
            rows.append(row)
        return rows, selector[0]
    except (sqlite3.Error, OSError, ValueError):
        return None


def _analysis_provenance(session: Session, contrast: StudyContrast | None) -> dict:
    if not contrast or not contrast.analysis_version:
        return {}
    run = session.scalar(select(AnalysisRun).where(
        AnalysisRun.series_accession == contrast.series_accession,
        AnalysisRun.analysis_version == contrast.analysis_version,
    ))
    if not run:
        return {}
    metadata = run.metadata_json or {}
    return {"version": run.analysis_version, "status": run.status,
            "script_sha256": run.source_script_sha256,
            "environment_lock_sha256": run.environment_lock_sha256,
            "inputs": run.input_manifest_json, "outputs": run.output_manifest_json,
            "release_approval": metadata.get("release_approval"),
            "completion_marker": metadata.get("completion_marker"),
            "qc_scope": metadata.get("qc_limit") or metadata.get("qc_scope"),
            "statistical_scope": metadata.get("multiple_testing") or metadata.get("fdr_scope"),
            "sample_scope": metadata.get("sample_scope", {}),
            "audit_attachments": metadata.get("audit_attachments", {}),
            "producer_output_audit": metadata.get("producer_output_audit", {}),
            "environment_lock_file_current_sha256": metadata.get("environment_lock_file_current_sha256"),
            "expert_manual_review_status": "pending"}


def _assertion_dict(session: Session, assertion: EvidenceAssertion, cache: dict | None = None) -> dict:
    exposure = session.get(Exposure, assertion.exposure_id)
    source = session.get(BridgeSource, assertion.source_id)
    study = session.get(ResearchStudy, assertion.study_id)
    contrast = session.get(StudyContrast, assertion.contrast_id) if assertion.contrast_id else None
    arms = [session.get(InterventionArm, aid) for aid in contrast.arm_ids] if contrast else []
    selector = None
    results, count = [], 0
    if contrast:
        selector = {"accession": contrast.series_accession, "contrast": contrast.contrast,
                    "analysis_version": contrast.analysis_version}
        if cache is not None and contrast.id in cache:
            results, count = cache[contrast.id]
        else:
            results, count = _effects(session, contrast)
            if cache is not None:
                cache[contrast.id] = results, count
    provenance_key = ("analysis_provenance", contrast.series_accession, contrast.analysis_version) if contrast else None
    if cache is not None and provenance_key in cache:
        analysis_provenance = cache[provenance_key]
    else:
        analysis_provenance = _analysis_provenance(session, contrast)
        if cache is not None:
            cache[provenance_key] = analysis_provenance
    return {
        "id": assertion.id, "layer": assertion.layer, "assertion": assertion.assertion,
        "review_status": assertion.review_status, "organism": assertion.organism,
        "inference": assertion.inference, "study_id": assertion.study_id,
        "study_title": study.title if study else None,
        "series_accession": contrast.series_accession if contrast else None,
        "contrast": contrast.contrast if contrast else None,
        "design": contrast.design if contrast else (study.metadata_json or {}).get("study_design") if study else None,
        "tissue": contrast.tissue if contrast else None,
        "response_time_h": contrast.response_time_h if contrast else None,
        "n_people": contrast.n_people if contrast else None,
        "n_samples": contrast.n_samples if contrast else None,
        "arms": [{"id":a.id,"label":a.label,"allocation":a.allocation,"n_people":a.n_people,
                  "exposure_ids":a.exposure_ids,"metadata":a.metadata_json} for a in arms if a],
        "contrast_metadata": contrast.metadata_json if contrast else {},
        "exposure": exposure_dict(exposure) if exposure else {},
        "result_status": "reanalyzed" if count else "metadata_only",
        "results": results, "effect_count": count, "effect_selector": selector,
        "all_results_url": "/api/v1/evidence?" + urlencode({k:v for k,v in selector.items() if v is not None}) if selector else None,
        "preview_selection": "lexical_not_significance_selected",
        "limitations": assertion.limitations, "clinical_endpoints": assertion.metadata_json.get("clinical_endpoints", []),
        "clinical_data_status": assertion.metadata_json.get("clinical_data_status", "not_linked"),
        "metadata": assertion.metadata_json,
        "provenance": _provenance(source) if source else {},
        "analysis_provenance": analysis_provenance,
        "export_allowed": bool(source and source.export_allowed),
        "individual_prediction": False,
    }


def search_assertions(session: Session, query: str = "", layer=None, organism=None,
                      tissue=None, study_design=None, contrast=None, limit=100, offset=0,
                      accession=None, feature_type=None, analysis_version=None) -> dict:
    conditions = [EvidenceAssertion.review_status.in_(REVIEWED)]
    stmt = select(EvidenceAssertion).join(Exposure).outerjoin(StudyContrast)
    if query.strip():
        pattern = "%" + query.strip() + "%"
        conditions.append(or_(EvidenceAssertion.assertion.ilike(pattern), Exposure.label.ilike(pattern),
                              EvidenceAssertion.study_id.ilike(pattern), StudyContrast.series_accession.ilike(pattern)))
    for column, value in [(EvidenceAssertion.layer, layer), (EvidenceAssertion.organism, organism),
                          (StudyContrast.tissue, tissue), (StudyContrast.design, study_design),
                          (StudyContrast.contrast, contrast), (StudyContrast.series_accession, accession),
                          (StudyContrast.analysis_version, analysis_version)]:
        if value is not None:
            conditions.append(column == value)
    if feature_type:
        conditions.append(select(StudyEffect.id).where(
            StudyEffect.series_accession == StudyContrast.series_accession,
            StudyEffect.contrast == StudyContrast.contrast,
            StudyEffect.analysis_version == StudyContrast.analysis_version,
            StudyEffect.feature_type == feature_type,
        ).exists())
    stmt = stmt.where(*conditions)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(stmt.order_by(EvidenceAssertion.id).limit(limit).offset(offset))
    cache = {}
    items = [_assertion_dict(session, row, cache) for row in rows]
    return {"items": items, "total": total, "count": len(items), "limit": limit, "offset": offset}


def list_interventions(session: Session, query: str = "", limit=100, offset=0) -> dict:
    stmt = select(Exposure)
    if query.strip():
        stmt = stmt.where(or_(Exposure.label.ilike("%" + query + "%"), Exposure.study_id == query))
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = []
    for row in session.scalars(stmt.order_by(Exposure.id).limit(limit).offset(offset)):
        source = session.get(BridgeSource, row.source_id)
        item = exposure_dict(row)
        item.update(study_id=row.study_id, provenance=_provenance(source) if source else {},
                    export_allowed=bool(source and source.export_allowed))
        items.append(item)
    return {"items": items, "total": total, "count": len(items), "limit": limit, "offset": offset}


def get_provenance(session: Session, evidence_id: str) -> dict | None:
    assertion = session.get(EvidenceAssertion, evidence_id)
    source = session.get(BridgeSource, assertion.source_id if assertion else evidence_id)
    if source is None:
        from .annotations import annotation_provenance
        return annotation_provenance(session, evidence_id)
    result = _provenance(source)
    if assertion:
        result.update(evidence_id=assertion.id, review_status=assertion.review_status,
                      study_id=assertion.study_id, assertion=assertion.assertion)
        c = session.get(StudyContrast, assertion.contrast_id) if assertion.contrast_id else None
        if c and c.analysis_version:
            result["analysis"] = _analysis_provenance(session, c)
    return result


def study_bridge_detail(session: Session, accession: str) -> dict:
    return search_assertions(session, accession=accession, limit=1000)


def applicability(exposure: Exposure, contrast: StudyContrast | None, foods: list[tuple[Food,float]],
                  components: dict, reference_context: dict | None) -> dict:
    context = reference_context or {}
    component_matches = sorted(set(exposure.component_keys) & set(components))
    related = [(f,g) for f,g in foods if _food_related(f, exposure)]
    verified = [(f,g) for f,g in related
                if (f.metadata_json or {}).get("intervention_formulation") == exposure.formulation]
    input_amount = None
    comparison_scope = "not_comparable"
    if exposure.kind in {"food", "product"} and related and exposure.unit == "g":
        input_amount = sum(g for _,g in related)
        comparison_scope = "verified_formulation_mass" if verified else "nominal_related_food_mass"
    elif exposure.kind == "mixed_meal" and exposure.unit == "kcal":
        energy = components.get("FDC-NUTRIENT:1008")
        if energy:
            input_amount = convert_unit(energy["amount"], energy["unit"], "kcal")
            comparison_scope = "nominal_total_meal_energy"
    elif exposure.kind in {"component", "compound", "nutrient"} and len(component_matches) == 1:
        value = components[component_matches[0]]
        input_amount = convert_unit(value["amount"], value["unit"], exposure.unit)
        comparison_scope = "component_intake_not_systemic_exposure"
    frequency = exposure.metadata_json.get("dose_frequency", "unreported")
    nominal_dose_comparison = _compare_number(input_amount, exposure.amount)
    dimensions = {
        "food_formulation": "match" if verified else "unknown",
        "component": "match" if component_matches else "unknown",
        "dose": "unknown" if frequency == "per_day" else nominal_dose_comparison,
        "dose_frequency": "match" if frequency == "single" else "unknown",
        "duration": _compare_number(context.get("regimen_duration_days"), exposure.duration_days),
        "tissue": _compare(context.get("tissue"), contrast.tissue if contrast else None),
        "response_time": _compare_number(context.get("response_time_h"), contrast.response_time_h if contrast else None),
        "population": "unknown",
    }
    # Personal characteristics are not provided in v0.1; direct transportability remains unknown.
    classification = "related_food_or_component" if component_matches or related else "insufficient"
    return {"classification": classification, "label": LABELS[classification],
            "dimensions": dimensions, "matched_component_keys": component_matches,
            "related_food_ids": [f.id for f,_ in related], "input_comparable_amount": input_amount,
            "study_amount": exposure.amount, "study_unit": exposure.unit,
            "dose_comparison_scope": comparison_scope,
            "dose_frequency": frequency,
            "nominal_amount_comparison": nominal_dose_comparison,
            "dose_basis": exposure.dose_basis,
            "component_specific_causality": False, "individual_prediction": False}


def meal_evidence(session: Session, calculation: dict, reference_context: dict | None = None) -> dict:
    ids = {int(row["component_id"]) for row in calculation.get("components", [])}
    keys = {c.id: c.component_key for c in session.scalars(select(Component).where(Component.id.in_(ids)))} if ids else {}
    components = {keys[row["component_id"]]: row for row in calculation.get("components", [])
                  if row["component_id"] in keys and row.get("amount",0) > 0}
    food_ids = {row["food_id"] for row in calculation.get("items", [])}
    food_map = {f.id: f for f in session.scalars(select(Food).where(Food.id.in_(food_ids)))}
    foods = [(food_map.get(row["food_id"]), row["amount_g"]) for row in calculation.get("items", [])]
    foods = [(f,g) for f,g in foods if f is not None]
    output, gaps, provenance, cache = [], [], [], {}
    exposures = list(session.scalars(select(Exposure).order_by(Exposure.id)))
    study_ids = {e.study_id for e in exposures}
    source_ids = {e.source_id for e in exposures}
    contrast_rows = list(session.scalars(select(StudyContrast).where(StudyContrast.study_id.in_(study_ids))))
    arm_ids = {aid for c in contrast_rows for aid in c.arm_ids}
    # Keep small context records alive within this request. The identity map uses
    # weak references, otherwise successive assertions repeatedly decode them.
    cache["context_records"] = [*contrast_rows,
        *session.scalars(select(BridgeSource).where(BridgeSource.id.in_(source_ids))),
        *session.scalars(select(ResearchStudy).where(ResearchStudy.id.in_(study_ids))),
        *session.scalars(select(InterventionArm).where(InterventionArm.id.in_(arm_ids)))]
    matched_keys = set()
    for exposure in exposures:
        contrasts = {c.id:c for c in contrast_rows if c.study_id == exposure.study_id}
        assertions = session.scalars(select(EvidenceAssertion).where(EvidenceAssertion.exposure_id == exposure.id,
                                                                     EvidenceAssertion.review_status.in_(REVIEWED)))
        for assertion in assertions:
            c = contrasts.get(assertion.contrast_id)
            match = applicability(exposure, c, foods, components, reference_context)
            if match["classification"] == "insufficient":
                continue
            row = _assertion_dict(session, assertion, cache)
            row["applicability"] = match
            output.append(row)
            matched_keys.update(match["matched_component_keys"])
            provenance.append({"evidence_id": row["id"], **row["provenance"]})
            if row["result_status"] != "reanalyzed":
                gaps.append({"evidence_id": row["id"], "code": "no_reanalyzed_results",
                             "message": "중재 정보만 확보되었으며 재분석 효과는 아직 없습니다."})
    hypotheses = []
    try:
        from .annotations import mechanisms_for_keys
        hypotheses = mechanisms_for_keys(session, set(components))
    except ImportError:
        pass
    for key in sorted(set(components) - matched_keys):
        gaps.append({"component_key": key, "code": "no_linked_human_intervention",
                     "message": "현재 DB에 연결된 사람 중재가 없습니다. 근거 부재나 무효과를 의미하지 않습니다."})
    if not output:
        gaps.append({"code": "no_contextual_human_evidence", "message": "현재 입력에 관련된 검수 완료 사람 중재가 없습니다."})
    gaps.append({"code": "individual_response_not_validated", "message": "개인별 발현·CpG 변화와 질병 위험 예측은 검증되지 않았습니다."})
    if output and any(x["clinical_data_status"] == "not_linked" for x in output):
        gaps.append({"code": "clinical_endpoint_not_linked", "message": "분자 변화에 대응하는 임상 결과가 연결되지 않은 연구가 있습니다."})
    return {"observed_evidence": output, "mechanistic_hypotheses": hypotheses,
            "applicability": [{"evidence_id": x["id"], **x["applicability"]} for x in output],
            "evidence_gaps": gaps, "provenance": provenance,
            "individual_prediction": False, "transitive_health_inference": False}
