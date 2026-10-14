"""Combine inherited study audit with new source-backed inventory and coverage."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import SCHEMA, TABLES, connect, coverage, insert
from scripts.import_public_expansion import soft_fields

ROOT = Path("research/2026-09-30/expansion")


def main():
    db = connect(ROOT / "nutrition_evidence.sqlite3")
    config = Path("config/study_groups.json")
    raw = config.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    release = "inherited-audit:" + digest[:20]
    with db:
        insert(
            db,
            "ng_release",
            {
                "id": release,
                "source": "local-existing-study-audit",
                "version": "2026-09-30",
                "url": "https://www.ncbi.nlm.nih.gov/geo/",
                "retrieved_at": datetime.fromtimestamp(config.stat().st_mtime, UTC).isoformat(),
                "sha256": digest,
                "local_path": str(config),
                "license": "Local configuration audit; underlying GEO NCBI policy applies",
                "access_status": "metadata_only",
            },
        )
        for study in json.loads(raw):
            counts = {"S01": 5, "S02": 14, "S03": None, "S04": 36, "S05": 18}
            designs = {
                "S01": "nonrandomized_intervention",
                "S02": "nonrandomized_intervention",
                "S03": "nonrandomized_intervention",
                "S04": "randomized",
                "S05": "nonrandomized_intervention",
            }
            insert(
                db,
                "ng_study",
                {
                    "id": study["id"],
                    "title": study["title"],
                    "population": study["description"],
                    "design": designs[study["id"]],
                    "participant_group": "baseline:" + study["id"],
                    "independence_status": "verified",
                    "independence_basis": "Inherited existing five-study audit and configured grouping; not re-audited against production DB during this expansion",
                    "n_people": counts[study["id"]],
                    "pubmed_id": study["pubmed_id"],
                    "metadata_json": json.dumps(study, ensure_ascii=False, sort_keys=True),
                },
            )
            for accession in study["series"]:
                path = Path("research/2026-09-29/geo_metadata") / (accession + ".soft.txt")
                fields = soft_fields(path.read_text(encoding="utf-8"))
                insert(
                    db,
                    "ng_dataset",
                    {
                        "id": accession,
                        "accession": accession,
                        "study_id": study["id"],
                        "release_id": release,
                        "modality": "; ".join(fields.get("type", [])),
                        "access_status": "metadata_only",
                        "ingest_status": "metadata",
                        "n_assays": len(fields.get("sample_id", [])),
                        "metadata_json": json.dumps(
                            {
                                "prior_metadata_file": str(path),
                                "prior_metadata_sha256": hashlib.sha256(
                                    path.read_bytes()
                                ).hexdigest(),
                                "production_analysis_state": "not reverified here",
                                "design": study.get("series_design", {}).get(accession),
                            },
                            sort_keys=True,
                        ),
                    },
                )
        source_rows = {r["source"]: r for r in db.execute("SELECT * FROM ng_release")}
        if "wastyk" in source_rows:
            row = source_rows["wastyk"]
            insert(
                db,
                "ng_study",
                {
                    "id": "N11",
                    "title": "Wastyk fiber/fermented-food intervention",
                    "population": "36 generally healthy adults analysed; mean age 52 years",
                    "design": "nonrandomized_intervention",
                    "n_people": 36,
                    "participant_group": "Wastyk-NCT03275662",
                    "independence_status": "verified",
                    "independence_basis": "Named registered trial with distinct recruitment and intervention; repository/PXD deposit share the same participant group",
                    "pubmed_id": "34256014",
                    "registration": "NCT03275662",
                    "metadata_json": json.dumps(
                        {
                            "randomization": "mostly randomized; two participants requested high-fiber arm and were not randomized",
                            "dose": "target +20 g fiber/day or 6 servings fermented foods/day",
                            "duration": "4-week ramp, 6-week maintenance, 4-week follow-up",
                            "license": "GitHub API license field null; data reuse terms need resolution",
                            "causal_inference": "requires separately identified randomized subset and prespecified contrasts",
                        }
                    ),
                },
            )
            insert(
                db,
                "ng_dataset",
                {
                    "id": "Wastyk:public-repository",
                    "accession": "SonnenburgLab/fiber-fermented-study",
                    "study_id": "N11",
                    "release_id": row["id"],
                    "modality": "dietary logs, microbiome, cytokines and proteomics; metadata inspected",
                    "access_status": "metadata_only",
                    "ingest_status": "blocked",
                    "metadata_json": json.dumps(
                        {
                            "url": "https://github.com/SonnenburgLab/fiber-fermented-study/",
                            "related_deposit": "PXD021786",
                            "blocker": "No declared repository license; clarify dataset reuse rights before raw-data ingestion",
                        }
                    ),
                },
            )
        if "nhanes_dietary" in source_rows:
            row = source_rows["nhanes_dietary"]
            insert(
                db,
                "ng_study",
                {
                    "id": "N12",
                    "title": "NHANES survey programme (2017-2018 planned pilot)",
                    "population": "US civilian noninstitutionalized population; design weights required",
                    "design": "observational",
                    "participant_group": "NHANES-programme",
                    "independence_status": "candidate",
                    "independence_basis": "One survey programme; dietary/lab/exam modules and mortality linkage are not independent cohorts. Never duplicate 2017-2018 in overlapping 2017-March2020 pre-pandemic release.",
                    "metadata_json": json.dumps(
                        {
                            "join": "cycle plus SEQN; verify module-specific eligibility and duplicate keys",
                            "weights": "use appropriate dietary/MEC/subsample weights with SDMVSTRA and SDMVPSU",
                            "person_records": "not obtained",
                        }
                    ),
                },
            )
            insert(
                db,
                "ng_dataset",
                {
                    "id": "NHANES:DR1TOT_J",
                    "accession": "NHANES-2017-2018-DR1TOT_J",
                    "study_id": "N12",
                    "release_id": row["id"],
                    "modality": "24-hour dietary recall totals",
                    "access_status": "agreement_required",
                    "ingest_status": "blocked",
                    "metadata_json": json.dumps(
                        {
                            "policy_url": "https://www.cdc.gov/nchs/policy/data-user-agreement.html",
                            "blocker": "Use signifies agreement; approval required before participant files are used",
                        }
                    ),
                },
            )
    report = coverage(db)
    report["completed_at"] = datetime.now(UTC).isoformat()
    report["baseline_state"] = {
        "series": 7,
        "independent_studies": 5,
        "food_composition": {"total": 1200, "nonzero": 930, "measured_zero": 244, "missing": 26},
        "CpG_export": {"rows": 484803, "BH_q_under_0_05": 0},
        "basis": "Inherited user-provided prior audit and local quality report; production DB not accessible or reverified here",
        "preserved": True,
    }
    report["new_scope"] = {
        "public_geo_series": 7,
        "geo_study_groups": 4,
        "new_geo_verified_group": ["N06"],
        "new_geo_candidate_groups": ["N07", "N08", "N09"],
        "new_processed_matrix_group": ["N06"],
        "published_aggregate_only_groups": ["N10", "N13"],
        "public_repository_metadata_only_group": ["N11"],
        "agreement_blocked_programme": ["N12"],
        "production_database_updated": False,
    }
    for r in db.execute("SELECT metadata_json FROM ng_dataset WHERE ingest_status='measurements'"):
        artifact = json.loads(r[0])["measurement_artifact"]
        report["processed_matrix"] = artifact["matrix"]
        report["processed_matrix"]["total_values"] = (
            artifact["matrix"]["n_features"] * artifact["matrix"]["n_samples"]
        )
    manifest = json.loads((ROOT / "snapshots/manifest.json").read_text())
    report["download_failures"] = manifest["failures"]
    report["access_gaps"] = manifest["blocked_sources"]
    report["rejected_noncohorts"] = [
        {
            "accession": "GSE244041",
            "reason": "human adipocyte culture plus zebrafish, not a human participant cohort",
        },
        {
            "accession": "GSE183445",
            "reason": "human melanoma xenografts in mice, not a human dietary cohort",
        },
    ]
    report["methodology_gaps"] = [
        "GEO family/sample records and publications must reconcile participant IDs, treatment order, periods, washout and assay inclusion before inference",
        "GSE27385 source processed intensity scale/normalization and GPL570 probe-to-gene mappings require audit before differential expression",
        "No new molecular target or pathway relations admitted. ChEMBL identity/license checked (CC BY-SA 3.0); activity ingestion deferred under the no-new-agreements constraint. Reactome terms also require review.",
        "Food-to-molecule semantic mappings remain candidate related mappings; total vitamin C and hydrolysed amino-acid totals are not exact molecular species",
        "DASH is a dietary-pattern contrast, not evidence for an individual food, nutrient or molecule; confidence intervals and contrast sample count not available in retrieved abstract",
        "Hall numeric outcomes withheld pending review of 2020 erratum; 2019 erratum captured, changes meal-specific results",
        "Remote production host unreachable in this environment; no production migration/deployment applied",
    ]
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "coverage.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    for table, filename in [
        ("ng_study", "cohort_inventory.csv"),
        ("ng_dataset", "dataset_inventory.csv"),
        ("ng_mapping", "identifier_mappings.csv"),
        ("ng_evidence", "evidence_inventory.csv"),
    ]:
        cursor = db.execute("SELECT * FROM " + table + " ORDER BY id")
        with (reports / filename).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow([col[0] for col in cursor.description])
            writer.writerows(cursor)
    migrations = ROOT / "migrations"
    migrations.mkdir(exist_ok=True)
    (migrations / "001_up.sql").write_text(
        "BEGIN IMMEDIATE;\n"
        + SCHEMA
        + "\nINSERT INTO ng_migration VALUES ('nutrition-evidence-v1', datetime('now'));\nCOMMIT;\n"
    )
    (migrations / "001_down.sql").write_text(
        "BEGIN IMMEDIATE;\n"
        + "\n".join(f"DROP TABLE IF EXISTS {table};" for table in TABLES)
        + "\nCOMMIT;\n"
    )
    db.close()
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
