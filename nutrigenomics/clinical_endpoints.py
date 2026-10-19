"""Source-located clinical summaries, kept separate from genomic effects.

The layer records published descriptive or comparative summaries. A graph link
means that the endpoint was reported by the same study, never mediation,
individual prediction, or a gene-to-disease causal effect. The initial curation
is machine-assisted and deliberately remains pending human scientific review.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

from .explorer_build import ReleaseWriter, canonical

CLINICAL_SCHEMA_VERSION = 1
ALLOWED_DOMAINS = {"blood_pressure", "lipids", "obesity"}
RESULT_TYPES = {"baseline_summary", "repeated_group_summary", "crossover_iAUC_summary"}
STATISTIC_TYPES = {"median", "mean", "median_iAUC"}
AUDIT_STATES = {
    "quantitative_source_checked_candidate",
    "fulltext_quantitative_table_not_audited",
    "abstract_only_quantitative_semantics_not_audited",
    "metadata_only_clinical_results_not_audited",
}
_HASH = re.compile(r"[a-f0-9]{64}")


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(label + " must be a finite reported number")


def validate_clinical_config(config: dict) -> dict:
    """Fail closed on fabricated certainty, incompatible summaries or lost scope."""
    if config.get("schema_version") != CLINICAL_SCHEMA_VERSION:
        raise ValueError("unsupported clinical schema")
    if config.get("expert_review_status") != "pending":
        raise ValueError("machine-assisted clinical curation must remain expert pending")
    if config.get("human_approved_records") != 0:
        raise ValueError("human approval cannot be created by this importer")
    if not config.get("version"):
        raise ValueError("clinical version required")
    sources = {r["id"]: r for r in config.get("sources", [])}
    if len(sources) != len(config.get("sources", [])):
        raise ValueError("duplicate clinical source ID")
    for source in sources.values():
        if (not _HASH.fullmatch(source.get("sha256", ""))
                or not source.get("source_url", "").startswith("https://")
                or not source.get("version") or not source.get("license")):
            raise ValueError("clinical source must have URL, hash, version and rights")
        if source.get("export_allowed") is not True or source.get("license") != "CC-BY-4.0":
            raise ValueError("clinical fact projection requires checked affirmative source rights")
        if not source.get("rights_location"):
            raise ValueError("source-specific rights location required")
    audits = {r["study_id"]: r for r in config.get("study_audits", [])}
    if len(audits) != len(config.get("study_audits", [])):
        raise ValueError("duplicate clinical study audit")
    for audit in audits.values():
        if audit.get("status") not in AUDIT_STATES or audit.get("expert_review_status") != "pending":
            raise ValueError("invalid clinical study audit state")
        if audit.get("not_measured") is not None:
            raise ValueError("unassessed endpoints cannot be labelled not measured")
        if not audit.get("source_urls") or not audit.get("reason"):
            raise ValueError("clinical audit must explain source and missingness")
    identifiers = set()
    for record in config.get("records", []):
        if record.get("id") in identifiers or not record.get("id"):
            raise ValueError("duplicate or missing clinical record ID")
        identifiers.add(record["id"])
        if record.get("study_id") not in audits:
            raise ValueError("clinical study audit missing")
        if record.get("result_type") not in RESULT_TYPES:
            raise ValueError("unsupported clinical result type")
        if (record.get("expert_review_status") != "pending"
                or record.get("genomic_mediation_demonstrated") is not False
                or record.get("individual_prediction_available") is not False):
            raise ValueError("clinical context is not expert approval, mediation or individual prediction")
        if record.get("clinical_effect_estimate") is not None:
            raise ValueError("summary differences are not a reported causal effect estimate")
        if any(record.get(k) is not None for k in ("standard_error", "confidence_interval", "health_benefit_score")):
            raise ValueError("unreported uncertainty and health scores cannot be fabricated")
        endpoint = record.get("endpoint", {})
        if endpoint.get("domain") not in ALLOWED_DOMAINS or not endpoint.get("id") or not endpoint.get("unit"):
            raise ValueError("endpoint identity, domain and reported unit required")
        if not record.get("source_location") or not record.get("interpretation_limit"):
            raise ValueError("clinical record must preserve source location and interpretation limit")
        if not record.get("source_ids") or any(s not in sources for s in record["source_ids"]):
            raise ValueError("clinical source missing")
        references = record.get("source_references", [])
        if [r.get("id") for r in references] != record["source_ids"]:
            raise ValueError("offline clinical source references must match exact source IDs")
        for reference in references:
            source = sources[reference["id"]]
            if any(reference.get(k) != source.get(v) for k, v in (
                ("url", "source_url"), ("version", "version"), ("sha256", "sha256"), ("license", "license"))):
                raise ValueError("clinical source reference URL/version/hash/rights mismatch")
        if (not isinstance(record.get("n_people"), int) or isinstance(record["n_people"], bool)
                or record["n_people"] <= 0 or not record.get("n_people_scope")):
            raise ValueError("person count and scope required")
        observations = record.get("observations", [])
        if not observations:
            raise ValueError("quantitative clinical records require reported observations")
        for obs in observations:
            _number(obs.get("value"), "clinical value")
            if obs.get("summary_statistic") not in STATISTIC_TYPES or not obs.get("time_label") or not obs.get("arm"):
                raise ValueError("clinical statistic, arm and time scope required")
            dispersion = obs.get("dispersion")
            if dispersion is not None:
                kind = dispersion.get("kind")
                if kind in {"range", "IQR"}:
                    _number(dispersion.get("lower"), "dispersion lower")
                    _number(dispersion.get("upper"), "dispersion upper")
                    if not dispersion["lower"] <= obs["value"] <= dispersion["upper"]:
                        raise ValueError("reported range must contain reported summary")
                    if kind == "range" and obs["summary_statistic"] != "median":
                        raise ValueError("source median range cannot become mean SD")
                    if kind == "IQR" and obs["summary_statistic"] != "median_iAUC":
                        raise ValueError("source iAUC IQR must retain its estimand")
                elif kind == "SD":
                    _number(dispersion.get("value"), "reported SD")
                    if dispersion["value"] < 0 or obs["summary_statistic"] != "mean":
                        raise ValueError("SD must be nonnegative and accompany a mean")
                else:
                    raise ValueError("untyped dispersion cannot be reinterpreted as SE or CI")
        if record["result_type"] == "baseline_summary" and (
            len(observations) != 1 or observations[0].get("time_days") != 0 or record.get("statistical_tests")
        ):
            raise ValueError("baseline descriptors cannot be presented as intervention effects")
        if record["result_type"] == "crossover_iAUC_summary" and (
            endpoint.get("canonical_unit") is not None or not endpoint.get("unit_caveat")
        ):
            raise ValueError("source iAUC time unit is unreported; do not invent concentration-time units")
        for test in record.get("statistical_tests", []):
            if not test.get("comparison_scope") or not test.get("multiplicity"):
                raise ValueError("test comparison and multiplicity scope required")
            p = test.get("p_value")
            if p is not None:
                _number(p, "reported p value")
                if not 0 <= p <= 1:
                    raise ValueError("invalid reported p value")
            elif test.get("reported_label") not in {"ns", "not_reported"}:
                raise ValueError("missing p is not a numerical zero")
    return config


def load_clinical_config(path: Path | str) -> dict:
    return validate_clinical_config(json.loads(Path(path).read_text(encoding="utf-8")))


def _record(db, identifier, kind, value):
    db.execute("INSERT INTO science_record(id,kind,metadata_json) VALUES(?,?,?)",
               (identifier, kind, canonical(value)))


def attach_clinical_endpoints(db, config: dict | Path | str) -> dict:
    """Attach only to a new derived release; caller owns transaction and hashing.

    Existing effect/measurement tables are never read or written. Existing
    scientific records and graph rows are not replaced. Reinstallation is an
    error, preventing silent overwriting of later review decisions.
    """
    config = load_clinical_config(config) if isinstance(config, (Path, str)) else validate_clinical_config(config)
    present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    needed = {"science_record", "node", "edge", "source", "node_domain", "edge_domain"}
    if not needed <= present:
        raise ValueError("clinical attachment requires an existing scientific Explorer schema")
    if "workbench_evidence" in present:
        columns = [r[1] for r in db.execute("PRAGMA table_info(workbench_evidence)")]
        if columns != ["id", "subject_id", "object_id", "relation", "layer", "study_id",
                       "source_id", "tissue", "organism", "review_status", "export_allowed", "metadata_json"]:
            raise ValueError("unsupported clinical workbench evidence schema")
    if db.execute("SELECT 1 FROM science_record WHERE kind IN ('clinical_endpoint_result','clinical_study_audit') LIMIT 1").fetchone():
        raise ValueError("clinical layer already installed; create a new explicit version")
    studies = {str(r[0]) for r in db.execute("SELECT id FROM science_record WHERE kind='study'")}
    if any(a["study_id"] not in studies for a in config["study_audits"]):
        raise ValueError("clinical audit refers to an unregistered study")
    writer = ReleaseWriter(db)
    for source in config["sources"]:
        if db.execute("SELECT 1 FROM source WHERE id=?", (source["id"],)).fetchone():
            raise ValueError("clinical source namespace already installed")
        writer.source(source["id"], source)
    for audit in config["study_audits"]:
        _record(db, "ClinicalStudyAudit:" + audit["study_id"], "clinical_study_audit", audit)
    by_endpoint, by_study = defaultdict(list), defaultdict(list)
    for record in config["records"]:
        _record(db, record["id"], "clinical_endpoint_result", record)
        by_endpoint[record["endpoint"]["id"]].append(record)
        by_study[record["study_id"]].append(record)
    for identifier, records in by_endpoint.items():
        endpoint = records[0]["endpoint"]
        if db.execute("SELECT 1 FROM node WHERE id=?", (identifier,)).fetchone():
            raise ValueError("clinical endpoint ID conflict")
        writer.node(identifier, "outcome", endpoint["label_ko"], {
            "entity_type": "measured_clinical_endpoint",
            "endpoint": {k: endpoint[k] for k in ("id", "label", "label_ko", "domain")},
            "reported_units": sorted({r["endpoint"]["unit"] for r in records}),
            "source_ids": sorted({s for r in records for s in r["source_ids"]}),
            "clinical_endpoints": records, "clinical_layer_version": config["version"],
            "expert_review_status": "pending", "genomic_mediation_demonstrated": False,
            "individual_prediction_available": False,
        }, aliases=[endpoint["label"]], label_en=endpoint["label"], domains=[endpoint["domain"]])
    for study_id, records in by_study.items():
        context_id = "ClinicalStudy:" + study_id
        if db.execute("SELECT 1 FROM node WHERE id=?", (context_id,)).fetchone():
            raise ValueError("clinical study context ID conflict")
        domains = sorted({r["endpoint"]["domain"] for r in records})
        writer.node(context_id, "diet", study_id + " · 임상 결과를 보고한 연구", {
            "entity_type": "study_context", "study_id": study_id,
            "clinical_endpoints": records, "expert_review_status": "pending",
            "source_ids": sorted({s for r in records for s in r["source_ids"]}),
            "clinical_layer_version": config["version"],
            "note": "같은 연구의 결과를 함께 보여주는 연결이며 유전자 매개·인과 효과를 뜻하지 않습니다.",
        }, domains=domains)
        sources = sorted({s for r in records for s in r["source_ids"]})
        for row in db.execute("SELECT id,metadata_json FROM science_record WHERE kind='exposure'"):
            exposure = json.loads(row[1])
            exposure_id = "Exposure:" + str(row[0])
            if exposure.get("study_id") != study_id or not db.execute("SELECT 1 FROM node WHERE id=?", (exposure_id,)).fetchone():
                continue
            writer.edge(exposure_id, context_id, "same_study_clinical_context", "observed_intervention", {
                "study_id": study_id, "relationship_semantics": "same_study_context_not_causal",
                "causality_identified": False, "genomic_mediation_demonstrated": False,
                "expert_review_status": "pending", "clinical_layer_version": config["version"],
            }, sources, taxon="9606", review_status="pending", domains=domains,
                identifier="ClinicalContext:" + hashlib.sha256(exposure_id.encode()).hexdigest())
        for record in records:
            context = {
                "clinical_endpoint_result_id": record["id"], "study_id": study_id,
                "result_type": record["result_type"], "source_location": record["source_location"],
                "relationship_semantics": "published_clinical_summary_not_genomic_causality",
                "expert_review_status": "pending", "genomic_mediation_demonstrated": False,
                "individual_prediction_available": False,
                "clinical_endpoints": [record],
            }
            edge_id = "ClinicalResultEdge:" + record["id"]
            writer.edge(context_id, record["endpoint"]["id"], "reported_clinical_endpoint", "observed", context,
                record["source_ids"], pmids=[record["pmid"]], taxon="9606",
                review_status="pending", domains=[record["endpoint"]["domain"]],
                identifier=edge_id)
            if "workbench_evidence" in present:
                payload = {
                    "id": edge_id, "subject_id": context_id, "object_id": record["endpoint"]["id"],
                    "relation": "reported_clinical_endpoint", "layer": "clinical", "study_id": study_id,
                    "context": context, "clinical_endpoints": [record], "result_type": record["result_type"],
                    "n_people": record["n_people"], "n_people_scope": record["n_people_scope"],
                    "effect": None, "effect_estimate": None, "effect_unit": None,
                    "p_value": None, "q_value": None, "standard_error": None,
                    "ci_low": None, "ci_high": None, "n_samples": None,
                    "hypothesis_family": "Separate original test scopes in clinical_endpoints[].statistical_tests; no single pooled test",
                    "hypothesis_comparisons": record["statistical_tests"],
                    "value_status": "source_reported_summary_no_single_effect_scalar",
                    "source_ref": record["source_location"], "source_references": record["source_references"],
                    "study": {"id": study_id, "clinical_context_only": True},
                    "expert_review_status": "pending", "export_allowed": True,
                }
                db.execute("INSERT INTO workbench_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                    edge_id, context_id, record["endpoint"]["id"], "reported_clinical_endpoint", "clinical",
                    study_id, record["source_ids"][0], None, "9606", "source_checked_expert_pending", 1,
                    canonical(payload)))
    # Update only domains/degrees for affected graph nodes; keep inherited labels
    # and existing relation payloads intact. Global search indexes are rebuilt by
    # the caller after all metadata-only layer additions.
    for node_id, domains in writer.domains.items():
        row = db.execute("SELECT domains_json FROM node WHERE id=?", (node_id,)).fetchone()
        merged = sorted(set(json.loads(row[0])) | set(domains))
        db.execute("UPDATE node SET domains_json=?,degree=(SELECT count(*) FROM edge WHERE subject=?)+(SELECT count(*) FROM edge WHERE object=?) WHERE id=?",
                   (canonical(merged), node_id, node_id, node_id))
        db.executemany("INSERT OR IGNORE INTO node_domain VALUES(?,?)", [(node_id, d) for d in merged])
    db.executemany("INSERT OR IGNORE INTO edge_domain VALUES(?,?)", [
        (row[0], d) for row in db.execute("SELECT id,domains_json FROM edge WHERE id LIKE 'ClinicalResultEdge:%' OR id LIKE 'ClinicalContext:%'")
        for d in json.loads(row[1])])
    summary = {"version": config["version"], "records": len(config["records"]),
               "study_audits": len(config["study_audits"]), "studies_with_quantitative_candidates": len(by_study),
               "health_endpoint_nodes": len(by_endpoint), "expert_review_status": "pending",
               "workbench_clinical_rows": len(config["records"]) if "workbench_evidence" in present else 0,
               "human_approved_records": 0,
               "result_types": dict(Counter(r["result_type"] for r in config["records"])),
               "effect_and_measurement_rows_modified": 0,
               "interpretation": "Source-checked machine-assisted candidates; same-study context is not mediation or health prediction."}
    if "meta" in present:
        db.execute("INSERT OR REPLACE INTO meta(key,value_json) VALUES(?,?)", ("clinical_endpoints", canonical(summary)))
    return summary


def study_clinical_summary(db, study_id: str) -> dict:
    """Bounded study detail helper; missing schema and unassessed are not zero effects."""
    available = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='science_record'").fetchone()
    if not available:
        return {"clinical_endpoints": [], "clinical_endpoint_audit": None,
                "review_summary": {"expert_review_status": "pending", "human_approved_records": 0,
                                   "availability_reason": "clinical_layer_not_installed"}}
    records = [json.loads(row[0]) for row in db.execute(
        "SELECT metadata_json FROM science_record WHERE kind='clinical_endpoint_result' AND json_extract(metadata_json,'$.study_id')=? ORDER BY id", (study_id,))]
    row = db.execute("SELECT metadata_json FROM science_record WHERE kind='clinical_study_audit' AND id=?", ("ClinicalStudyAudit:" + study_id,)).fetchone()
    audit = json.loads(row[0]) if row else None
    return {"clinical_endpoints": records, "clinical_endpoint_audit": audit,
            "review_summary": {"expert_review_status": "pending", "human_approved_records": 0,
                               "source_checked_candidate_records": len(records),
                               "availability_reason": audit["status"] if audit else "clinical_study_not_audited",
                               "same_study_context_is_not_mediation": True}}
