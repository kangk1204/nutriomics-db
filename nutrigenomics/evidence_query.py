"""Read-only queries preserve evidence layers and candidate mapping labels."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

LAYERS = {
    "composition",
    "molecular_identity",
    "molecular_target",
    "pathway",
    "molecular_measurement",
    "health_outcome",
}


def query(
    database: Path,
    subject_id: str | None = None,
    layer: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    if not database.is_file():
        raise FileNotFoundError("evidence database not found")
    if layer is not None and layer not in LAYERS:
        raise ValueError("unknown evidence layer")
    if not 1 <= limit <= 1000 or offset < 0:
        raise ValueError("invalid bounded pagination")
    conditions, parameters = [], []
    if subject_id is not None:
        conditions.append("e.subject_id=?")
        parameters.append(subject_id)
    if layer is not None:
        conditions.append("e.layer=?")
        parameters.append(layer)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        total = db.execute("SELECT count(*) FROM ng_evidence e" + where, parameters).fetchone()[0]
        rows = db.execute(
            """SELECT e.*,a.label AS subject_label,b.label AS object_label,
                             r.source,r.version,r.url,r.retrieved_at,r.sha256,r.license,
                             s.design,s.population,s.participant_group,s.independence_status
                             FROM ng_evidence e JOIN ng_entity a ON e.subject_id=a.id
                             JOIN ng_entity b ON e.object_id=b.id
                             JOIN ng_release r ON e.release_id=r.id
                             LEFT JOIN ng_study s ON e.study_id=s.id"""
            + where
            + " ORDER BY e.id LIMIT ? OFFSET ?",
            parameters + [limit, offset],
        ).fetchall()
        records = [dict(row) for row in rows]
        for row in records:
            for field in ["context_json", "covariates_json"]:
                row[field.removesuffix("_json")] = json.loads(row.pop(field))
        mappings = []
        if subject_id:
            mappings = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM ng_mapping WHERE from_id=? OR to_id=? ORDER BY id LIMIT 100",
                    (subject_id, subject_id),
                )
            ]
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "records": records,
        "direct_mappings": mappings,
        "transitive_health_inference": False,
        "candidate_mappings_require_review": True,
    }
