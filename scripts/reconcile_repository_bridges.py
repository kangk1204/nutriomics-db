"""Bounded offline reconciliation. Writes a separate quarantined staging database."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.repository_bridge import build_bridge

ROOT = Path("research/2026-09-30/expansion/repository-reconciliation")
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=ROOT / "quarantined_food_affinity_v2.sqlite3"
    )
    args = parser.parse_args()
    report = build_bridge(
        Path("final_2026/repos/nutriomics-compound-target/data/food_bridge"),
        args.output,
    )
    args.output.with_suffix(".validation.json").write_text(json.dumps(report, indent=2) + "\n")
    snapshot = json.loads((ROOT / "nutriomics-intervention-atlas.json").read_text())
    with sqlite3.connect(
        (ROOT.parent / "nutrition_evidence.sqlite3").resolve().as_uri() + "?mode=ro", uri=True
    ) as db:
        existing = {
            row[0]: row[1] for row in db.execute("SELECT accession,study_id FROM ng_dataset")
        }
    cohorts = []
    for cohort in snapshot["cohorts"]:
        validation = cohort["validation"]
        accession = validation["accession"]
        cohorts.append(
            {
                "accession": accession,
                "existing_study_id": existing.get(accession),
                "new_cohort": accession not in existing,
                "repository_claimed_people": validation["independent_people"],
                "primary_ingestion_authorized": accession != "ST001257",
                "analysis_reproduction": "repository report only; not independently rerun",
                "status": "agreement_review_required"
                if accession == "ST001257"
                else "existing_dataset_reused_not_new_cohort",
            }
        )
    (ROOT / "cohort_reconciliation.json").write_text(
        json.dumps(
            {
                "cohorts": cohorts,
                "individual_multiomics_fusion": False,
                "new_candidate_groups": sum(c["new_cohort"] for c in cohorts),
                "terms_source": "https://www.metabolomicsworkbench.org/about/termsofuse.php",
                "terms_checked_date": "2026-10-01",
                "terms_accepted": False,
            },
            indent=2,
        )
        + "\n"
    )
    print(json.dumps(report["counts"]))
