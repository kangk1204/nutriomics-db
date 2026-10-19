"""One release-bound inventory, shared by APIs and publication exports.

Build/install only in a new derived release before hashing it. Request handlers
read the installed inventory; legacy read-only releases use a fingerprint-bound
fallback cache. A database record count is never a participant count.
"""
from __future__ import annotations

import copy
import json
from functools import lru_cache

from .explorer_performance import fingerprint, logical_connection_path, read_connection

SCHEMA_VERSION = "nutriomics-release-contract-v1"
CORE_KINDS = {"study", "exposure", "arm", "contrast", "assertion", "analysis_run"}
COUNT_DEFINITIONS = {
    "node": "Graph identifier records, not distinct biological entities or people.",
    "edge": "Source relationships grouped by evidence tier; not validated causal food effects.",
    "measurement": "Food-component source records, including derived, zero and missing states.",
    "source": "Registered source records; registration does not imply unrestricted redistribution.",
    "composition_backed_foods": "Distinct food identifiers referenced by composition records; not a count of quantified analytes.",
    "effect": "Feature-by-contrast-by-analysis-version quantitative rows; not independent participants.",
    "indexed_effect_rows": "Rows covered by the local performance selector, possibly a subset of source results.",
    "science_record": "Curation records, counted separately by record kind.",
    "study_registry_records": "Study registry groups; trial independence and participant overlap are unaudited.",
    "reanalyzed_study_groups": "Registry groups with at least one completed analysis series; not independent replication cohorts.",
    "unique_pmids": "Distinct cited PubMed identifiers, not independent studies or acquired full texts.",
    "independent_studies": "Null until trial membership and cohort overlap have been audited.",
    "independent_people": "Null until participant overlap across studies and modalities has been audited.",
    "biological_genes": "Null until identifier nodes are reconciled without counting aliases as separate genes.",
    "pharmacological_contrasts": "Explicit drug-comparator contrasts; excluded from dietary-effect interpretation.",
    "candidate_documents": "Registered citation/source documents; not expert-reviewed assertions or independent trials.",
    "automatically_extracted_assertions": "Source-registered automatic candidates, including withheld candidates; not approved scientific facts.",
    "withheld_literature_assertions": "Release-wide restricted candidate count, not relevance to the current search seed and not evidence absence.",
    "dmr": "Preserved source feature type; fixed-window region tests are not automatically discovered or validated DMRs.",
}


def _json(value, default=None):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return default


def _meta(db):
    if "meta" not in _tables(db):
        return {}
    return {row[0]: _json(row[1]) for row in db.execute("SELECT key,value_json FROM meta")}


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _group(db, table, column):
    return dict(db.execute(f"SELECT {column},count(*) FROM {table} GROUP BY {column}"))


def build_release_contract(db, *, release_id=None, scope=None, previous_metadata=None,
                           manifest=None, quantitative_db=None, verify_effect_counts=True):
    """Compute actual inventory once. Does not change the supplied connections.

    The optional quantitative_db is the frozen internal core. The public native
    effect selector is a small exact aggregate; build-time verification compares
    its total with the native effect table. Legacy API startup skips that large
    scan, records the basis explicitly, and never repeats it per request.
    """
    original = _meta(db)
    prior = {**original, **(previous_metadata or {})}
    manifest = manifest or {}
    current = release_id or original.get("release_id") or prior.get("release_id")
    if not isinstance(current, str) or not current.strip():
        raise ValueError("a non-empty current release_id is required")
    tables = _tables(db)
    counts = {name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
              for name in ("node", "edge", "measurement", "source", "science_record")
              if name in tables}
    for singular, plural in (("node", "nodes"), ("edge", "edges"), ("measurement", "measurements")):
        counts[plural] = counts.get(singular)
    counts["by_kind"] = _group(db, "node", "kind") if "node" in tables else {}
    counts["node_kinds"] = dict(counts["by_kind"])
    counts["by_relation"] = _group(db, "edge", "relation") if "edge" in tables else {}
    counts["by_tier"] = _group(db, "edge", "tier") if "edge" in tables else {}
    counts["relation_tiers"] = dict(counts["by_tier"])
    counts["science_kinds"] = _group(db, "science_record", "kind") if "science_record" in tables else {}
    counts["workbench_by_layer"] = _group(db, "workbench_evidence", "layer") if "workbench_evidence" in tables else {}
    if "candidate_document" in tables:
        counts["candidate_documents"] = db.execute("SELECT count(*) FROM candidate_document").fetchone()[0]
        counts["automatically_extracted_assertions"] = db.execute("SELECT coalesce(sum(candidate_count),0) FROM candidate_document").fetchone()[0]
        counts["withheld_literature_assertions"] = db.execute(
            "SELECT coalesce(sum(candidate_count),0) FROM candidate_document WHERE json_extract(metadata_json,'$.assertion_visibility')='withheld'").fetchone()[0]
    counts["composition_backed_foods"] = (db.execute("SELECT count(DISTINCT food) FROM measurement").fetchone()[0]
                                               if "measurement" in tables else None)
    counts["unique_pmids"] = (db.execute("SELECT count(DISTINCT j.value) FROM edge e,json_each(e.pmids_json) j").fetchone()[0]
                              if "edge" in tables else None)
    count_basis = {"graph_and_curation": "actual release tables, build-time inventory"}
    if "effect_selector" in tables:
        counts["effect_selector"] = db.execute("SELECT count(*) FROM effect_selector").fetchone()[0]
        counts["effect_types"] = dict(db.execute("SELECT feature_type,sum(total) FROM effect_selector GROUP BY feature_type"))
        counts["effect"] = sum(counts["effect_types"].values())
        count_basis["effect"] = "sum of native effect_selector.total"
        if verify_effect_counts and "effect" in tables:
            actual = db.execute("SELECT count(*) FROM effect").fetchone()[0]
            if actual != counts["effect"]:
                raise ValueError("effect selector total differs from actual effect rows")
            count_basis["effect"] += "; verified against actual effect row count"
    if "perf_effect_selector" in tables:
        counts["indexed_effect_rows"] = db.execute("SELECT coalesce(sum(total),0) FROM perf_effect_selector").fetchone()[0]
        count_basis["indexed_effect_rows"] = "sum of local perf_effect_selector.total; not a full-core count"
    if quantitative_db is not None:
        qt = _tables(quantitative_db)
        table = "study_effect" if "study_effect" in qt else "effect" if "effect" in qt else None
        if table is None:
            raise ValueError("quantitative source has no effect table")
        counts["effect_types"] = _group(quantitative_db, table, "feature_type")
        counts["effect"] = sum(counts["effect_types"].values())
        count_basis["effect"] = f"actual frozen quantitative source {table} rows grouped by feature_type"
    counts.setdefault("effect", None)
    counts.setdefault("effect_types", {})
    counts.update(independent_studies=None, independent_people=None, biological_genes=None)

    records = [(kind, _json(payload, {})) for kind, payload in
               db.execute("SELECT kind,metadata_json FROM science_record")] if "science_record" in tables else []
    studies = [value for kind, value in records if kind == "study"]
    runs = [value for kind, value in records if kind == "analysis_run"]
    series = sorted({value.get("series_accession") for value in runs
                     if value.get("status") == "completed" and value.get("series_accession")})
    reanalyzed, metadata_only = [], []
    for study in studies:
        accessions = study.get("metadata", {}).get("series", study.get("series", []))
        (reanalyzed if set(accessions).intersection(series) else metadata_only).append(study.get("id"))
    counts["study_registry_records"] = len(studies)
    counts["reanalyzed_study_groups"] = len(reanalyzed)
    counts["pharmacological_contrasts"] = sum(
        value.get("context_type", value.get("comparison_kind")) == "pharmacological_comparator"
        or (value.get("series_accession") == "GSE27385" and value.get("contrast") == "fenofibrate_vs_placebo_6weeks")
        for kind, value in records if kind == "contrast")
    core = [(kind, value) for kind, value in records if kind in CORE_KINDS]
    # Source-checked facts, producer release approvals and publication status
    # strings alone are not human scientific review evidence.
    review = {"expert_review_status": "pending", "core_records": len(core),
              "expert_approved_records": None, "approval_count_status": "not_audited",
              "records_by_kind": {kind: counts["science_kinds"].get(kind, 0) for kind in sorted(CORE_KINDS)},
              "independent_review": False,
              "additional_clinical_records": sum(kind in {"clinical_endpoint_result", "clinical_study_audit"} for kind, _ in records),
              "reason": "Human review requires a separate record-hash-bound decision audit; source checks are not approval."}
    ancestry = list(copy.deepcopy(prior.get("provenance", {}).get("ancestry", [])))
    parent_id = manifest.get("release_id") if manifest.get("release_id") != current else None
    if parent_id is None:
        parent_id = prior.get("release_id") if prior.get("release_id") != current else prior.get("parent_release_id")
    if parent_id:
        parent = {"release_id": parent_id, "database_sha256": (
            manifest.get("database_sha256") if manifest.get("release_id") == parent_id else
            prior.get("parent_database_sha256") if prior.get("parent_release_id") == parent_id else None)}
        if parent not in ancestry:
            ancestry.append(parent)
    legacy = {key: value for key, value in prior.items()
              if key in ("release_id", "dataset_release_id", "service_build_id", "parent_release_id",
                         "derived_from_release", "public_service_parent") and value != current}
    contract = {
        "schema_version": SCHEMA_VERSION, "release_id": current,
        "dataset_release_id": current, "service_build_id": current,
        "scope": scope or prior.get("scope", "research_only"), "counts": counts,
        "graph_counts": {key: counts[key] for key in ("node", "edge", "measurement", "by_kind", "by_tier", "by_relation") if key in counts},
        "count_definitions": dict(COUNT_DEFINITIONS), "study_scope": {
            "registered_studies": len(studies), "reanalyzed_study_groups": len(reanalyzed),
            "reanalyzed_study_ids": sorted(value for value in reanalyzed if value),
            "metadata_only_study_ids": sorted(value for value in metadata_only if value),
            "analysis_series": series, "analysis_runs": len(runs), "independent_trials_audited": False},
        "review_summary": review, "expert_review": "pending",
        "status": "candidate_pending_expert_review", "submission_ready": False,
        "doi": prior.get("doi"), "published": prior.get("published", False),
        "provenance": {"ancestry": ancestry, "legacy_release_identifiers": legacy,
                       "count_basis": count_basis, "inventory_schema": SCHEMA_VERSION},
    }
    if parent_id:
        contract.update(parent_release_id=parent_id, parent_database_sha256=parent["database_sha256"])
    return contract


def install_release_contract(db, contract):
    """Write metadata only in a derived staging DB; caller owns commit/hash."""
    if contract.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported release contract")
    values = {**contract, "release_contract": contract}
    if "public_counts" in _meta(db):
        values["public_counts"] = contract["counts"]
    for key, value in values.items():
        db.execute("INSERT OR REPLACE INTO meta(key,value_json) VALUES(?,?)",
                   (key, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))


@lru_cache(maxsize=16)
def _legacy_contract(path, identity, release_id=None):
    with read_connection(path) as db:
        return build_release_contract(db, release_id=release_id, verify_effect_counts=False)


def metadata_with_contract(db, metadata=None):
    """Normalize aliases using the installed build-time inventory, read only."""
    stored = _meta(db)
    # Staging has already written its new graph/index metadata to the DB.
    # A previous metadata file can fill missing attachments, never override it.
    result = {**(metadata or {}), **stored}
    contract = stored.get("release_contract")
    if not isinstance(contract, dict) or contract.get("schema_version") != SCHEMA_VERSION:
        path = logical_connection_path(db)
        release_id = stored.get("release_id") or result.get("release_id")
        contract = (_legacy_contract(path, fingerprint(path), release_id) if path else
                    build_release_contract(db, release_id=release_id, verify_effect_counts=False))
    result.update(copy.deepcopy(contract))
    if "public_counts" in result:
        result["public_counts"] = copy.deepcopy(contract["counts"])
    # The same logical inventory object appears under historical aliases.
    result["release_contract"] = copy.deepcopy(contract)
    return result
