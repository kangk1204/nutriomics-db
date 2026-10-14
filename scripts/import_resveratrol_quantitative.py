"""Primary arm summaries and explicitly derived differences; no paired CI inference."""

import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.context_joins import put_context
from nutrigenomics.expansion import backup, connect, insert, validate
from nutrigenomics.primary_tables import tables
from scripts.import_public_expansion import entity
from scripts.import_trial_context import ROOT, arm

CAP = ROOT / "cohort-primary/GSE32357"


def endpoint_id(row):
    existing = {
        "Glucose": "glucose",
        "Triglycerides": "triglycerides",
        "ALAT": "ALT",
        "HOMA index": "HOMA-index",
        "Systolic blood pressure": "systolic-blood-pressure",
    }
    return "ClinicalOutcome:Timmers2011:" + existing.get(
        row["label"], row["table"] + ":" + str(row["row_index"])
    )


def main():
    source = next(
        row
        for row in json.loads((CAP / "quantitative-source-manifest.json").read_text())
        if row.get("file") == "PMC3880862-BioC.xml" and "sha256" in row
    )
    raw = (CAP / source["file"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != source["sha256"]:
        raise ValueError("Primary table checksum mismatch")
    parsed = tables(raw)
    if len(parsed) != 27:
        raise ValueError("Primary table row count changed")
    path = ROOT / "nutrition_evidence.sqlite3"
    if not (ROOT / "before-quantitative-resveratrol.sqlite3").exists():
        backup(path, ROOT / "before-quantitative-resveratrol.sqlite3")
    db = connect(path)
    count_means, count_differences, below_detection = 0, 0, 0
    with db:
        release = "PMC-primary-manuscript:" + source["sha256"][:20]
        insert(
            db,
            "ng_release",
            {
                "id": release,
                "source": "PMC primary author manuscript",
                "version": source["retrieved_at"],
                "url": source["url"],
                "retrieved_at": source["retrieved_at"],
                "sha256": source["sha256"],
                "local_path": str(CAP / source["file"]),
                "license": "author_manuscript; no redistribution grant established",
                "access_status": "public",
            },
        )
        put_context(
            db,
            "ng_rights",
            {
                "release_id": release,
                "status": "quarantined",
                "attribution": "Timmers et al. / PMC3880862",
                "basis": "Official publicly readable author manuscript; redistribution/license grant unverified",
                "reviewed_at": source["retrieved_at"],
            },
        )
        arm(
            db,
            "N14-parent-RCT",
            "PLACEBO",
            "TrialExposure:GSE32357:PLACEBO",
            None,
            None,
            "30 days",
            release,
        )
        for row in parsed:
            molecular = row["label"] in {"Resveratrol", "Dihydroresveratrol"}
            target = (
                "ClinicalAnalyte:Timmers2011:" + row["label"] if molecular else endpoint_id(row)
            )
            if not db.execute("SELECT id FROM ng_entity WHERE id=?", (target,)).fetchone():
                entity(
                    db,
                    target,
                    "molecular_feature" if molecular else "outcome",
                    row["label"],
                    "TrialAnalyte" if molecular else "TrialOutcome",
                    target,
                    {
                        "endpoint_class": "total_conjugated_and_unconjugated_analyte"
                        if molecular
                        else "physiological_or_clinical_marker",
                        "chemically_pure_free_form": False if molecular else None,
                        "disease_incidence": False,
                    },
                )
            common = {
                "subject_id": "TrialExposure:GSE32357:RESVERATROL",
                "object_id": target,
                "layer": "molecular_measurement" if molecular else "health_outcome",
                "study_id": "N14-parent-RCT",
                "release_id": release,
                "source_ref": "PMID:22055504#" + row["table"] + ":row=" + str(row["row_index"]),
                "tier": "human_randomized",
                "organism": "Homo sapiens",
                "duration": "30 days",
                "n_people": row["n_people"],
                "effect_unit": row["unit"],
                "tissue": "plasma" if row["table"] == "T2" else None,
            }
            if row["table"] == "T2":
                db.execute(
                    "UPDATE ng_evidence SET tissue='plasma' WHERE release_id=? AND source_ref=? AND tissue IS NULL",
                    (release, common["source_ref"]),
                )
            for treatment, value in [
                ("PLACEBO", row["placebo"]),
                ("RESVERATROL", row["resveratrol"]),
            ]:
                context = {
                    "endpoint_class": "total_plasma_analyte"
                    if molecular
                    else "physiological_or_clinical_marker",
                    "analysis_unit": "reported condition mean; repeated-person crossover",
                    "source_value": value["source_cell"],
                    "uncertainty": "source-reported SEM of the condition mean",
                    "individual_data_unavailable": True,
                    "sample_size_from_table_caption": True,
                    "multiplicity_adjustment_not_reported": True,
                    "source_pairwise_p": row["p_value"],
                    "source_units_preserved": True,
                    "total_not_free_analyte": molecular,
                    "detection_limit_not_reported": (value["value_status"] == "below_detection"),
                }
                record = dict(
                    common,
                    id="PMC3880862:" + row["table"] + ":" + str(row["row_index"]) + ":" + treatment,
                    subject_id="TrialExposure:GSE32357:" + treatment,
                    relation="reported_arm_mean",
                    inference="descriptive",
                    value_status=value["value_status"],
                    context_json=json.dumps(context, sort_keys=True),
                )
                if value["mean"] is not None:
                    record.update(
                        effect=value["mean"],
                        standard_error=value["sem"],
                        uncertainty_type="standard_error",
                    )
                    count_means += 1
                else:
                    below_detection += 1
                if treatment == "RESVERATROL":
                    record.update(dose=150, dose_unit="mg/day trans-resveratrol")
                insert(db, "ng_evidence", record)
            if not molecular:
                difference = float(
                    Decimal(row["resveratrol"]["source_mean"])
                    - Decimal(row["placebo"]["source_mean"])
                )
                context = {
                    "endpoint_class": "physiological_or_clinical_marker",
                    "analysis_unit": "derived arithmetic difference of reported condition means",
                    "derived_from_rounded_arm_means": True,
                    "source_placebo_mean": row["placebo"]["mean"],
                    "source_resveratrol_mean": row["resveratrol"]["mean"],
                    "source_placebo_SEM": row["placebo"]["sem"],
                    "source_resveratrol_SEM": row["resveratrol"]["sem"],
                    "paired_difference_SEM_unavailable": True,
                    "paired_difference_CI_unavailable": True,
                    "source_test_rule": "Paired t-test if normal; Wilcoxon signed-rank otherwise; endpoint-specific choice unspecified",
                    "covariate_adjustment": "not reported",
                    "multiplicity_adjustment": "not reported",
                    "p_value_is_source_condition_comparison_not_refitted_from_rounded_difference": True,
                    "p_value_ge_0_05_as_printed": (row["p_value"] >= 0.05),
                    "rounding_boundary_unknown": (row["p_value"] == 0.05),
                    "individual_crosswalk_available": False,
                    "food_health_attribution": False,
                }
                insert(
                    db,
                    "ng_evidence",
                    dict(
                        common,
                        id="PMC3880862:"
                        + row["table"]
                        + ":"
                        + str(row["row_index"])
                        + ":difference",
                        relation="derived_difference_of_reported_arm_means",
                        inference="intervention",
                        comparison="resveratrol minus placebo",
                        value_status="measured_zero" if difference == 0 else "measured",
                        effect=difference,
                        p_value=row["p_value"],
                        dose=150,
                        dose_unit="mg/day trans-resveratrol",
                        context_json=json.dumps(context, sort_keys=True),
                    ),
                )
                count_differences += 1
        if validate(db):
            raise ValueError(validate(db))
    report = {
        "primary_table_rows": 27,
        "reported_numeric_arm_means": count_means,
        "below_detection_records": below_detection,
        "source_derived_condition_differences": count_differences,
        "paired_difference_CI_inferred": False,
        "clinical_n": 11,
        "energy_metabolism_n": 10,
        "source_rights_quarantined": True,
        "quantitative_primary_table_data_now_available": True,
        "participant_matched_health": False,
    }
    (ROOT / "reports/resveratrol_quantitative_validation.json").write_text(
        json.dumps(report, indent=2)
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
