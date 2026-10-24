"""Read-only review/classification context for a composed public release.

Canonical science payloads keep their reviewed hashes. Decisions and source
checks are separate declarations; neither a source check nor a pending record
is human approval. Legacy releases retain their original response shape.
"""
from __future__ import annotations

import json

from .reporting_review import digest


def record_context(db, kind, identifier, record=None):
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('curation_record_digest','scientific_review_decision','scientific_review_dependency','source_classification')")}
    if "curation_record_digest" not in tables:
        return {}
    row = db.execute("SELECT record_sha256 FROM curation_record_digest WHERE kind=? AND id=?",
                     (kind, str(identifier))).fetchone()
    if row is None:
        return {}
    if record is not None and digest(record) != row[0]:
        raise ValueError("Serving science payload differs from its composed record hash")
    decision = None
    if "scientific_review_decision" in tables:
        found = db.execute("SELECT decision_json FROM scientific_review_decision "
                           "WHERE kind=? AND id=? AND record_sha256=?", (kind, str(identifier), row[0])).fetchone()
        decision = json.loads(found[0]) if found else None
    decision = decision or {"record_kind": kind, "record_id": str(identifier),
                            "record_sha256": row[0], "decision": "pending",
                            "reviewer_attestation": False}
    classification = None
    if "source_classification" in tables:
        found = db.execute("SELECT metadata_json FROM source_classification "
                           "WHERE kind=? AND id=? AND record_sha256=?", (kind, str(identifier), row[0])).fetchone()
        classification = json.loads(found[0]) if found else None
    result = {"canonical_record_sha256": row[0], "human_review": decision,
            "publication_disposition": {"accept": "accepted", "exclude": "excluded",
                "correct": "correction_required", "unconfirmed": "unconfirmed"}.get(decision["decision"], "pending"),
            "scientific_approval": decision["decision"] == "accept",
            "source_classification": classification}
    if "scientific_review_dependency" in tables:
        dependency = db.execute("SELECT dependency_json FROM scientific_review_dependency WHERE kind=? AND id=? AND record_sha256=?",
                                (kind, str(identifier), row[0])).fetchone()
        if dependency:
            result["source_dependency_binding"] = json.loads(dependency[0])
    if (decision["decision"] in {"exclude", "correct", "unconfirmed"}
            or (classification and classification.get("primary_dietary_evidence_eligible") is False)):
        result["primary_dietary_evidence_eligible"] = False
    return result


def study_context(db, identifier):
    row = db.execute("SELECT metadata_json FROM science_record WHERE kind='study' AND id=?",
                     (str(identifier),)).fetchone()
    return record_context(db, "study", identifier, json.loads(row[0])) if row else {}


def selector_context(db, accession, contrast, analysis_version):
    """Exact native selector context; no feature ID/chemical identity inference."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='curation_record_digest'").fetchone():
        return {}
    records = db.execute("SELECT id,metadata_json FROM science_record WHERE kind='contrast' "
        "AND json_extract(metadata_json,'$.series_accession')=? "
        "AND json_extract(metadata_json,'$.contrast')=? "
        "AND json_extract(metadata_json,'$.analysis_version')=? ORDER BY id",
        (accession, contrast, analysis_version)).fetchall()
    result = []
    for identifier, payload in records:
        value = json.loads(payload)
        result.append({"contrast_id": identifier,
            "contrast_review": record_context(db, "contrast", identifier, value),
            "study_review": study_context(db, value.get("study_id"))})
    return {"selector": [accession, contrast, analysis_version], "curated_contrasts": result,
            "individual_prediction": False}


def annotate_matches(db, evidence):
    """Add context after matching; original effect keys/values/order are untouched."""
    for item in evidence:
        context = study_context(db, item["study_id"])
        if not context:
            continue
        item["science_review_context"] = context
        if context.get("primary_dietary_evidence_eligible") is False:
            item["primary_dietary_evidence_eligible"] = False
        classification = context.get("source_classification")
        if classification:
            item["source_classification"] = classification
            # A source-only classification cannot override a restrictive human
            # disposition. The same restriction follows each study contrast.
            item["primary_dietary_evidence_eligible"] = (
                context.get("primary_dietary_evidence_eligible") is not False
                and classification["primary_dietary_evidence_eligible"])
            if "supplement_attribution_established" in classification:
                item["supplement_attribution_established"] = classification["supplement_attribution_established"]
        for contrast in item.get("contrasts", []):
            if item.get("primary_dietary_evidence_eligible") is False:
                contrast["primary_dietary_evidence_eligible"] = False
            own = record_context(db, "contrast", contrast["id"])
            if own:
                contrast["science_review_context"] = own
                if own.get("primary_dietary_evidence_eligible") is False:
                    contrast["primary_dietary_evidence_eligible"] = False
            if classification:
                contrast["study_source_classification"] = classification
    return evidence
