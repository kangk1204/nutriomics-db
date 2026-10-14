"""Read-only source annotations; graph reachability does not establish a meal effect."""
from __future__ import annotations

import json

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.orm import Session

_LAYERS = {"molecular_identity", "molecular_target", "pathway", "molecular_measurement"}
_SINGLE_NUTRIENTS = {"fdc-nutrient:1278", "fdc-nutrient:1272"}
_REVIEWED = {"source_checked", "expert_verified", "verified"}


def mechanisms_for_keys(session: Session, keys: set[str]) -> list[dict]:
    """Return bounded direct annotations with explicit chemical mapping scope.

    Missing optional tables are a normal state. Broader mappings and mixtures
    are never traversed. EPA/DHA analytical nutrients may resolve to qualified
    related references, never exact full-stereochemistry identities.
    Associations remain associations; their source-study context remains intact.
    """
    if not keys:
        return []
    required = {"ng_entity", "ng_mapping", "ng_evidence", "ng_release"}
    if not required <= set(inspect(session.connection()).get_table_names()):
        return []
    roots = session.execute(text("""SELECT id,kind,metadata_json FROM ng_entity
        WHERE lower(id) IN :keys OR lower(identifier) IN :keys""").bindparams(
            bindparam("keys", expanding=True)), {"keys": sorted(key.lower() for key in keys)}
    ).mappings().all()
    ids = {row["id"] for row in roots}
    related = {}
    for root in roots:
        metadata = json.loads(root["metadata_json"] or "{}")
        if metadata.get("is_mixture") or metadata.get("identity_scope") == "mixture":
            continue
        if root["kind"] == "nutrient" and (
            root["id"].lower() not in _SINGLE_NUTRIENTS
            and metadata.get("identity_scope") != "single_compound"
        ):
            continue
        mapped = session.execute(text("""SELECT to_id FROM ng_mapping
            WHERE from_id=:id AND relation='exact' AND review_status='verified'"""),
            {"id": root["id"]}).scalars().all()
        ids.update(mapped)
        if root["id"].lower() in _SINGLE_NUTRIENTS:
            for row in session.execute(text("""SELECT to_id,relation,review_status FROM ng_mapping
                WHERE from_id=:id AND relation='related' AND review_status='verified'"""),
                {"id": root["id"]}).mappings():
                ids.add(row["to_id"])
                related[row["to_id"]] = {"input_id": root["id"], "relation": "related",
                    "scope": "Analytical nutrient does not establish exact full chemical stereochemistry"}
    if not ids:
        return []
    rows = session.execute(text("""SELECT e.*, a.label AS subject_label,
        b.label AS object_label, r.source, r.version, r.url, r.sha256, r.license,
        r.retrieved_at, r.local_path, r.access_status
        FROM ng_evidence e JOIN ng_entity a ON a.id=e.subject_id
        JOIN ng_entity b ON b.id=e.object_id JOIN ng_release r ON r.id=e.release_id
        WHERE (e.subject_id IN :ids OR e.object_id IN :ids)
        AND e.layer IN :layers
        AND json_extract(e.context_json,'$.review_status') IN :reviewed
        ORDER BY e.id LIMIT 200""").bindparams(
            bindparam("ids", expanding=True), bindparam("layers", expanding=True),
            bindparam("reviewed", expanding=True)),
        {"ids": sorted(ids), "layers": sorted(_LAYERS), "reviewed": sorted(_REVIEWED)}).mappings().all()
    records = _records(rows)
    for row in records:
        if row["subject_id"] in related:
            row["identity_mapping"] = related[row["subject_id"]]
            row["limitations"] = [*row["limitations"],
                "Related chemical reference only; the nutrient quantity is not a measured exposure to this exact stereochemical compound."]
    return records


def _records(rows) -> list[dict]:
    records = []
    for row in rows:
        context = json.loads(row["context_json"] or "{}")
        if context.get("review_status", "candidate") not in _REVIEWED:
            continue
        records.append({
            "id": row["id"], "layer": row["layer"],
            "assertion": context.get("assertion", f"{row['subject_label']} {row['relation']} {row['object_label']}"),
            "source_url": context.get("source_url", row["url"]),
            "organism": row["organism"], "tissue": row["tissue"],
            "inference": row["inference"], "review_status": context.get("review_status", "candidate"),
            "export_allowed": bool(context.get("export_allowed", False)) and row["access_status"] == "public",
            "subject_id": row["subject_id"], "object_id": row["object_id"],
            "effect": row["effect"], "effect_unit": row["effect_unit"],
            "study_id": row["study_id"], "tier": row["tier"],
            "n_people": row["n_people"], "n_samples": row["n_samples"],
            "p_value": row["p_value"], "q_value": row["q_value"],
            "value_status": row["value_status"], "context": context,
            "provenance": {
                "release_id": row["release_id"], "source": row["source"],
                "version": row["version"], "sha256": row["sha256"],
                "license": row["license"], "source_ref": row["source_ref"],
                "retrieved_at": row["retrieved_at"], "local_path": row["local_path"],
            },
            "limitations": context.get("limitations", [
                "Does not predict a personal meal response or establish a causal health effect."
            ]),
        })
    return records


def search_annotations(session: Session, query: str | None = None, layer: str | None = None,
                       organism: str | None = None, tissue: str | None = None,
                       limit: int = 50, offset: int = 0) -> dict:
    """Paged source-reviewed direct annotations; metadata registries are not links."""
    if not 1 <= limit <= 200 or offset < 0 or (layer and layer not in _LAYERS):
        raise ValueError("invalid annotation filters or pagination")
    if not {"ng_entity", "ng_evidence", "ng_release"} <= set(inspect(session.connection()).get_table_names()):
        return {"total": 0, "records": [], "limit": limit, "offset": offset}
    clauses = ["e.layer IN :layers", "json_extract(e.context_json,'$.review_status') IN :reviewed"]
    parameters = {"layers": sorted(_LAYERS), "reviewed": sorted(_REVIEWED), "limit": limit, "offset": offset}
    if query:
        clauses.append("""(a.label LIKE :query ESCAPE '\\' OR b.label LIKE :query ESCAPE '\\'
            OR e.id LIKE :query ESCAPE '\\'
            OR json_extract(e.context_json,'$.source_row."eGene gene"') LIKE :query ESCAPE '\\')""")
        parameters["query"] = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    for field, value in [("layer", layer), ("organism", organism), ("tissue", tissue)]:
        if value:
            clauses.append(f"e.{field}=:{field}")
            parameters[field] = value
    where = " WHERE " + " AND ".join(clauses)
    joins = " FROM ng_evidence e JOIN ng_entity a ON a.id=e.subject_id JOIN ng_entity b ON b.id=e.object_id JOIN ng_release r ON r.id=e.release_id"
    count = text("SELECT count(*)" + joins + where).bindparams(
        bindparam("layers", expanding=True), bindparam("reviewed", expanding=True))
    total = session.execute(count, parameters).scalar_one()
    statement = text("""SELECT e.*,a.label AS subject_label,b.label AS object_label,
        r.source,r.version,r.url,r.sha256,r.license,r.retrieved_at,r.local_path,r.access_status"""
        + joins + where + " ORDER BY e.id LIMIT :limit OFFSET :offset").bindparams(
            bindparam("layers", expanding=True), bindparam("reviewed", expanding=True))
    rows = session.execute(statement, parameters).mappings().all()
    return {"total": total, "records": _records(rows), "limit": limit, "offset": offset}


def annotation_provenance(session: Session, evidence_id: str) -> dict | None:
    """Resolve one reviewed annotation or a frozen source release, never candidates."""
    if not {"ng_evidence", "ng_release"} <= set(inspect(session.connection()).get_table_names()):
        return None
    row = session.execute(text("""SELECT e.id AS evidence_id,e.source_ref,e.context_json,
        e.inference,e.organism,e.tissue,e.study_id,r.* FROM ng_evidence e
        JOIN ng_release r ON r.id=e.release_id WHERE e.id=:id"""), {"id": evidence_id}).mappings().first()
    if row is not None:
        context = json.loads(row["context_json"] or "{}")
        if context.get("review_status", "candidate") not in _REVIEWED:
            return None
        return {"id": row["id"], "evidence_id": row["evidence_id"], "url": row["url"],
            "source_url": context.get("source_url", row["url"]), "version": row["version"],
            "sha256": row["sha256"], "source_location": row["local_path"],
            "source_ref": row["source_ref"], "license": row["license"],
            "retrieved_at": row["retrieved_at"], "access_status": row["access_status"],
            "export_allowed": bool(context.get("export_allowed", False)) and row["access_status"] == "public",
            "review_status": context["review_status"], "study_id": row["study_id"],
            "inference": row["inference"], "organism": row["organism"], "tissue": row["tissue"],
            "assertion": context.get("assertion"), "context": context, "metadata": context,
            "hash_scope": "source_snapshot"}
    source = session.execute(text("SELECT * FROM ng_release WHERE id=:id"), {"id": evidence_id}).mappings().first()
    if source is None:
        return None
    # A registry source can have a provenance record without contributing links.
    return {"id": source["id"], "url": source["url"], "source_url": source["url"],
        "source": source["source"], "version": source["version"], "sha256": source["sha256"],
        "source_location": source["local_path"], "source_ref": None, "license": source["license"],
        "retrieved_at": source["retrieved_at"], "access_status": source["access_status"],
        "export_allowed": False, "review_status": "source_snapshot_only", "context": {},
        "metadata": {"source_registry_only": source["access_status"] == "metadata_only"},
        "hash_scope": "source_snapshot"}
