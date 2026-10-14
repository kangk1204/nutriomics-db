"""Read-only public deployment. No collection, write, or analysis routes are installed."""
from __future__ import annotations

import base64
import json
import math
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, FiniteFloat

from .explorer import attach_explorer
from .explorer_build import canonical, sha256


class PublicMealItem(BaseModel):
    food_id: str | int
    amount_g: FiniteFloat = Field(gt=0, le=20000)


class PublicMeal(BaseModel):
    items: list[PublicMealItem] = Field(min_length=1, max_length=100)


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
    os.environ["NUTRIGENOMICS_EXPLORER_DATABASE"] = str(database)
    os.environ["NUTRIGENOMICS_EXPLORER_CORE_DATABASE"] = str(database)
    app = FastAPI(title="NutriOmics public data", version=metadata["release_id"])

    @app.middleware("http")
    async def release_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-NutriOmics-Release"] = metadata["release_id"]
        response.headers["X-NutriOmics-Scope"] = "public"
        return response

    @contextmanager
    def read():
        db = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        try:
            yield db
        finally:
            db.close()

    @app.get("/healthz")
    def health():
        return {"status": "ok", "release_id": metadata["release_id"], "scope": "public_only"}

    @app.get("/api/v1/public/meta")
    def meta():
        return metadata

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
            return {"items": page, "total_selector": total, "effects_status": "available" if total is not None else "not_in_public_release", "next_cursor": base64.urlsafe_b64encode(canonical([scope, page[-1]["feature_type"], page[-1]["feature_id"]]).encode()).decode() if more else None,
                    "release_id": metadata["release_id"], "expert_review": metadata["expert_review"]}

    @app.post("/api/v1/meals/analyze")
    def meal(payload: PublicMeal):
        totals, provenance, excluded, selected = {}, [], [], []
        with read() as db:
            for item in payload.items:
                food_id = str(item.food_id)
                if isinstance(item.food_id, int) or food_id.isdecimal():
                    mapped = db.execute("SELECT food_id FROM food_alias WHERE legacy_food_id=?", (int(item.food_id),)).fetchone()
                    if not mapped:
                        raise HTTPException(404, "food is outside the public release; this is not evidence of absence")
                    food_id = mapped[0]
                food = db.execute("SELECT id,label FROM node WHERE id=? AND kind='food'", (food_id,)).fetchone()
                if food is None:
                    raise HTTPException(404, "food not in public release")
                selected.append({"food_id": food_id, "amount_g": item.amount_g, "name": food["label"]})
                for row in db.execute("SELECT * FROM measurement WHERE food=? ORDER BY id", (food_id,)):
                    if row["amount"] is None or row["basis"] != "source_100g":
                        excluded.append({"measurement_id": row["id"], "status": row["status"], "reason": "missing_value_or_non_100g_basis"})
                        continue
                    amount = row["amount"] * item.amount_g / 100
                    if not math.isfinite(amount):
                        raise HTTPException(503, "non-finite source value")
                    key = (row["component"], row["unit"])
                    totals[key] = totals.get(key, 0.0) + amount
                    provenance.append({"measurement_id": row["id"], "source_id": row["source_id"], "basis": row["basis"], "status": row["status"], "contribution": amount, "unit": row["unit"]})
            components = []
            for (key, unit), value in sorted(totals.items()):
                node = db.execute("SELECT label,metadata_json FROM node WHERE id=?", (key,)).fetchone()
                info = json.loads(node["metadata_json"]) if node else {}
                components.append({"component_key": info.get("component_key", key), "name": node["label"] if node else key, "unit": unit, "amount": value})
        return {"items": selected, "components": components, "provenance": provenance,
                "excluded_measurements": excluded, "individual_prediction": False,
                "release_id": metadata["release_id"], "expert_review": metadata["expert_review"]}

    attach_explorer(app)
    return app


def app_factory():
    release = os.environ.get("NUTRIOMICS_PUBLIC_RELEASE")
    if not release:
        raise ValueError("NUTRIOMICS_PUBLIC_RELEASE is required; no internal database fallback")
    return create_app(Path(release), allow_candidate=os.environ.get("NUTRIOMICS_ALLOW_CANDIDATE") == "1")
