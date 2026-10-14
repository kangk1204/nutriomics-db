"""Validate partial processed files, preserving unit and sample-alias conflicts."""

import gzip
import hashlib
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import backup, connect, validate
from scripts.import_continuation import release

ROOT = Path("research/2026-09-30/expansion")
CAP = ROOT / "continuation"


def parse(raw, study):
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        expanded = stream.read(64_000_001)
    if len(expanded) > 64_000_000:
        raise ValueError("Decompression cap")
    if study == "GSE323980":
        frame = pd.read_csv(io.BytesIO(expanded), dtype={"Geneid": "string"})
        if len(frame.columns) != 2 or frame.columns[0] != "Geneid":
            raise ValueError("Unexpected RNA-seq columns")
    else:
        frame = pd.read_csv(
            io.BytesIO(expanded),
            sep="\t",
            header=None,
            names=["source_gene_annotation", "source_reported_counts", "source_reported_fraction"],
            dtype={"source_gene_annotation": "string"},
        )
    if (
        len(frame) > 1_000_000
        or frame.iloc[:, 0].isna().any()
        or frame.iloc[:, 0].duplicated().any()
    ):
        raise ValueError("Missing/duplicate/capped source features")
    values = frame.iloc[:, 1:].to_numpy(dtype=float)
    if np.isinf(values).any() or (values < 0).any():
        raise ValueError("Invalid measurement")
    report = {
        "n_features": len(frame),
        "n_value_columns": len(frame.columns) - 1,
        "missing_values": int(np.isnan(values).sum()),
        "reported_zero_values": int((values == 0).sum()),
        "fractional_values": int(((values != np.floor(values)) & np.isfinite(values)).sum()),
        "columns": list(frame.columns),
        "imputation_performed": False,
        "effect_analysis_performed": False,
    }
    if study == "GSE323980":
        ensembl = frame.iloc[:, 0].str.fullmatch(r"ENSG\d+(?:\.\d+)?")
        report.update(
            ensembl_shaped_identifiers=int(ensembl.sum()),
            unresolved_identifiers=int((~ensembl).sum()),
            feature_namespace="Mixed source identifiers; Ensembl-shaped IDs not mapped; other identifiers quarantined from gene joins",
        )
    else:
        report["feature_namespace"] = (
            "Source HMP microbial gene IDs plus annotations; no human gene mapping"
        )
    return frame, report


def main():
    path = ROOT / "nutrition_evidence.sqlite3"
    before = ROOT / "before-candidate-subset.sqlite3"
    if not before.exists():
        backup(path, before)
    db = connect(path)
    artifacts = {}
    rows = json.loads((CAP / "subset-manifest.json").read_text())
    with db:
        for row in rows:
            raw = (CAP / row["file"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != row["sha256"]:
                raise ValueError("Input checksum mismatch")
            frame, meta = parse(raw, row["study"])
            output = CAP / (row["accession"] + ".parquet")
            if output.exists():
                old = pd.read_parquet(output)
                if not old.equals(frame):
                    raise ValueError("Stored output differs from source replay")
            else:
                frame.to_parquet(output, index=False)
            source = release(db, row, "GEO-processed-sample", row["retrieved_at"])
            meta.update(
                accession=row["accession"],
                release_id=source,
                path=str(output),
                input_sha256=row["sha256"],
                sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
                raw_or_derived="Source processed supplementary file; local conversion only",
                unit_status="unresolved",
                analysis_ready=False,
                warning="CSV alias AMa68A/B differs from GEO D01A/B; sample linkage/normalization codebook missing"
                if row["study"] == "GSE323980"
                else "Source describes total reads, but first quantitative column contains fractions; scaling unspecified. Human gut microbial features, not human genes.",
            )
            artifacts.setdefault(row["study"], []).append(meta)
            db.execute(
                "INSERT OR IGNORE INTO ng_rights VALUES (?,?,?,?,?,?,?,?)",
                (
                    source,
                    "quarantined",
                    None,
                    row["url"],
                    row["sha256"],
                    "GEO submitters",
                    "Access public; source-specific redistribution conditions unverified",
                    datetime.now(UTC).isoformat(),
                ),
            )
        for accession, values in artifacts.items():
            dataset = db.execute(
                "SELECT * FROM ng_dataset WHERE accession=?", (accession,)
            ).fetchone()
            meta = json.loads(dataset["metadata_json"])
            meta["partial_measurement_artifacts"] = values
            meta["partial_ingestion"] = {
                "captured_assays": len(values),
                "deposited_assays": dataset["n_assays"],
                "complete_cohort": False,
                "analysis_ready": False,
            }
            db.execute(
                "UPDATE ng_dataset SET ingest_status=?,metadata_json=? WHERE id=?",
                ("measurements", json.dumps(meta, sort_keys=True), dataset["id"]),
            )
    if validate(db):
        raise ValueError(validate(db))
    db.close()
    (ROOT / "reports/candidate_subset_validation.json").write_text(
        json.dumps(artifacts, indent=2), encoding="utf-8"
    )
    print(json.dumps(artifacts, indent=2))


if __name__ == "__main__":
    main()
