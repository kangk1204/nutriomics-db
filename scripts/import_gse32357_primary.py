"""Primary GEO import of a distinct paired resveratrol trial; no copied gene effects."""

import csv
import gzip
import hashlib
import io
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.context_joins import migrate_context, put_context, validate_context
from nutrigenomics.expansion import backup, connect, insert, validate
from scripts.import_public_expansion import entity, soft_fields
from scripts.import_trial_context import arm
from scripts.ingest_public_matrix import parse_matrix

ROOT = Path("research/2026-09-30/expansion")
CAP = ROOT / "cohort-primary/GSE32357"


def paired_contexts(raw):
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        expanded = stream.read(64_000_001)
    if len(expanded) > 64_000_000:
        raise ValueError("Matrix decompression cap")
    header = expanded.decode().split("!series_matrix_table_begin")[0]
    fields = {}
    for line in header.splitlines():
        if line.startswith("!Sample_"):
            values = next(csv.reader([line], delimiter="\t"))
            fields.setdefault(values[0], []).append(values[1:])
    samples, titles = fields["!Sample_geo_accession"][0], fields["!Sample_title"][0]
    if len(samples) != len(set(samples)) or len(samples) != len(titles):
        raise ValueError("Sample metadata alignment/uniqueness failed")
    contexts, participants = [], defaultdict(set)
    for index, (sample, title) in enumerate(zip(samples, titles, strict=True)):
        match = re.fullmatch(r"Subject (\d+)_t=30days_(resveratrol|placebo)", title)
        if not match:
            raise ValueError("Unreviewed participant title: " + title)
        participant, treatment = match.groups()
        if treatment in participants[participant]:
            raise ValueError("Repeated participant-treatment assay")
        participants[participant].add(treatment)
        processing = fields["!Sample_data_processing"][0][index]
        protocol = fields["!Sample_treatment_protocol_ch1"][0][index]
        if "RMA" not in processing or "150mg/day trans-resveratrol" not in protocol:
            raise ValueError("Processing or intervention mismatch")
        contexts.append(
            {
                "accession": sample,
                "participant": participant,
                "treatment": treatment.upper(),
                "processing": processing,
                "source_title": title,
                "time_label": "30 days after assigned treatment",
            }
        )
    if not participants or any(
        arms != {"resveratrol", "placebo"} for arms in participants.values()
    ):
        raise ValueError("Incomplete within-person crossover pairs")
    return contexts


def main():
    captures = json.loads((CAP / "manifest.json").read_text())
    for row in captures:
        if hashlib.sha256((CAP / row["file"]).read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("Primary GEO checksum mismatch")
    fields = soft_fields((CAP / "GSE32357.soft").read_text())
    if fields["geo_accession"] != ["GSE32357"] or fields["pubmed_id"] != ["22055504"]:
        raise ValueError("Primary trial identity mismatch")
    raw = (CAP / "GSE32357_series_matrix.txt.gz").read_bytes()
    contexts = paired_contexts(raw)
    frame, measurement = parse_matrix(raw, "GSE32357", set(fields["sample_id"]))
    people = len({row["participant"] for row in contexts})
    if people != 10 or len(contexts) != 20:
        raise ValueError("Source participant count mismatch")
    output = CAP / "GSE32357.parquet"
    if not output.exists():
        frame.to_parquet(output, index=False)
    artifact = dict(
        path=str(output),
        sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
        n_values=measurement["n_features"] * measurement["n_samples"],
        **measurement,
        raw_or_derived="Source RMA/Oligo estimates; only local Parquet conversion",
        raw_CEL_downloaded=False,
    )
    database = ROOT / "nutrition_evidence.sqlite3"
    if not (ROOT / "before-gse32357.sqlite3").exists():
        backup(database, ROOT / "before-gse32357.sqlite3")
    db = connect(database)
    migrate_context(db)
    # Do not reinterpret an existing same-publication record as another cohort.
    duplicates = db.execute(
        "SELECT id FROM ng_study WHERE pubmed_id='22055504' AND id NOT IN ('N14','N14-parent-RCT')"
    ).fetchall()
    if duplicates:
        raise ValueError("Existing same publication requires explicit study reconciliation")
    with db:
        mapping_present = bool(
            db.execute("SELECT name FROM sqlite_master WHERE name='ng_feature_gene'").fetchone()
        ) and bool(
            db.execute(
                "SELECT 1 FROM ng_feature_gene WHERE dataset_id='GSE32357' LIMIT 1"
            ).fetchone()
        )
        old_study = db.execute("SELECT metadata_json FROM ng_study WHERE id='N14'").fetchone()
        if old_study:
            prior_metadata = json.loads(old_study[0])
            if "gene_mapping_not_imported" in prior_metadata:
                prior_metadata["gene_mapping_not_imported"] = not mapping_present
                db.execute(
                    "UPDATE ng_study SET metadata_json=? WHERE id='N14'",
                    (json.dumps(prior_metadata, sort_keys=True),),
                )
        releases = {}
        for row in captures:
            identifier = "GEO-primary:" + row["sha256"][:20]
            insert(
                db,
                "ng_release",
                {
                    "id": identifier,
                    "source": "NCBI GEO primary",
                    "version": row["retrieved_at"],
                    "url": row["url"],
                    "retrieved_at": row["retrieved_at"],
                    "sha256": row["sha256"],
                    "local_path": str(CAP / row["file"]),
                    "license": "Public access; submitter/publication redistribution rights not verified",
                    "access_status": "public",
                },
            )
            put_context(
                db,
                "ng_rights",
                {
                    "release_id": identifier,
                    "status": "quarantined",
                    "attribution": "NCBI GEO / Timmers et al.",
                    "basis": "Original rights require verification; no private repository effect estimates copied",
                    "reviewed_at": row["retrieved_at"],
                },
            )
            releases[row["file"]] = identifier
        source = releases["GSE32357_series_matrix.txt.gz"]
        insert(
            db,
            "ng_study",
            {
                "id": "N14",
                "title": fields["title"][0],
                "population": "10 healthy obese men, age 45-65, muscle biopsies",
                "design": "randomized",
                "participant_group": "Timmers2011-GSE32357",
                "independence_status": "verified",
                "independence_basis": "Distinct primary trial/PMID from existing inventory; ten explicitly paired source participants; cross-study roster overlap not assessable",
                "n_people": people,
                "pubmed_id": "22055504",
                "metadata_json": json.dumps(
                    {
                        "cross_study_person_overlap_unknown": True,
                        "study_unit": "one randomized crossover trial",
                        "participant_pairs": 10,
                        "gene_mapping_not_imported": not mapping_present,
                    },
                    sort_keys=True,
                ),
            },
        )
        insert(
            db,
            "ng_dataset",
            {
                "id": "GSE32357",
                "study_id": "N14",
                "accession": "GSE32357",
                "release_id": source,
                "modality": "Expression profiling by array",
                "access_status": "public",
                "ingest_status": "measurements",
                "n_assays": len(contexts),
                "metadata_json": json.dumps(
                    {
                        "platform": "GPL11532",
                        "measurement_artifact": artifact,
                        "source_metadata_release": releases["GSE32357.soft"],
                        "analysis_ready": False,
                        "source_log_base_not_explicit": True,
                        "participant_health_crosswalk_unavailable": True,
                        "repository_lead_commit": "9ca37db477b34ca8a54f986b006c69424dedb81e",
                        "repository_source_hash_matches": (
                            hashlib.sha256(raw).hexdigest()
                            == "3caa19c7a64c1fb467f88c7e246cf1f67fad14bb63672bae0ada79532df8e1d5"
                        ),
                    },
                    sort_keys=True,
                ),
            },
        )
        for label in ["RESVERATROL", "PLACEBO"]:
            exposure = "TrialExposure:GSE32357:" + label
            entity(
                db,
                exposure,
                "diet",
                label.lower() + " intervention",
                "TrialExposure",
                "GSE32357:" + label,
                {
                    "trans_resveratrol_reported": (label == "RESVERATROL"),
                    "molecule_structure_not_imported": True,
                },
            )
            arm(
                db,
                "N14",
                label,
                exposure,
                150 if label == "RESVERATROL" else None,
                "mg/day trans-resveratrol" if label == "RESVERATROL" else None,
                "30 days",
                source,
            )
        for row in contexts:
            put_context(
                db,
                "ng_assay_context",
                {
                    "accession": row["accession"],
                    "dataset_id": "GSE32357",
                    "participant_key": "N14:" + row["participant"],
                    "arm_id": "N14:" + row["treatment"],
                    "time_label": row["time_label"],
                    "tissue": "vastus lateralis muscle",
                    "organism": "Homo sapiens",
                    "processing": row["processing"],
                    "unit_status": "source RMA expression estimate; log base not explicitly given",
                    "release_id": source,
                    "source_ref": "GSE32357#Sample:" + row["accession"],
                },
            )
        errors = validate(db) + validate_context(db)
        if errors:
            raise ValueError(errors)
    report = {
        "study_id": "N14",
        "accession": "GSE32357",
        "n_people": people,
        "n_arrays": len(contexts),
        "artifact": artifact,
        "processing": "RMA / Oligo v1.16.0",
        "treatment": "150 mg/day trans-resveratrol, 30 days",
        "paired_within_person": True,
        "participant_matched_health": False,
        "source_rights_quarantined": True,
        "validation_errors": [],
    }
    (ROOT / "reports/GSE32357_primary_validation.json").write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                "n_people": people,
                "n_arrays": len(contexts),
                "n_values": artifact["n_values"],
                "missing": artifact["missing_values"],
                "zeros": artifact["reported_zero_values"],
                "repository_effects_imported": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
