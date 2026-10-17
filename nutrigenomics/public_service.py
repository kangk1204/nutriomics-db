"""Read-only public deployment. No collection, write, or analysis routes are installed."""
from __future__ import annotations

import base64
import json
import math
import os
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, FiniteFloat, field_validator

from .composition_policy import POLICY_VERSION, assess, convert_unit, normalize_unit, select_unique
from .explorer import MatchInput, ReferenceContext, _match, _source, attach_explorer
from .explorer_build import canonical, sha256
from .explorer_context import ExplorerContextMiddleware
from .explorer_performance import read_connection


class PublicMealItem(BaseModel):
    food_id: str | int
    amount_g: FiniteFloat = Field(gt=0, le=20000)

    @field_validator("amount_g", mode="before")
    @classmethod
    def finite_amount(cls, value):
        if isinstance(value, (int, float)) and not math.isfinite(value):
            raise HTTPException(422, "섭취량은 유한한 숫자여야 합니다.")
        return value


class PublicMeal(BaseModel):
    items: list[PublicMealItem] = Field(min_length=1, max_length=100)
    reference_context: ReferenceContext | None = None


def create_app(release: Path, *, allow_candidate=False, verify_hash=True):
    release = Path(release).resolve()
    metadata = json.loads((release / "dataset-metadata.json").read_text(encoding="utf-8"))
    manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
    database = release / "nutriomics.sqlite3"
    if verify_hash and sha256(database) != manifest["database_sha256"]:
        raise ValueError("public release hash mismatch")
    if not allow_candidate:
        gate_path = release / "release-gate.json"
        gate = json.loads(gate_path.read_text(encoding="utf-8")) if gate_path.is_file() else {}
        if (metadata.get("status") != "submission_ready" or gate.get("submission_ready") is not True
                or gate.get("release_id") != metadata["release_id"]
                or gate.get("database_sha256") != manifest["database_sha256"]
                or gate.get("failures") != []):
            raise ValueError("public deployment requires a reviewed, validated, hash-bound release gate")
        bindings = gate.get("evidence_files", [])
        if not bindings:
            raise ValueError("public deployment requires stored review and validation evidence")
        for binding in bindings:
            bound = (release / binding["path"]).resolve()
            if (not bound.is_relative_to(release) or not bound.is_file()
                    or sha256(bound) != binding.get("sha256")):
                raise ValueError("public release review/validation evidence hash mismatch")
    app = FastAPI(title="NutriOmics public data", version=metadata["release_id"])

    @app.middleware("http")
    async def release_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-NutriOmics-Release"] = metadata["release_id"]
        response.headers["X-NutriOmics-Scope"] = "public"
        return response

    @contextmanager
    def read():
        with read_connection(database) as db:
            yield db

    @app.get("/healthz")
    def health():
        return {"status": "ok", "release_id": metadata["release_id"], "scope": "public_only"}

    @app.get("/api/v1/public/meta")
    def meta():
        return metadata

    @app.get("/api/v1/analyses")
    def analyses(role: str | None = "primary_candidate"):
        with read() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='contrast_selector'").fetchone():
                return {"items": [], "availability_reason": "context_index_not_built",
                        "release_id": metadata["release_id"]}
            rows = db.execute("SELECT * FROM contrast_selector" +
                              (" WHERE analysis_role=?" if role else "") +
                              " ORDER BY series_accession,analysis_version,contrast",
                              (role,) if role else ()).fetchall()
            return {"items": [dict(r) for r in rows], "expert_review": metadata["expert_review"],
                    "release_id": metadata["release_id"]}

    @app.get("/api/v1/foods")
    def foods(query: str = "", limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        with read() as db:
            rows = db.execute("SELECT * FROM node WHERE kind='food' AND (label LIKE ? OR aliases_json LIKE ? OR id=?) ORDER BY label,id LIMIT ? OFFSET ?", ("%" + query + "%", "%" + query + "%", query, limit, offset)).fetchall()
            return {"items": [dict(r) for r in rows], "release_id": metadata["release_id"]}

    @app.get("/api/v1/interventions")
    def interventions():
        with read() as db:
            return {"items": [json.loads(r[0]) for r in db.execute("SELECT metadata_json FROM science_record WHERE kind='exposure' ORDER BY id")], "release_id": metadata["release_id"]}

    @app.get("/api/v1/studies")
    def studies():
        with read() as db:
            return {"items": [json.loads(r[0]) for r in db.execute("SELECT metadata_json FROM science_record WHERE kind='study' ORDER BY id")], "release_id": metadata["release_id"]}

    @app.get("/api/v1/studies/{identifier}")
    def study(identifier: str):
        with read() as db:
            records = [(r["kind"], json.loads(r["metadata_json"])) for r in db.execute("SELECT * FROM science_record")]
            root = next((r for k, r in records if k == "study" and (r["id"] == identifier or identifier in r.get("metadata", {}).get("series", []))), None)
            if root is None:
                raise HTTPException(404, "study not in this public release")
            return {"study": root, "records": [{"kind": k, "record": r} for k, r in records if r.get("study_id") == root["id"]], "release_id": metadata["release_id"]}

    @app.get("/api/v1/evidence")
    def evidence(accession: str, contrast: str, analysis_version: str,
                 feature_type: str | None = None, feature_id: str | None = None,
                 limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
        clauses = ["series_accession=?", "contrast=?", "analysis_version=?"]
        args = [accession, contrast, analysis_version]
        for key, value in (("feature_type", feature_type), ("feature_id", feature_id)):
            if value is not None:
                clauses.append(key + "=?")
                args.append(value)
        scope = [metadata["release_id"], accession, contrast, analysis_version, feature_type, feature_id]
        if cursor is not None:
            try:
                decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if decoded[0] != scope or len(decoded) != 3:
                    raise ValueError("cursor scope mismatch")
            except (ValueError, TypeError, IndexError) as exc:
                raise HTTPException(422, "cursor does not match this release and selector") from exc
            clauses.append("(feature_type>? OR (feature_type=? AND feature_id>?))")
            args.extend([decoded[1], decoded[1], decoded[2]])
        with read() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM effect WHERE " + " AND ".join(clauses) + " ORDER BY feature_type,feature_id LIMIT ?", [*args, limit + 1])]
            page, more = rows[:limit], len(rows) > limit
            for r in page:
                r["metadata"] = json.loads(r.pop("metadata_json"))
            total = db.execute("SELECT sum(total) FROM effect_selector WHERE series_accession=? AND contrast=? AND analysis_version=?" + (" AND feature_type=?" if feature_type else ""), [accession, contrast, analysis_version, *([feature_type] if feature_type else [])]).fetchone()[0]
            context = None
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='contrast_selector'").fetchone():
                context = db.execute("SELECT * FROM contrast_selector WHERE series_accession=? AND contrast=? AND analysis_version=?",
                                     (accession, contrast, analysis_version)).fetchone()
            return {"items": page, "total_selector": total, "effects_status": "available" if total is not None else "not_in_public_release", "next_cursor": base64.urlsafe_b64encode(canonical([scope, page[-1]["feature_type"], page[-1]["feature_id"]]).encode()).decode() if more else None,
                    "analysis_context": dict(context) if context else None,
                    "availability_reason": "public_quantitative_results" if total is not None else "not_reported_or_withheld_in_this_release",
                    "release_id": metadata["release_id"], "expert_review": metadata["expert_review"]}

    @app.post("/api/v1/meals/analyze")
    def meal(payload: PublicMeal):
        totals, provenance, excluded, selected = {}, [], [], []
        component_sources, source_cache = {}, {}
        matches = []
        with read() as db:
            for item in payload.items:
                food_id = str(item.food_id)
                if isinstance(item.food_id, int) or food_id.isdecimal():
                    mapped = db.execute("SELECT food_id FROM food_alias WHERE legacy_food_id=?", (int(item.food_id),)).fetchone()
                    if not mapped:
                        raise HTTPException(404, "food is outside the public release; this is not evidence of absence")
                    food_id = mapped[0]
                food = db.execute("SELECT id,label,metadata_json FROM node WHERE id=? AND kind='food'", (food_id,)).fetchone()
                if food is None:
                    raise HTTPException(404, "food not in public release")
                food_info = json.loads(food["metadata_json"])
                selected.append({"food_id": food_id, "amount_g": item.amount_g,
                                 "name": food["label"], "food_name": food["label"],
                                 "preparation_state": food_info.get("preparation_state", food_info.get("preparation")),
                                 "calculation_mode": "direct"})
                previous_contributions = len(provenance)
                candidates = []
                for row in db.execute("SELECT * FROM measurement WHERE food=? ORDER BY id", (food_id,)):
                    raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
                    assessment = assess(status=row["status"], amount=row["amount"], unit=row["unit"],
                                        basis=row["basis"], metadata=raw, food_metadata=food_info,
                                        source_id=row["source_id"])
                    candidates.append({"row": row, "key": row["component"], "id": row["id"],
                                       "unit": row["unit"], "assessment": assessment})
                chosen, omitted = select_unique(candidates)
                for entry, reasons in omitted:
                    row = entry["row"]
                    component = db.execute("SELECT label FROM node WHERE id=?", (row["component"],)).fetchone()
                    excluded.append({"measurement_id": row["id"], "food_id": food_id,
                                     "name": component[0] if component else row["component"],
                                     "source_id": row["source_id"],
                                     "status": row["status"], "basis": row["basis"],
                                     "reasons": reasons, "assessment": entry["assessment"]})
                for entry in chosen:
                    row, assessment = entry["row"], entry["assessment"]
                    node = db.execute("SELECT metadata_json FROM node WHERE id=?", (row["component"],)).fetchone()
                    component_info = json.loads(node[0]) if node else {}
                    unit = normalize_unit(component_info.get("canonical_unit")) or normalize_unit(row["unit"])
                    converted = convert_unit(row["amount"], row["unit"], unit)
                    if converted is None:
                        converted, unit = row["amount"], normalize_unit(row["unit"])
                    amount = converted * item.amount_g / 100
                    if not math.isfinite(amount):
                        raise HTTPException(503, "non-finite source value")
                    key = (row["component"], unit)
                    totals[key] = totals.get(key, 0.0) + amount
                    if row["source_id"] not in source_cache:
                        source_cache[row["source_id"]] = _source(db, row["source_id"])
                    source = source_cache[row["source_id"]]
                    component_sources.setdefault(key, []).append({
                        "measurement_id": row["id"], "food_id": food_id,
                        "source_id": row["source_id"], "release": source["version"],
                        "source_ref": source["url"], "basis": row["basis"],
                        "status": row["status"], "contribution": amount,
                        "unit": unit, "source_unit": row["unit"],
                    })
                    provenance.append({"measurement_id": row["id"], "food_id": food_id,
                                       "source_id": row["source_id"], "basis": row["basis"],
                                       "status": row["status"], "contribution": amount, "unit": unit,
                                       "source_unit": row["unit"], "assessment": assessment})
                if len(provenance) == previous_contributions:
                    selected[-1]["calculation_mode"] = "unavailable"
                match = _match(db, MatchInput(seed_id=food_id, intake_g=item.amount_g,
                                             reference_context=payload.reference_context or ReferenceContext()))
                matches.append({"food_id": food_id, "amount_g": item.amount_g, **match})
            components = []
            for (key, unit), value in sorted(totals.items()):
                node = db.execute("SELECT label,metadata_json FROM node WHERE id=?", (key,)).fetchone()
                info = json.loads(node["metadata_json"]) if node else {}
                components.append({"component_key": info.get("component_key", key), "name": node["label"] if node else key, "unit": unit, "amount": value,
                                   "source_records": component_sources[(key, unit)]})
        observed = [{**e, "query_food_id": m["food_id"], "query_intake_g": m["amount_g"]}
                    for m in matches for e in m["observed_evidence"]]
        applicability = [{"food_id": e["query_food_id"], "exposure_id": e["id"],
                          "conditions": e.get("applicability", [])} for e in observed]
        gaps = list(dict.fromkeys(g for m in matches for g in m["evidence_gaps"]))
        if excluded:
            gaps.append("함량·단위·상태·가식부 기준이 미확인인 조성은 섭취량 합계에서 제외했습니다.")
        return {"items": selected, "components": components, "provenance": provenance,
                "status": "partial" if components and excluded else "measured" if components else "unavailable",
                "unquantified": [{**row, "exclusion_reasons": row["reasons"]} for row in excluded],
                "interpretation": "함량 기준이 확인된 성분의 섭취량 계산입니다. 개인 반응 또는 건강 효과 예측이 아닙니다.",
                "observed_evidence": observed, "evidence_by_food": matches,
                "applicability": applicability, "mechanistic_hypotheses": [], "evidence_gaps": gaps,
                "reference_context": (payload.reference_context or ReferenceContext()).model_dump(exclude_none=True),
                "excluded_measurements": excluded, "individual_prediction": False,
                "composition_policy_version": POLICY_VERSION,
                "release_id": metadata["release_id"], "expert_review": metadata["expert_review"]}

    attach_explorer(app, public=True)
    # Outermost context is inherited by endpoint worker threads and all Explorer routes.
    app.add_middleware(ExplorerContextMiddleware, paths={
        "NUTRIGENOMICS_EXPLORER_DATABASE": database,
        "NUTRIGENOMICS_EXPLORER_CORE_DATABASE": database,
        "NUTRIGENOMICS_EXPLORER_RIGHTS": release / "rights-policy.json",
    })
    return app


def app_factory():
    release = os.environ.get("NUTRIOMICS_PUBLIC_RELEASE")
    if not release:
        raise ValueError("NUTRIOMICS_PUBLIC_RELEASE is required; no internal database fallback")
    return create_app(Path(release), allow_candidate=os.environ.get("NUTRIOMICS_ALLOW_CANDIDATE") == "1")
