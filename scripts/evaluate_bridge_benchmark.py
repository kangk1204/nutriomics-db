"""Evaluate candidate development queries and machine integrity, never claim SOTA.

Holdout evaluation requires an explicit expert acceptance record bound to this
frozen manifest. It is intentionally not available through an override flag.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sqlalchemy import func, or_, select

from nutrigenomics.bridge import meal_evidence
from nutrigenomics.bridge_models import BridgeSource, Exposure
from nutrigenomics.composition import analyze_meal
from nutrigenomics.db import get_session_factory
from nutrigenomics.models import Component, Food, StudyEffect
from nutrigenomics.settings import DATABASE_URL


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wilson(successes: int, total: int) -> dict:
    if total == 0:
        return {"successes": 0, "denominator": 0, "estimate": None, "ci95": None}
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z*z/total
    center = (p + z*z/(2*total)) / denominator
    width = z * math.sqrt(p*(1-p)/total + z*z/(4*total*total)) / denominator
    return {"successes": successes, "denominator": total, "estimate": p,
            "ci95": [max(0, center-width), min(1, center+width)], "interval": "Wilson95%",
            "dependence_note": "Repeated scenarios share trials; this interval is descriptive, not a trial-independent uncertainty estimate."}


def source_integrity(directory: Path) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    files = {name: digest(directory / name) == expected for name, expected in manifest["files"].items()}
    facts = json.loads((directory / "source_facts.candidate.json").read_text(encoding="utf-8"))
    errors, verified, parsed = [], 0, {}
    seen = set()
    for fact in facts:
        if fact["id"] in seen:
            errors.append({"fact_id": fact["id"], "reason": "duplicate_atomic_fact"})
            continue
        seen.add(fact["id"])
        path = Path(fact["source_path"])
        if not path.is_file():
            # Frozen snapshots can be copied to a Linux staging directory.
            basename = str(fact["source_path"]).replace("\\", "/").rsplit("/", 1)[-1]
            path = directory / "sources" / basename
        if not path.is_file() or digest(path) != fact["source_sha256"]:
            errors.append({"fact_id": fact["id"], "reason": "source_snapshot_hash_mismatch"})
            continue
        if str(path) not in parsed:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            parsed[str(path)] = (text.splitlines(), list(csv.DictReader(text.splitlines(), delimiter="\t")) if path.suffix == ".txt" else None)
        lines, rows = parsed[str(path)]
        location = fact["source_location"]
        if "line" in location:
            raw = lines[location["line"]-1]
            actual = raw.split(" = ", 1)[1]
            if fact["predicate"].startswith("Sample_characteristics"):
                _, separator, value = actual.partition(": ")
                actual = value if separator else actual
        elif "row" in location:
            actual = rows[location["row"]-2][location["column"]]
        elif location["aggregation"] == "row_count":
            actual = len(rows)
        elif rows is not None:
            actual = len({r[location["column"]] for r in rows})
        else:
            prefix = "!" + location["field"] + " = "
            actual = len({line[len(prefix):] for line in lines if line.startswith(prefix)})
        if actual == fact["expected_value"]:
            verified += 1
        else:
            errors.append({"fact_id": fact["id"], "reason": "source_field_differs"})
    return {"files_verified": files, "atomic_facts": len(facts), "unique_facts": len(seen),
            "source_field_checks": wilson(verified, len(facts)), "errors": errors,
            "scope": "Exact source/hash/metadata-field preservation; not independent scientific relevance validation"}


def load_queries(directory: Path, partition: str) -> list[dict]:
    if partition == "holdout":
        acceptance_path = directory / "expert_acceptance.json"
        if not acceptance_path.is_file():
            raise PermissionError("Holdout sealed: expert acceptance is missing")
        acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
        if not (acceptance.get("status") == "accepted" and acceptance.get("reviewer")
                and acceptance.get("reviewed_at")
                and acceptance.get("manifest_sha256") == digest(directory / "manifest.json")):
            raise PermissionError("Holdout sealed: acceptance is not bound to this manifest and reviewer")
        filename = "holdout_queries.sealed.json"
    else:
        filename = "development_queries.candidate.json"
    queries = json.loads((directory / filename).read_text(encoding="utf-8"))
    if partition == "holdout":
        accepted = {r["id"] for r in acceptance.get("query_reviews", [])
                    if r.get("review_status") == "expert_verified" and r.get("reviewer")}
        facts = json.loads((directory / "source_facts.candidate.json").read_text(encoding="utf-8"))
        accepted_facts = {r["id"] for r in acceptance.get("fact_reviews", [])
                          if r.get("review_status") == "expert_verified" and r.get("reviewer")}
        needed = {f["id"] for f in facts if f["study_id"] in {q["study_id"] for q in queries}}
        if not {q["id"] for q in queries} <= accepted or not needed <= accepted_facts:
            raise PermissionError("Holdout sealed: every held-out query and source fact requires expert review")
    return queries


def statistical_integrity(session) -> dict:
    total = session.scalar(select(func.count()).select_from(StudyEffect)) or 0
    invalid = session.scalar(select(func.count()).select_from(StudyEffect).where(or_(
        StudyEffect.p_value < 0, StudyEffect.p_value > 1,
        StudyEffect.q_value < 0, StudyEffect.q_value > 1,
        StudyEffect.n_people <= 0, StudyEffect.n_samples <= 0,
    ))) or 0
    duplicate = session.scalar(select(func.count()).select_from(
        select(StudyEffect.series_accession, StudyEffect.feature_type, StudyEffect.feature_id,
               StudyEffect.contrast, StudyEffect.analysis_version)
        .group_by(StudyEffect.series_accession, StudyEffect.feature_type, StudyEffect.feature_id,
                  StudyEffect.contrast, StudyEffect.analysis_version)
        .having(func.count() > 1).subquery())) or 0
    null_q = session.scalar(select(func.count()).select_from(StudyEffect).where(StudyEffect.q_value.is_(None))) or 0
    non_significant = session.scalar(select(func.count()).select_from(StudyEffect).where(StudyEffect.q_value >= 0.05)) or 0
    negative = session.scalar(select(func.count()).select_from(StudyEffect).where(StudyEffect.effect_estimate < 0)) or 0
    source_rows = list(session.scalars(select(BridgeSource)))
    provenance_complete = sum(bool(r.url and r.version and re.fullmatch(r"[a-fA-F0-9]{64}", r.sha256)
                                   and r.source_location and r.license) for r in source_rows)
    return {"effect_rows": total, "invalid_numeric_rows": invalid, "duplicate_full_effect_keys": duplicate,
            "missing_q_value_rows": null_q, "non_significant_rows_preserved": non_significant,
            "negative_effect_rows_preserved": negative,
            "bridge_provenance_fields": wilson(provenance_complete, len(source_rows)),
            "scope": "Machine integrity only; sign, nonsignificance and provenance completeness do not establish a health effect"}


def scenario_calculation(session, query: dict) -> tuple[dict | None, str | None]:
    if query["component_keys"]:
        rows = list(session.scalars(select(Component).where(Component.component_key.in_(query["component_keys"]))))
        if not rows:
            return None, "scenario_components_not_in_database"
        return {"items": [], "components": [{"component_id": c.id, "name": c.name,
            "component_key": c.component_key, "amount": query["scenario_component_amount"],
            "unit": c.canonical_unit or "g"} for c in rows]}, None
    text = query.get("food_query")
    food = session.scalar(select(Food).where(or_(Food.name.ilike(f"%{text}%"), Food.name_en.ilike(f"%{text}%"))).order_by(Food.id)) if text else None
    if food is None:
        return None, "scenario_food_not_in_database"
    return analyze_meal(session, [{"food_id": food.id, "amount_g": 150}]), None


def evaluate(directory: Path, database_url: str, partition="development") -> dict:
    integrity = source_integrity(directory)
    if not all(integrity["files_verified"].values()) or integrity["errors"]:
        raise ValueError("Frozen benchmark/source integrity failed")
    queries = load_queries(directory, partition)
    outcomes, counters = [], {method: Counter() for method in ("keyword_only", "context_free", "bridge")}
    policy_pass, policy_total, exact_pass, exact_total = 0, 0, 0, 0
    with get_session_factory(database_url)() as session:
        stats = statistical_integrity(session)
        exposures = list(session.scalars(select(Exposure)))
        for query in queries:
            calculation, reason = scenario_calculation(session, query)
            if calculation is None:
                outcomes.append({"query_id": query["id"], "status": "not_evaluable", "reason": reason})
                continue
            words = {w.casefold() for w in re.findall(r"[\w-]+", query["query"]) if len(w) > 2}
            keyword_ids = {e.study_id for e in exposures if words & set(re.findall(r"[\w-]+", e.label.casefold()))}
            keys = {c["component_key"] for c in calculation.get("components", [])}
            context_free_ids = {e.study_id for e in exposures if keys & set(e.component_keys)}
            result = meal_evidence(session, calculation, query["reference_context"])
            returned = result.get("observed_evidence", [])
            bridge_ids = {r["study_id"] for r in returned}
            expected = set(query["candidate_relevant_study_ids"])
            methods = {"keyword_only": keyword_ids, "context_free": context_free_ids, "bridge": bridge_ids}
            row = {"query_id": query["id"], "source_group": query["source_group"], "status": "candidate_evaluation", "methods": {}}
            for method, ids in methods.items():
                good = len(ids & expected)
                counters[method].update(true_positive=good, returned=len(ids), relevant=len(expected),
                                        retrieved_any=int(bool(ids & expected)), evaluated_queries=1)
                row["methods"][method] = {"returned_study_ids": sorted(ids), "candidate_scope_correct": good,
                                         "returned": len(ids), "candidate_relevant": len(expected)}
            checks = [result.get("individual_prediction") is False,
                      result.get("transitive_health_inference") is False]
            for record in returned:
                app = record.get("applicability", {})
                checks.append(app.get("component_specific_causality") is False)
                dimensions = app.get("dimensions", {})
                for key, field in [("tissue", "tissue"), ("response_time_h", "response_time"),
                                   ("regimen_duration_days", "duration")]:
                    if query["reference_context"].get(key) is None:
                        checks.append(dimensions.get(field) == "unknown")
                selector = record.get("effect_selector") or {}
                for preview in record.get("results", []):
                    exact_total += 1
                    original = session.scalar(select(StudyEffect).where(
                        StudyEffect.series_accession == selector.get("accession"),
                        StudyEffect.feature_type == preview["feature_type"],
                        StudyEffect.feature_id == preview["feature_id"],
                        StudyEffect.contrast == selector.get("contrast"),
                        StudyEffect.analysis_version == selector.get("analysis_version"),
                    ))
                    exact_pass += int(original is not None and all(
                        getattr(original, field) == preview.get(field) for field in
                        ("effect_estimate", "standard_error", "p_value", "q_value", "status")))
            policy_pass += sum(checks)
            policy_total += len(checks)
            row["policy_checks"] = {"passed": sum(checks), "total": len(checks)}
            outcomes.append(row)
    metrics = {method: {"candidate_scope_precision": wilson(c["true_positive"], c["returned"]),
                       "candidate_scope_recall": wilson(c["true_positive"], c["relevant"]),
                       "retrieval_coverage": wilson(c["retrieved_any"], c["evaluated_queries"])}
               for method, c in counters.items()}
    return {"partition": partition, "query_count": len(queries), "evaluated_queries": sum(r["status"] == "candidate_evaluation" for r in outcomes),
            "qualification": "candidate curator-defined scope; expert scientific relevance pending",
            "source_integrity": integrity, "statistical_integrity": stats, "candidate_comparison": metrics,
            "machine_policy_checks": wilson(policy_pass, policy_total), "exact_effect_preview_checks": wilson(exact_pass, exact_total),
            "scientific_precision": {"status": "not_evaluated", "reason": "No independently expert-verified relevance gold"},
            "holdout_status": "not_evaluated_sealed" if partition == "development" else "expert_accepted_evaluation",
            "sota_status": "not_established", "prior_art_comparisons": {"FoodAtlas": "not_evaluated", "Eat4Genes": "not_evaluated", "NutriGenomeDB": "not_evaluated"},
            "outcomes": outcomes}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", type=Path, default=ROOT / "research" / "2026-10-01" / "benchmark")
    parser.add_argument("--database-url", default=DATABASE_URL)
    parser.add_argument("--partition", choices=["development", "holdout"], default="development")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = evaluate(args.benchmark, args.database_url, args.partition)
    except (PermissionError, ValueError) as error:
        parser.exit(2, str(error) + "\n")
    output = args.output or args.benchmark / f"{args.partition}_evaluation.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "query_count": result["query_count"],
                      "evaluated_queries": result["evaluated_queries"], "scientific_precision": result["scientific_precision"],
                      "sota_status": result["sota_status"]}, ensure_ascii=False, indent=2))
