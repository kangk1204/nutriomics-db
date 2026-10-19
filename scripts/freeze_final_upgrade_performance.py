"""Freeze inherited performance cases plus source-bound final-upgrade regressions.

No source database is changed and no HTTP job is started. The optional response
verification reads the benchmark's saved JSON responses, not clinical gold labels.
"""
from __future__ import annotations

import argparse
import copy
import json
import tempfile
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from nutrigenomics.clinical_endpoints import load_clinical_config
from nutrigenomics.explorer_build import read_only, sha256
from nutrigenomics.explorer_performance import fingerprint
from nutrigenomics.source_updates import atomic_json
from scripts.benchmark_explorer_performance import digest
from scripts.freeze_candidate_performance import freeze_candidates

SCHEMA = "nutriomics-final-upgrade-performance-v1"
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config/clinical_endpoints.v1.json"
STUDIES = ("N15", "S04", "S05")
HEALTH_SEEDS = (
    ("blood-pressure", "blood_pressure", "ClinicalEndpoint:systolic_blood_pressure"),
    ("lipids", "lipids", "ClinicalEndpoint:ldl_cholesterol"),
    ("obesity", "obesity", "ClinicalEndpoint:bmi"),
)


def _load_spec(path):
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    if digest(spec["cases"]) != spec.get("case_sha256"):
        raise ValueError("Frozen case SHA mismatch")
    identifiers = [case["id"] for case in spec["cases"]]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Duplicate frozen case IDs")
    return spec


def _native_clinical(db, config):
    records = {row[0]: json.loads(row[1]) for row in db.execute(
        "SELECT id,metadata_json FROM science_record WHERE kind='clinical_endpoint_result'")}
    configured = {row["id"]: row for row in config["records"]}
    if not configured or set(records) != set(configured):
        raise ValueError("Expected clinical result records are absent or differ from the fixed configuration")
    if any(digest(records[key]) != digest(value) for key, value in configured.items()):
        raise ValueError("Clinical result values differ from the fixed source configuration")
    audits = {row[0]: json.loads(row[1]) for row in db.execute(
        "SELECT id,metadata_json FROM science_record WHERE kind='clinical_study_audit'")}
    for audit in config["study_audits"]:
        if audits.get("ClinicalStudyAudit:" + audit["study_id"]) != audit:
            raise ValueError("Expected clinical study audit is absent or changed")
    by_study, by_endpoint = defaultdict(dict), defaultdict(dict)
    for key, value in records.items():
        by_study[value["study_id"]][key] = digest(value)
        by_endpoint[value["endpoint"]["id"]][key] = digest(value)
        node = db.execute("SELECT kind,metadata_json FROM node WHERE id=?", (value["endpoint"]["id"],)).fetchone()
        if node is None or node[0] != "outcome":
            raise ValueError("Expected measured health endpoint node is absent")
        native_node = json.loads(node[1])
        if not any(r.get("id") == key and digest(r) == digest(value) for r in native_node.get("clinical_endpoints", [])):
            raise ValueError("Expected health node lacks its original clinical result")
        edge_id = "ClinicalResultEdge:" + key
        edge = db.execute("SELECT object,tier,relation FROM edge WHERE id=?", (edge_id,)).fetchone()
        if edge is None or tuple(edge) != (value["endpoint"]["id"], "observed", "reported_clinical_endpoint"):
            raise ValueError("Expected observed clinical graph relation is absent")
        wb = db.execute("SELECT layer,study_id,metadata_json FROM workbench_evidence WHERE id=?", (edge_id,)).fetchone()
        if wb is None or wb[0] != "clinical" or wb[1] != value["study_id"]:
            raise ValueError("Expected clinical evidence-filter record is absent")
        payload = json.loads(wb[2])
        if not any(r.get("id") == key and digest(r) == digest(value) for r in payload.get("clinical_endpoints", [])):
            raise ValueError("Clinical evidence lacks its original result")
    if any(not by_study.get(study) for study in STUDIES):
        raise ValueError("Required N15/S04/S05 clinical results are absent")
    for _, _, seed in HEALTH_SEEDS:
        if not by_endpoint.get(seed):
            raise ValueError("Required clinical health graph seed is absent")
    return records, audits, dict(by_study), dict(by_endpoint)


def freeze_final_upgrade(release, output, public=False, clinical_config=DEFAULT_CONFIG, baseline_spec=None):
    release, output, clinical_config = (Path(p).resolve() for p in (release, output, clinical_config))
    if output.exists():
        raise FileExistsError("Frozen performance cases already exist; use a new output")
    if output.is_relative_to(release):
        raise ValueError("Frozen cases must be outside the immutable release")
    config = load_clinical_config(clinical_config)
    database = release / ("nutriomics.sqlite3" if public else "explorer.sqlite")
    source_identity = fingerprint(database)
    manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
    with closing(read_only(database)) as db:
        metadata = {row[0]: json.loads(row[1]) for row in db.execute("SELECT key,value_json FROM meta")}
        release_id = metadata["release_id"]
        if manifest.get("release_id") != release_id:
            raise ValueError("Current DB and manifest release IDs disagree")
        records, audits, by_study, by_endpoint = _native_clinical(db, config)
        kinds = dict(db.execute("SELECT kind,count(*) FROM node GROUP BY kind"))
        science_kinds = dict(db.execute("SELECT kind,count(*) FROM science_record GROUP BY kind"))
    output.parent.mkdir(parents=True, exist_ok=True)
    # The inherited freezer writes intermediate specs. Keep these temporary so
    # failed validation cannot leave a misleading final frozen artifact.
    with tempfile.TemporaryDirectory(prefix=".freeze-final-upgrade-", dir=output.parent) as temp:
        spec = freeze_candidates(release, Path(temp) / "inherited.json", public)
    inherited = copy.deepcopy(spec["cases"])
    if spec.get("database_sha256") != manifest.get("database_sha256"):
        raise ValueError("Current DB hash differs from its immutable release manifest")
    prefix = {case["id"]: case for case in spec["cases"]}
    baseline = _load_spec(baseline_spec) if baseline_spec else None
    if baseline:
        for case in baseline["cases"]:
            if case["id"] in prefix and prefix[case["id"]] != case:
                raise ValueError("Inherited frozen case changed: " + case["id"])
            if case["id"] not in prefix:
                spec["cases"].append(copy.deepcopy(case))
                prefix[case["id"]] = case
    baseline_sha = digest(spec["cases"])

    def add(identifier, path, params=None, target=1, **expectation):
        if identifier in prefix:
            raise ValueError("Final-upgrade case ID already exists: " + identifier)
        spec["cases"].append({"id": identifier, "method": "GET", "path": path,
            "params": params or {}, "target_s": target,
            "expected_response": {"release_id": release_id, **expectation}})
        prefix[identifier] = spec["cases"][-1]

    add("final-canonical-meta", "/api/v1/explorer/meta", kind="meta",
        by_kind=kinds, science_kinds=science_kinds)
    for study in STUDIES:
        add("clinical-study-" + study.lower(), "/api/v1/explorer/studies/" + study + "/clinical",
            kind="clinical_study", study_id=study, records=by_study[study])
        if public:
            add("public-study-" + study.lower(), "/api/v1/studies/" + study,
                kind="clinical_study", study_id=study, records=by_study[study])
    for label, domain, seed in HEALTH_SEEDS:
        params = {"seed_id": seed, "tier": "observed", "domain": domain}
        edge_ids = ["ClinicalResultEdge:" + key for key in sorted(by_endpoint[seed])]
        for suffix, caps, target in (("default", {}, 1), ("maximum", {"max_nodes": 250, "max_edges": 750}, 3)):
            add("clinical-" + label + "-" + suffix, "/api/v1/explorer/graph", {**params, **caps}, target,
                kind="clinical_graph", seed_id=seed, required_edge_ids=edge_ids,
                records=by_endpoint[seed], max_nodes=caps.get("max_nodes", 80), max_edges=caps.get("max_edges", 200))
    all_hashes = {key: digest(value) for key, value in records.items()}
    add("clinical-evidence-all", "/api/v1/explorer/evidence", {"layer": "clinical", "page_size": 50},
        kind="clinical_evidence", records=all_hashes, total=len(records))
    for study in STUDIES:
        add("clinical-evidence-" + study.lower(), "/api/v1/explorer/evidence",
            {"layer": "clinical", "study_id": study, "page_size": 50}, kind="clinical_evidence",
            study_id=study, records=by_study[study], total=len(by_study[study]))
    if public:
        files = {item["path"]: {"sha256": item["sha256"], "bytes": item["bytes"]}
                 for item in manifest.get("files", [])}
        required = {"clinical-endpoints.json", "release-contract.json", "review/manifest.json", "nutriomics.sqlite3"}
        if not required <= files.keys():
            raise ValueError("Required public clinical/review/data files are absent from the manifest index")
        add("public-manifest-file-index", "/api/v1/public/files", kind="public_files", files=files)
    if fingerprint(database) != source_identity:
        raise ValueError("Release database changed while freezing performance cases")
    spec["final_upgrade_scope"] = {"schema_version": SCHEMA, "public": public,
        "inherited_case_count": len(inherited), "inherited_case_sha256": digest(inherited),
        "baseline_prefix_case_count": len(spec["cases"]) - sum("expected_response" in c for c in spec["cases"]),
        "baseline_prefix_case_sha256": baseline_sha,
        "prior_baseline_case_sha256": baseline.get("case_sha256") if baseline else None,
        "clinical_config_sha256": sha256(clinical_config), "clinical_result_records": len(records),
        "clinical_study_audits": len(audits), "clinical_health_nodes": len(by_endpoint),
        "scientific_expert_accuracy": "not_evaluated",
        "scope": "Engineering regression and native result-value preservation; not sealed retrieval evaluation or clinical efficacy validation"}
    spec["source_manifest_sha256"] = sha256(release / "manifest.json")
    spec["case_sha256"] = digest(spec["cases"])
    atomic_json(output, spec)
    return spec


def _check_expected(data, expected):
    errors = []
    if not isinstance(data, dict):
        return ["response_is_not_an_object"]
    if data.get("release_id") != expected["release_id"]:
        errors.append("release_id_mismatch")
    def record_values(actual):
        if not isinstance(actual, list) or any(not isinstance(row, dict) for row in actual):
            errors.append("clinical_results_missing")
            return
        found = {row.get("id"): digest(row) for row in actual}
        if len(found) != len(actual) or found != expected["records"]:
            errors.append("clinical_result_ids_or_values_changed")
    kind = expected["kind"]
    if kind == "clinical_study":
        record_values(data.get("clinical_endpoints"))
        audit = data.get("clinical_endpoint_audit")
        if not isinstance(audit, dict) or audit.get("study_id") != expected["study_id"]:
            errors.append("clinical_study_audit_missing")
        if data.get("review_summary", {}).get("expert_review_status") != "pending":
            errors.append("clinical_review_status_changed")
    elif kind == "clinical_graph":
        nodes, edges = data.get("nodes", []), data.get("edges", [])
        if expected["seed_id"] not in {node.get("id") for node in nodes}:
            errors.append("clinical_health_seed_missing")
        if len(nodes) > expected["max_nodes"] or len(edges) > expected["max_edges"]:
            errors.append("graph_caps_exceeded")
        found = {edge.get("id"): edge for edge in edges}
        if not set(expected["required_edge_ids"]) <= found.keys():
            errors.append("observed_clinical_edges_missing")
        clinical = []
        for identifier in expected["required_edge_ids"]:
            if identifier in found:
                edge = found[identifier]
                if edge.get("tier") != "observed":
                    errors.append("clinical_edge_tier_changed")
                clinical.extend(edge.get("context", {}).get("clinical_endpoints", []))
        record_values(clinical)
    elif kind == "clinical_evidence":
        items = data.get("items", [])
        if data.get("total") != expected["total"] or not items:
            errors.append("clinical_evidence_total_missing_or_changed")
        if any(item.get("layer") != "clinical" or (expected.get("study_id") and item.get("study_id") != expected["study_id"]) for item in items):
            errors.append("clinical_evidence_filter_changed")
        clinical = [row for item in items for row in item.get("metadata", {}).get("clinical_endpoints", [])]
        record_values(clinical)
    elif kind == "meta":
        counts = data.get("counts", {})
        if counts.get("by_kind") != expected["by_kind"] or counts.get("node_kinds") != expected["by_kind"]:
            errors.append("canonical_node_counts_changed")
        if counts.get("science_kinds") != expected["science_kinds"]:
            errors.append("canonical_science_counts_changed")
        if any(data.get(key) != expected["release_id"] for key in ("dataset_release_id", "service_build_id")):
            errors.append("legacy_release_alias_changed")
        if data.get("submission_ready") is not False or data.get("expert_review") != "pending":
            errors.append("scientific_approval_inferred")
    elif kind == "public_files":
        items = data.get("items", [])
        actual = {row.get("path"): {"sha256": row.get("sha256"), "bytes": row.get("bytes")} for row in items}
        if len(actual) != len(items) or actual != expected["files"]:
            errors.append("public_file_index_changed_or_empty")
        if data.get("availability") != "candidate_release_files":
            errors.append("public_file_availability_missing")
    else:
        errors.append("unsupported_response_expectation")
    return sorted(set(errors))


def verify_recorded_responses(spec_path, response_dir):
    """Check measured JSON artifacts against frozen native clinical values.

    This does not generate timing samples or count as expert scientific review.
    The standard benchmark separately checks HTTP failures, timings and hashes.
    """
    spec, response_dir = _load_spec(spec_path), Path(response_dir)
    issues, checked = [], 0
    performance = response_dir / "performance.json"
    if not performance.is_file():
        issues.append({"case": None, "code": "benchmark_report_missing"})
    else:
        report = json.loads(performance.read_text(encoding="utf-8"))
        if report.get("case_sha256") != spec["case_sha256"]:
            issues.append({"case": None, "code": "benchmark_case_sha_mismatch"})
    for case in spec["cases"]:
        path = response_dir / (case["id"] + "-response.json")
        if not path.is_file():
            issues.append({"case": case["id"], "code": "measured_response_missing"})
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            expected = case.get("expected_response")
            codes = (_check_expected(data, expected) if expected else
                     ([] if isinstance(data, dict) and data.get("release_id") == spec["baseline_release"] else ["release_id_mismatch"]))
        except (ValueError, TypeError, AttributeError) as exc:
            codes = ["response_validation_failed:" + type(exc).__name__]
        checked += 1
        issues.extend({"case": case["id"], "code": code} for code in codes)
    return {"schema_version": SCHEMA, "release_id": spec["baseline_release"],
        "case_sha256": spec["case_sha256"], "checked_cases": checked, "expected_cases": len(spec["cases"]),
        "passed": not issues, "issues": issues, "expert_semantic_accuracy": "not_evaluated",
        "scope": "Required-data and native clinical value preservation in measured response artifacts; not timing or biological validation"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    freeze = sub.add_parser("freeze")
    for name in ("release", "output"):
        freeze.add_argument("--" + name, type=Path, required=True)
    freeze.add_argument("--public", action="store_true")
    freeze.add_argument("--clinical-config", type=Path, default=DEFAULT_CONFIG)
    freeze.add_argument("--baseline-spec", type=Path)
    verify = sub.add_parser("verify")
    for name in ("spec", "responses", "output"):
        verify.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "freeze":
        result = freeze_final_upgrade(args.release, args.output, args.public, args.clinical_config, args.baseline_spec)
        print(json.dumps({"cases": len(result["cases"]), "case_sha256": result["case_sha256"]}))
    else:
        result = verify_recorded_responses(args.spec, args.responses)
        atomic_json(args.output, result)
        print(json.dumps({"passed": result["passed"], "checked_cases": result["checked_cases"], "issues": result["issues"]}))
        raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
