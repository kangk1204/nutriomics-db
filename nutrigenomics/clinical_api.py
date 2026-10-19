"""Read-only, source-bound clinical context; no mediation or individual prediction."""
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/v1/explorer", tags=["clinical-context"])


@router.get("/dictionary")
def data_dictionary():
    from .explorer import _meta, database
    from .release_contract import COUNT_DEFINITIONS
    from .reporting_review import clinical_data_dictionary
    with database() as db:
        tables = {}
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            name = row[0]
            quoted = '"' + name.replace('"', '""') + '"'
            tables[name] = [{"name": c[1], "sqlite_type": c[2], "not_null": bool(c[3]),
                "primary_key_order": c[5]} for c in db.execute("PRAGMA table_info(" + quoted + ")")]
        return {"release_id": _meta(db).get("release_id"), "tables": tables,
            "count_definitions": COUNT_DEFINITIONS, "clinical_subtypes": clinical_data_dictionary(db),
            "csv_null_convention": "Empty cells may represent NULL or an empty string; SQLite/Parquet preserve the distinction.",
            "scientific_approval": False, "individual_prediction": False}


@router.get("/studies/{study_id}/clinical")
def clinical_context(study_id: str):
    from .clinical_endpoints import study_clinical_summary
    from .explorer import _meta, database
    with database() as db:
        # Accept only explicitly registered studies, not arbitrary matching strings.
        row = db.execute("SELECT 1 FROM science_record WHERE kind='study' AND id=?", (study_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Study is not registered in this release")
        value = study_clinical_summary(db, study_id)
        return {**value, "study_id": study_id, "release_id": _meta(db).get("release_id"),
            "individual_prediction": False, "mediation_established": False}
