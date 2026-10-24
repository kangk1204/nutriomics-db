"""Fail-closed public-performance and engine-specific operation promotion gate."""

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from .benchmarks import evaluator_source_hashes, verify_receipt
from .db import connect, digest, dumps

CONTRACT = Path(__file__).resolve().parents[1] / "config" / "benchmark-acceptance-v2.json"


def _value(item):
    if isinstance(item, (str, Path)):
        return json.loads(Path(item).read_bytes())
    # output_path is a function convenience field, not part of an immutable receipt.
    return {key: value for key, value in item.items() if key != "output_path"}


def _meets(value, target):
    return type(value) in {int, float} and math.isfinite(value) and value >= target


def _provenance_identity(provenance):
    parser = provenance.get("parser", {})
    return {"engine_version": provenance.get("engine_version"), "rules_version": provenance.get("rules_version"),
            "dictionary_version": provenance.get("dictionary_version"),
            "parser_version": "|".join(str(parser.get(key, "")) for key in
                                        ["name", "version", "spacy_version", "scispacy_version"]),
            "model_sha256": parser.get("model_sha256")}


def _timestamp(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Operational timestamps require timezone")
    return value


def operational_gate(operational, provenance, contract, settings=None):
    if not operational:
        return {"passed": False, "reasons": ["seven_day_v2_operation_pending"]}
    try:
        record = _value(operational)
        reasons = []
        canonical = {key: value for key, value in record.items() if key != "receipt_sha256"}
        if record.get("receipt_sha256") != digest(dumps(canonical)):
            reasons.append("operational_receipt_hash_invalid")
        if not verify_receipt(settings, "engine_soak_receipt", record):
            reasons.append("operational_receipt_not_durably_recorded")
        expected = _provenance_identity(provenance)
        if (record.get("schema_version") != "literature-engine-soak-v1"
                or record.get("engine") != "heuristic-v2" or record.get("provenance") != expected):
            reasons.append("operational_engine_provenance_mismatch")
        samples = record.get("samples", [])
        if len(samples) < 2:
            reasons.append("insufficient_durable_health_samples")
        times = []
        health_rows = {}
        drill_rows = {}
        if settings is not None:
            with connect(settings) as con:
                health_rows = {row["observed_at"]: dict(row) for row in con.execute(
                    "SELECT * FROM engine_health WHERE engine_version='heuristic-v2'",
                )}
                drill_rows = {row["kind"]: json.loads(row["receipt_json"]) for row in con.execute(
                    "SELECT * FROM engine_drills WHERE engine_version='heuristic-v2'",
                )}
        for sample in samples:
            times.append(_timestamp(sample["at"]))
            if (sample.get("ok") is not True or sample.get("source") != "durable_health_snapshot"
                    or any(sample.get(key) != value for key, value in expected.items())
                    or len(sample.get("snapshot_sha256", "")) != 64):
                reasons.append("failed_or_wrong_engine_health_sample")
                break
            durable = health_rows.get(sample["at"])
            full_probe = ({"at": durable["observed_at"], "engine": durable["engine_version"],
                           "ok": bool(durable["ok"]), "provenance": json.loads(durable["provenance"]),
                           "detail": json.loads(durable["detail"])} if durable else None)
            if (durable is None or durable["ok"] != 1
                    or json.loads(durable["provenance"]) != expected
                    or durable["snapshot_sha256"] != sample["snapshot_sha256"]
                    or digest(dumps(full_probe)) != durable["snapshot_sha256"]):
                reasons.append("health_sample_not_bound_to_durable_probe")
                break
        if len(times) >= 2:
            differences = [(right - left).total_seconds() for left, right in zip(times, times[1:])]
            duration = (times[-1] - times[0]).total_seconds()
            if duration < contract["operational_days"] * 86400:
                reasons.append("seven_day_v2_operation_pending")
            if any(gap <= 0 or gap > contract["maximum_sample_gap_seconds"] for gap in differences):
                reasons.append("health_sampling_gap_or_order_failed")
            if (_timestamp(record["started_at"]) != times[0]
                    or _timestamp(record["completed_at"]) != times[-1]):
                reasons.append("operation_window_not_bound_to_samples")
            age = (datetime.now(timezone.utc) - times[-1]).total_seconds()
            if age < 0 or age > contract["maximum_sample_gap_seconds"]:
                reasons.append("latest_health_sample_stale_or_future")
        for name in contract["required_drills"]:
            drill = record.get("checks", {}).get(name, {})
            if drill.get("passed") is not True or len(drill.get("receipt_sha256", "")) != 64:
                reasons.append(name + "_drill_pending")
                continue
            durable = drill_rows.get(name, {})
            canonical_drill = {key: value for key, value in durable.items() if key != "receipt_sha256"}
            if (not durable or durable.get("passed") is not True or durable.get("kind") != name
                    or durable.get("engine") != "heuristic-v2" or durable.get("provenance") != expected
                    or durable.get("receipt_sha256") != drill["receipt_sha256"]
                    or digest(dumps(canonical_drill)) != drill["receipt_sha256"]
                    or not verify_receipt(settings, "engine_drill", durable)
                    or _timestamp(durable["completed_at"]) > datetime.now(timezone.utc)):
                reasons.append(name + "_drill_not_bound_to_durable_execution")
        return {"passed": not reasons, "reasons": sorted(set(reasons)), "id": record.get("id"),
                "samples": len(samples), "days_required": contract["operational_days"]}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"passed": False, "reasons": ["invalid_operational_evidence"]}


def promotion_status(public_reports, comparison, operational, contract_path=None, settings=None):
    """Only durable, frozen test measurements may change the default candidate engine.

    Regression tests, copied predictions, nutrition pending gold, v1 health records,
    and public development scores cannot satisfy this function.
    """
    raw = Path(contract_path or CONTRACT).read_bytes()
    contract = json.loads(raw)
    if contract.get("schema_version") != "literature-v2-promotion-contract-v1":
        raise ValueError("Expected explicit v2 public promotion contract")
    reasons, reports = [], []
    evaluator_hashes = evaluator_source_hashes()
    for candidate in public_reports or []:
        try:
            report = _value(candidate)
            if (report.get("schema_version") != "literature-public-evaluation-v1"
                    or report.get("status") != "measured_on_published_public_gold"
                    or not verify_receipt(settings, "public_benchmark_evaluation", report)):
                reasons.append("unverified_public_measurement")
                continue
            if (report.get("split") != "test" or not report.get("freeze_id")
                    or not (report.get("official_split") is True or report.get("preregistered_split") is True)
                    or report.get("complete_partition") is not True):
                reasons.append("unfrozen_incomplete_or_development_public_measurement")
                continue
            if report.get("provenance", {}).get("engine_version") != "heuristic-v2":
                reasons.append("public_measurement_wrong_engine")
                continue
            if report.get("ablation_stage") or report["provenance"].get("ablation_stage"):
                reasons.append("development_ablation_cannot_promote")
                continue
            if (report.get("evaluator_source_hashes") != evaluator_hashes
                    or report["provenance"].get("evaluator_source_hashes") != evaluator_hashes):
                reasons.append("frozen_evaluator_source_changed")
                continue
            reports.append(report)
        except (OSError, ValueError, TypeError, AttributeError):
            reasons.append("invalid_public_measurement")
    gene = next((r for r in reports if r["corpus"] in {"biored", "bioredirect"}
                 and r["mode"] == "end_to_end"), None)
    relation = next((r for r in reports if r["corpus"] in {"biored", "bioredirect"}
                     and r["mode"] == "gold_entities"), None)
    if gene is None:
        reasons.append("frozen_public_gene_detection_pending")
    else:
        metrics = gene.get("metrics", {}).get("gene_detection", {})
        if (not _meets(metrics.get("precision"), contract["gene_precision"])
                or not _meets(metrics.get("recall"), contract["gene_recall"])):
            reasons.append("gene_precision_or_recall_target_not_met")
    if relation is None:
        reasons.append("frozen_public_relation_oracle_pending")
    else:
        metrics = relation.get("metrics", {}).get("relation", {})
        if (not _meets(metrics.get("precision"), contract["relation_oracle_precision"])
                or not _meets(metrics.get("recall"), contract["relation_oracle_recall"])):
            reasons.append("relation_precision_or_recall_target_not_met")
        if relation.get("relation_coverage", {}).get("gold_all_types", 0) != metrics.get("tp", 0) + metrics.get("fn", 0):
            reasons.append("relation_primary_denominator_incomplete")
    provenance = relation.get("provenance") if relation else (gene.get("provenance") if gene else {})
    if any(report["provenance"] != provenance for report in reports):
        reasons.append("public_provenance_versions_differ")
    for report in reports:
        coverage = report.get("provenance_coverage", {})
        if (not _meets(coverage.get("fraction"), contract["provenance_fraction"])
                or coverage.get("total", 0) <= 0):
            reasons.append("source_provenance_not_complete")
    for level in contract["required_scope_source_levels"]:
        scope_report = next((r for r in reports if r["corpus"] == "bioscope"
                             and r["mode"] == "end_to_end" and level in r.get("scope_by_source", {})), None)
        if scope_report is None:
            reasons.append("frozen_bioscope_" + level + "_pending")
        else:
            for kind in ["negation", "speculation"]:
                metric = scope_report["scope_by_source"][level].get("scopes:" + kind, {})
                if not _meets(metric.get("f1"), contract["scope_token_f1"]):
                    reasons.append(level + "_" + kind + "_scope_target_not_met")
    try:
        paired = _value(comparison) if comparison else {}
        if (not verify_receipt(settings, "public_benchmark_comparison", paired)
                or paired.get("status") != "paired_public_gold_comparison"
                or paired.get("split") != "test" or paired.get("mode") != "gold_entities"
                or paired.get("baseline_provenance", {}).get("engine_version") != "heuristic-v1"
                or paired.get("candidate_provenance") != provenance
                or paired.get("evaluator_source_hashes") != evaluator_hashes
                or not relation or paired.get("input_sha256") != relation.get("input_sha256")):
            reasons.append("verified_paired_test_comparison_pending")
        else:
            bootstrap = paired.get("bootstrap", {}).get("relation", {})
            if bootstrap.get("iterations", 0) < contract["bootstrap_iterations"]:
                reasons.append("paired_2000_paper_bootstrap_pending")
            for metric in ["precision", "recall"]:
                if not _meets(bootstrap.get("metrics", {}).get(metric, {}).get("delta"),
                              contract["relation_delta_" + metric]):
                    reasons.append("relation_" + metric + "_improvement_not_met")
    except (OSError, ValueError, TypeError, AttributeError):
        reasons.append("invalid_paired_comparison")
    operation = operational_gate(operational, provenance, contract, settings)
    reasons.extend(operation["reasons"])
    parser = provenance.get("parser", {})
    if parser.get("mode") != "production" or parser.get("available") is not True:
        reasons.append("production_parser_not_measured")
    return {"schema_version": "literature-v2-promotion-v1", "passed": not reasons,
            "status": "public_and_operation_gates_met" if not reasons else "candidate_review_beta_gates_pending",
            "engine": "heuristic-v2", "reasons": sorted(set(reasons)),
            "contract_sha256": digest(raw), "contract": contract, "provenance": provenance,
            "public_reports": [r["id"] for r in reports], "operational": operation,
            "nutrition_accuracy": "unmeasured_pending_domain_gold", "review_time_saved": "unmeasured",
            "default_engine": "heuristic-v2" if not reasons else "heuristic-v1"}
