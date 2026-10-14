"""Freeze candidate queries and independently fetched primary-repository facts.

The repository facts are not expert-validated biological/clinical relevance gold.
No output of the retrieval implementation is used to generate expected facts.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import time
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "research" / "2026-10-01" / "benchmark"
HOLDOUT_STUDIES = {"N14", "N15", "N16"}
GEO = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi"
SDRF = "https://www.ebi.ac.uk/biostudies/files/E-MTAB-12527/E-MTAB-12527.sdrf.txt"


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def source_snapshot(url: str, path: Path, offline: bool = False) -> dict:
    if not path.exists():
        if offline:
            return {"url": url, "status": "unavailable", "reason": "No archived source snapshot"}
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "NutrigenomicsResearchDB/0.1"})
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError("Metadata source exceeded 2 MB budget")
            if b"recaptcha" in raw.lower() or b"checking your browser" in raw.lower():
                raise ValueError("Source requires interactive verification; no bypass attempted")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            time.sleep(0.4)
        except (OSError, ValueError) as error:
            return {"url": url, "status": "unavailable", "reason": str(error)}
    raw = path.read_bytes()
    return {"url": url, "status": "archived", "path": str(path), "sha256": sha256(raw),
            "bytes": len(raw), "retrieved_at": datetime.now(UTC).isoformat(),
            "content_scope": "public repository metadata; not article full text"}


def parse_soft(raw: str) -> list[tuple[int, str, str]]:
    rows = []
    for number, line in enumerate(raw.splitlines(), 1):
        if line.startswith("!") and " = " in line:
            key, value = line.split(" = ", 1)
            rows.append((number, key[1:], value))
    return rows


def atom(study: dict, source: dict, subject: str, predicate: str, value, location: dict) -> dict:
    identity = json.dumps([study["id"], subject, predicate, value], ensure_ascii=False, sort_keys=True)
    return {
        "id": "fact:" + sha256(identity.encode())[:24], "study_id": study["id"],
        "source_group": "trial:" + study["id"], "subject": subject, "predicate": predicate,
        "expected_value": value, "source_url": source["url"], "source_path": source["path"],
        "source_sha256": source["sha256"], "source_location": location,
        "fact_kind": "primary_repository_metadata", "review_status": "expert_review_pending",
        "verification_scope": "source field preservation only; biological interpretation unreviewed",
    }


def geo_facts(study: dict, output: Path, offline: bool) -> tuple[list[dict], list[dict]]:
    facts, sources = [], []
    # SubSeries are represented within the same source group, never independent studies.
    for accession in study["series"]:
        if not accession.startswith("GSE"):
            continue
        url = f"{GEO}?acc={accession}&targ=self&form=text&view=brief"
        source = source_snapshot(url, output / "sources" / f"{accession}.soft", offline)
        sources.append(source)
        if source["status"] != "archived":
            continue
        text = Path(source["path"]).read_text(encoding="utf-8-sig", errors="replace")
        if not text.startswith("^SERIES"):
            source.update(status="invalid_metadata", reason="Expected SOFT SERIES record")
            continue
        rows = parse_soft(text)
        for line, field, value in rows:
            if field in {"Series_type", "Series_pubmed_id", "Series_platform_id", "Series_relation"}:
                facts.append(atom(study, source, accession, field, value, {"line": line, "field": field}))
        sample_ids = [value for _, field, value in rows if field == "Series_sample_id"]
        if sample_ids:
            facts.append(atom(study, source, accession, "repository_sample_records", len(set(sample_ids)),
                              {"field": "Series_sample_id", "aggregation": "unique_count"}))
        # A bounded set of actual assay-to-person/time/condition mappings, not cloned
        # dose statements and not synthetic patients. All values retain their locations.
        for sample in sample_ids[:8]:
            sample_url = f"{GEO}?acc={sample}&targ=self&form=text&view=brief"
            sample_source = source_snapshot(sample_url, output / "sources" / f"{sample}.soft", offline)
            sources.append(sample_source)
            if sample_source["status"] != "archived":
                continue
            sample_text = Path(sample_source["path"]).read_text(encoding="utf-8-sig", errors="replace")
            if not sample_text.startswith("^SAMPLE"):
                sample_source.update(status="invalid_metadata", reason="Expected SOFT SAMPLE record")
                continue
            characteristic_number = Counter()
            candidates = []
            for line, field, value in parse_soft(sample_text):
                if field.startswith("Sample_characteristics_ch"):
                    name, _, observed = value.partition(": ")
                    characteristic_number[(field, name)] += 1
                    predicate = f"{field}:{name}:{characteristic_number[(field, name)]}"
                    priority = 0 if re.search("subject|individual|participant|time|diet|dose|visit|treat|condition|meal|sample", name, re.I) else 1
                    candidates.append((priority, line, predicate, observed or value))
                elif field in {"Sample_source_name_ch1", "Sample_source_name_ch2", "Sample_platform_id"}:
                    candidates.append((2, line, field, value))
            for _, line, field, value in sorted(candidates)[:5]:
                facts.append(atom(study, sample_source, sample, field, value,
                                  {"line": line, "field": field.split(":")[0]}))
    return facts, sources


def sdrf_facts(study: dict, output: Path, offline: bool) -> tuple[list[dict], list[dict]]:
    source = source_snapshot(SDRF, output / "sources" / "E-MTAB-12527.sdrf.txt", offline)
    if source["status"] != "archived":
        return [], [source]
    text = Path(source["path"]).read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text), delimiter="\t"))
    if not rows or "Source Name" not in rows[0]:
        source.update(status="invalid_metadata", reason="Expected SDRF Source Name")
        return [], [source]
    columns = list(rows[0])
    selected = [name for name in columns if name.lower() == "characteristics[individual]"
                or name.startswith("Factor Value") or name.lower() == "array data file"]
    facts, seen = [], set()
    for number, row in enumerate(rows, 2):
        subject = row["Source Name"]
        if subject in seen:
            continue
        seen.add(subject)
        for column in selected:
            facts.append(atom(study, source, subject, column, row[column],
                              {"row": number, "column": column}))
        if len(seen) >= 12:
            break
    facts.append(atom(study, source, "E-MTAB-12527", "sdrf_rows", len(rows),
                      {"aggregation": "row_count"}))
    facts.append(atom(study, source, "E-MTAB-12527", "biological_samples", len({r["Source Name"] for r in rows}),
                      {"column": "Source Name", "aggregation": "unique_count"}))
    return facts, [source]


CATEGORIES = [
    "registered_intervention", "tissue_mismatch", "time_mismatch", "duration_unknown",
    "formulation_unknown", "dose_or_unit_unknown", "mixed_exposure_causality_forbidden",
    "individual_prediction_forbidden", "clinical_endpoint_missing", "metadata_only_disclosure",
    "alias_study_deduplication", "non_significant_and_opposed_results_retained",
]


def query_cases(studies: list[dict], facts: list[dict]) -> tuple[list[dict], list[dict]]:
    dev, holdout = [], []
    dev_ids = sorted(s["id"] for s in studies if s["id"] not in HOLDOUT_STUDIES)
    if len(dev_ids) != 6 or len(HOLDOUT_STUDIES & {s["id"] for s in studies}) != 3:
        raise ValueError("Fixed 70/30 split requires six development and three holdout trials")
    for study in studies:
        partition = "holdout" if study["id"] in HOLDOUT_STUDIES else "development"
        number = 10 if partition == "holdout" else 12 if study["id"] in dev_ids[:4] else 11
        exposure = next((x for x in study["exposures"] if x.get("component_keys")), study["exposures"][0])
        keys = exposure.get("component_keys", [])
        food_query = next(iter(exposure.get("food_terms", [])), None) if not keys else None
        # This is a frozen curator-authored *candidate* relevance set. It is not
        # generated by live retrieval and must be expert-adjudicated before gold use.
        related = sorted({s["id"] for s in studies for x in s["exposures"]
                          if set(x.get("component_keys", [])) & set(keys)
                          or food_query and food_query in x.get("food_terms", [])})
        study_facts = [f["id"] for f in facts if f["study_id"] == study["id"]]
        for index, category in enumerate(CATEGORIES[:number]):
            context = {"tissue": None, "response_time_h": None, "regimen_duration_days": None}
            if category == "tissue_mismatch":
                context["tissue"] = "PBMC" if study["tissue"] != "PBMC" else "whole blood"
            elif category == "time_mismatch":
                context["response_time_h"] = 53
            elif category == "duration_unknown":
                context["tissue"] = study["tissue"]
            identity = f"{study['id']}:{category}"
            record = {
                "id": "query:" + identity, "partition": partition, "source_group": "trial:" + study["id"],
                "study_id": study["id"], "category": category,
                "query": exposure["label"], "component_keys": keys, "food_query": food_query,
                "input_kind": "hypothetical retrieval scenario; quantities are not measured research data",
                "scenario_component_amount": 0.15, "reference_context": context,
                "candidate_relevant_study_ids": related, "source_fact_ids": study_facts[:3],
                "review_status": "expert_review_pending", "expected_policy": {
                    "individual_prediction": False, "transitive_health_inference": False,
                    "component_specific_causality": False,
                    "unspecified_dimensions": "unknown",
                },
            }
            (holdout if partition == "holdout" else dev).append(record)
    return dev, holdout


def build(manifest: Path, output: Path, offline: bool = False) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    if (output / "manifest.json").exists():
        raise FileExistsError("Benchmark already frozen; choose a new output directory/version")
    raw = manifest.read_bytes()
    document = json.loads(raw)
    pools, sources = {}, []
    for study in document["studies"]:
        facts, source_records = sdrf_facts(study, output, offline) if "E-MTAB-12527" in study["series"] else geo_facts(study, output, offline)
        pools[study["id"]] = facts
        sources.extend(source_records)
    # Balanced source-group selection. A fact is unique by actual subject, field,
    # and value, not an artificial duplicate to meet a quota.
    facts, seen = [], set()
    while len(facts) < 200 and any(pools.values()):
        for study_id in sorted(pools):
            if pools[study_id] and len(facts) < 200:
                fact = pools[study_id].pop(0)
                if fact["id"] not in seen:
                    facts.append(fact)
                    seen.add(fact["id"])
    dev, holdout = query_cases(document["studies"], facts)
    write_json(output / "source_facts.candidate.json", facts)
    write_json(output / "development_queries.candidate.json", dev)
    write_json(output / "holdout_queries.sealed.json", holdout)
    write_json(output / "sources_manifest.json", sources)
    payload = {
        "version": "bridge-benchmark-2026-10-01-v1", "created_at": datetime.now(UTC).isoformat(),
        "curation_manifest": str(manifest), "curation_manifest_sha256": sha256(raw),
        "queries": {"development": len(dev), "holdout": len(holdout), "total": len(dev) + len(holdout)},
        "source_assertions": {"target": 200, "actual": len(facts), "missing": max(200-len(facts), 0),
                              "kind": "primary_repository_metadata", "expert_verified": 0},
        "source_groups": {"development": sorted({q["source_group"] for q in dev}),
                          "holdout": sorted({q["source_group"] for q in holdout})},
        "files": {name: sha256((output / name).read_bytes()) for name in
                  ("source_facts.candidate.json", "development_queries.candidate.json", "holdout_queries.sealed.json", "sources_manifest.json")},
        "holdout_status": "sealed_until_expert_acceptance", "scientific_accuracy": "not_evaluated",
        "sota_status": "not_established", "expert_review_status": "pending",
        "prior_art_comparisons": {"FoodAtlas": "not_evaluated", "Eat4Genes": "not_evaluated", "NutriGenomeDB": "not_evaluated"},
    }
    write_json(output / "manifest.json", payload)
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "config" / "human_exposures.v1.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build(args.manifest, args.output, args.offline), ensure_ascii=False, indent=2))
