"""Bounded GEO processed matrix import; values are measurements, never effects."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd

from nutrigenomics.expansion import connect, fetch, insert

ROOT = Path("research/2026-09-30/expansion")
ACCESSION = "GSE27385"
URL = (
    "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE27nnn/GSE27385/matrix/GSE27385_series_matrix.txt.gz"
)


def parse_matrix(raw: bytes, expected_accession: str, expected_samples: set[str]):
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        expanded = stream.read(64_000_001)
    if len(expanded) > 64_000_000:
        raise ValueError("decompressed matrix exceeds 64 MB")
    text = expanded.decode("utf-8")
    if f'"{expected_accession}"' not in text.split("!series_matrix_table_begin")[0]:
        raise ValueError("matrix accession mismatch")
    if text.count("!series_matrix_table_begin") != 1 or text.count("!series_matrix_table_end") != 1:
        raise ValueError("matrix boundaries missing or repeated")
    table = text.split("!series_matrix_table_begin\n", 1)[1].split("!series_matrix_table_end", 1)[0]
    frame = pd.read_csv(io.StringIO(table), sep="\t", dtype={"ID_REF": "string"})
    if len(frame) > 100000 or frame.columns[0] != "ID_REF" or frame["ID_REF"].duplicated().any():
        raise ValueError("invalid feature identifiers or feature cap")
    samples = set(frame.columns[1:])
    if samples != expected_samples:
        raise ValueError("matrix samples do not match reviewed GEO metadata")
    values = frame.iloc[:, 1:].to_numpy(dtype=float)
    import numpy as np

    if np.isinf(values).any():
        raise ValueError("non-finite matrix value")
    return frame, {
        "n_features": len(frame),
        "n_samples": len(samples),
        "missing_values": int(pd.isna(values).sum()),
        "reported_zero_values": int((values == 0).sum()),
        "imputation_performed": False,
        "effect_analysis_performed": False,
        "feature_namespace": "source platform probe ID_REF; gene mappings not inferred",
        "value_unit": "source processed expression intensity; normalization must be audited before analysis",
        "n_people": None,
        "n_people_basis": "Requires sample-level participant audit; array count is not person count",
    }


def main():
    target = ROOT / "measurements"
    target.mkdir(parents=True, exist_ok=True)
    manifest_file = target / "GSE27385.manifest.json"
    db = connect(ROOT / "nutrition_evidence.sqlite3")
    dataset = db.execute("SELECT * FROM ng_dataset WHERE accession=?", (ACCESSION,)).fetchone()
    if not dataset:
        raise ValueError("reviewed dataset must be imported first")
    expected_samples = set(json.loads(dataset["metadata_json"])["sample_id"])
    if manifest_file.exists():
        manifest = json.loads(manifest_file.read_text())
        raw_path = target / "GSE27385_series_matrix.txt.gz"
        if hashlib.sha256(raw_path.read_bytes()).hexdigest() != manifest["input"]["sha256"]:
            raise ValueError("stored matrix checksum mismatch")
        output_path = Path(manifest["output"]["path"])
        if not output_path.resolve().is_relative_to(target.resolve()):
            raise ValueError("matrix artifact path escapes measurement directory")
        if hashlib.sha256(output_path.read_bytes()).hexdigest() != manifest["output"]["sha256"]:
            raise ValueError("stored Parquet checksum mismatch")
    else:
        raw_path = target / "GSE27385_series_matrix.txt.gz"
        source = fetch(URL, raw_path, max_bytes=10_000_000)
        frame, metadata = parse_matrix(raw_path.read_bytes(), ACCESSION, expected_samples)
        parquet_path = target / "GSE27385.parquet"
        frame.to_parquet(parquet_path, index=False)
        manifest = {
            "accession": ACCESSION,
            "input": source,
            "matrix": metadata,
            "output": {
                "path": str(parquet_path),
                "sha256": hashlib.sha256(parquet_path.read_bytes()).hexdigest(),
                "bytes": parquet_path.stat().st_size,
            },
            "parser_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "license": "NCBI molecular data policy; submitter rights may remain",
        }
        manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    # Artifact references belong to dataset metadata, separate from StudyEffect.
    metadata = json.loads(dataset["metadata_json"])
    metadata["measurement_artifact"] = manifest
    release = "GEO-matrix:" + manifest["input"]["sha256"][:20]
    with db:
        insert(
            db,
            "ng_release",
            {
                "id": release,
                "source": "GEO-matrix",
                "version": manifest["input"]["retrieved_at"],
                "url": URL,
                "retrieved_at": manifest["input"]["retrieved_at"],
                "sha256": manifest["input"]["sha256"],
                "local_path": str(raw_path),
                "license": manifest["license"],
                "access_status": "public",
            },
        )
        db.execute(
            "UPDATE ng_dataset SET ingest_status=?, metadata_json=? WHERE id=?",
            ("measurements", json.dumps(metadata, sort_keys=True), dataset["id"]),
        )
    db.close()
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
