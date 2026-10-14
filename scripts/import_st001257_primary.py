"""Replay primary public NMDR measurements into reversible research tables."""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.context_joins import put_context
from nutrigenomics.expansion import backup, connect, insert, validate
from nutrigenomics.metabolomics import migrate_metabolomics, parse_source
from scripts.import_public_expansion import entity

ROOT = Path("research/2026-09-30/expansion")
CAP = ROOT / "cohort-primary/ST001257"


def main():
    manifest = json.loads((CAP / "manifest.json").read_text())
    for record in manifest:
        if hashlib.sha256((CAP / record["file"]).read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError("Primary snapshot checksum mismatch")
    features, samples, report = parse_source(
        (CAP / "untargeted.tsv").read_bytes(),
        json.loads((CAP / "factors.json").read_text()),
        json.loads((CAP / "analysis.json").read_text()),
        (CAP / "mwtab.txt").read_text(),
    )
    if (report["n_people"], report["food"], report["urine"], report["n_features"]) != (
        19,
        14,
        76,
        11693,
    ):
        raise ValueError("Primary cohort size changed")
    database = ROOT / "nutrition_evidence.sqlite3"
    if not (ROOT / "before-st001257.sqlite3").exists():
        backup(database, ROOT / "before-st001257.sqlite3")
    db = connect(database)
    migrate_metabolomics(db)
    releases = {}
    with db:
        for row in manifest:
            release = "NMDR-primary:" + row["sha256"][:20]
            releases[row["file"]] = release
            insert(
                db,
                "ng_release",
                {
                    "id": release,
                    "source": "NMDR primary public data",
                    "version": "ST001257 release 1; 2020-09-22",
                    "url": row["url"],
                    "retrieved_at": row["retrieved_at"],
                    "sha256": row["sha256"],
                    "local_path": str(CAP / row["file"]),
                    "license": "Released data public domain under NMDR data-sharing policy; website/article terms separate",
                    "access_status": "public",
                },
            )
            put_context(
                db,
                "ng_rights",
                {
                    "release_id": release,
                    "status": "quarantined",
                    "license_identifier": "NMDR-released-data-public-domain",
                    "policy_url": "https://www.metabolomicsworkbench.org/data/datasharing.php",
                    "attribution": "Reisdorph et al.; ST001257 / AN002086 / PR000843; DOI 10.21228/M8QH5G",
                    "basis": "Released-data policy verified; excluded from shareable release pending metadata and interpretation review; no public release authorized",
                    "reviewed_at": row["retrieved_at"],
                },
            )
        source = releases["untargeted.tsv"]
        factor_source = releases["factors.json"]
        insert(
            db,
            "ng_study",
            {
                "id": "N15",
                "title": "Nutrimetabolomics and DASH diet",
                "population": "19 human participants in controlled DASH-style intervention; 24-hour urine and separate food preparations",
                "design": "randomized",
                "participant_group": "ST001257-PR000843",
                "independence_status": "candidate",
                "independence_basis": "New accession/study compared with existing inventory; cross-study roster overlap and parent-trial registration unverified",
                "n_people": 19,
                "metadata_json": json.dumps(
                    {
                        "cross_study_person_overlap_unknown": True,
                        "food_preparations_are_not_people": True,
                        "individual_health_crosswalk_verified": False,
                        "period_sequence_unknown": True,
                        "repository_lead_commit": "71baf2fb8e29196c7f05904f8a8ae55f764d3cb5",
                    },
                    sort_keys=True,
                ),
            },
        )
        insert(
            db,
            "ng_dataset",
            {
                "id": "ST001257",
                "study_id": "N15",
                "accession": "ST001257",
                "release_id": source,
                "modality": "Untargeted LC-MS; neutral-mass and retention-time anonymous features",
                "access_status": "public",
                "ingest_status": "measurements",
                "n_assays": 90,
                "metadata_json": json.dumps(
                    {
                        "analysis_id": "AN002086",
                        "primary_report": report,
                        "metadata_release_ids": releases,
                        "processing": "Source already log2-transformed counts; no local normalization or second transformation",
                        "sparse_storage": "Observed finite cells only; explicit NA counts preserved per sample and full primary matrix retained",
                        "complete_food_compound_health_chains": 0,
                    },
                    sort_keys=True,
                ),
            },
        )
        for arm in ["Chicken", "Pork"]:
            exposure = "ST001257:DASH-" + arm
            entity(
                db,
                exposure,
                "diet",
                "DASH-style intervention; source factor " + arm,
                "NMDR:ST001257:arm",
                arm,
            )
            put_context(
                db,
                "ng_arm",
                {
                    "id": exposure,
                    "study_id": "N15",
                    "exposure_id": exposure,
                    "source_label": arm,
                    "duration": "6 weeks",
                    "release_id": factor_source,
                    "context_json": json.dumps(
                        {
                            "sequence_unknown": True,
                            "dose_unknown": True,
                            "food_specific_batch_composition_unknown": True,
                        },
                        sort_keys=True,
                    ),
                },
            )
        feature_ids = []
        for label in features:
            feature_id = "NMDR:AN002086:feature:" + label
            feature_ids.append(feature_id)
            neutral_mass, retention_time = label.split("_")
            entity(
                db,
                feature_id,
                "molecular_feature",
                label,
                "NMDR:AN002086:feature",
                label,
                {
                    "identity": "unidentified; no named metabolite identification",
                    "source_neutral_mass_label": neutral_mass,
                    "retention_time": retention_time,
                    "retention_time_unit": "Minutes",
                    "mass_unit_not_explicit": True,
                    "release_id": source,
                },
            )
        collection = "NMDR:AN002086:feature-collection"
        entity(
            db,
            collection,
            "molecular_feature",
            "11,693 unidentified LC-MS features",
            "NMDR:AN002086",
            "feature-collection",
        )
        for sample in samples:
            food = None
            if sample["sample_type"] == "food":
                food = "ST001257:foodprep:" + sample["source_id"]
                entity(
                    db,
                    food,
                    "food",
                    sample["label"],
                    "NMDR:ST001257:foodprep",
                    sample["source_id"],
                    {
                        "food_type": sample["food_type"],
                        "generic_food_composition_crosswalk_verified": False,
                        "not_a_trial_arm": True,
                    },
                )
            row = {
                "id": sample["id"],
                "dataset_id": "ST001257",
                "source_label": sample["label"],
                "source_sample_id": sample["source_id"],
                "sample_type": sample["sample_type"],
                "participant_key": sample["participant"],
                "arm_id": "ST001257:DASH-" + sample["arm"] if sample["arm"] else None,
                "time_label": sample["time"],
                "food_entity_id": food,
                "unit": report["unit"],
                "missing_count": sample["missing"],
                "release_id": factor_source,
                "context_json": json.dumps(sample["context"], sort_keys=True),
            }
            existing = db.execute(
                "SELECT * FROM ng_molecular_sample WHERE id=?", (row["id"],)
            ).fetchone()
            if existing and any(existing[k] != v for k, v in row.items()):
                raise ValueError("Conflicting molecular sample")
            if not existing:
                db.execute(
                    "INSERT INTO ng_molecular_sample ("
                    + ",".join(row)
                    + ") VALUES("
                    + ",".join("?" for _ in row)
                    + ")",
                    list(row.values()),
                )
            values = [
                (
                    sample["id"],
                    feature_id,
                    value,
                    "measured_zero" if value == 0 else "measured",
                    source,
                )
                for feature_id, value in zip(feature_ids, sample["values"], strict=True)
                if value is not None
            ]
            present = db.execute(
                "SELECT sample_id,feature_id,value,value_status,release_id FROM ng_molecular_measurement WHERE sample_id=? ORDER BY feature_id",
                (sample["id"],),
            ).fetchall()
            if present:
                if [tuple(r) for r in present] != sorted(values, key=lambda r: r[1]):
                    raise ValueError("Conflicting measurements or sparse coverage")
            else:
                db.executemany("INSERT INTO ng_molecular_measurement VALUES(?,?,?,?,?)", values)
        for kind in ["food", "urine"]:
            subject = "ST001257:sample-type:" + kind
            entity(
                db, subject, "diet", kind + " sample collection", "NMDR:ST001257:collection", kind
            )
            insert(
                db,
                "ng_evidence",
                {
                    "id": "ST001257:matrix-summary:" + kind,
                    "subject_id": subject,
                    "object_id": collection,
                    "relation": "processed_feature_matrix_summary",
                    "layer": "molecular_measurement",
                    "study_id": "N15",
                    "release_id": source,
                    "source_ref": "ST001257/AN002086",
                    "tier": "human_randomized" if kind == "urine" else "composition",
                    "inference": "descriptive",
                    "tissue": "24-hour urine" if kind == "urine" else "food preparation",
                    "value_status": "qualitative",
                    "n_people": 19 if kind == "urine" else None,
                    "n_samples": report[kind],
                    "context_json": json.dumps(report, sort_keys=True),
                },
            )
        errors = validate(db)
        if errors:
            raise ValueError(errors)
    report["observed_measurement_rows"] = db.execute(
        "SELECT COUNT(*) FROM ng_molecular_measurement m JOIN ng_molecular_sample s ON s.id=m.sample_id WHERE s.dataset_id='ST001257'"
    ).fetchone()[0]
    report["food_urine_feature_paths"] = db.execute(
        "SELECT COUNT(*) FROM ng_food_urine_feature_path WHERE dataset_id='ST001257'"
    ).fetchone()[0]
    report["integrity"] = db.execute("PRAGMA integrity_check").fetchone()[0]
    report["foreign_key_violations"] = len(db.execute("PRAGMA foreign_key_check").fetchall())
    report["health_effects_added"] = 0
    report["named_metabolite_identities_added"] = 0
    (ROOT / "reports/st001257_validation.json").write_text(json.dumps(report, indent=2) + "\n")
    db.close()
    print(json.dumps({k: v for k, v in report.items() if k != "conflicts"}))


if __name__ == "__main__":
    main()
