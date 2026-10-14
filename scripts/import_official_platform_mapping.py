"""Join bounded official platform annotations to actual acquired measurements."""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd

from nutrigenomics.context_joins import put_context
from nutrigenomics.expansion import backup, connect, insert, validate
from nutrigenomics.platform_mapping import migrate_platform, parse_annotation, validate_platform

ROOT = Path("research/2026-09-30/expansion")
CAP = ROOT / "platform-annotations"
DATASETS = {
    "GPL7020": {"GSE15284": "continuation/GSE15284.parquet"},
    "GPL11532": {"GSE32357": "cohort-primary/GSE32357/GSE32357.parquet"},
    "GPL570": {
        "GSE27385": "measurements/GSE27385.parquet",
    },
}


def main():
    sources = json.loads((CAP / "capture-manifest.json").read_text())
    if (CAP / "GPL7020-manifest.json").exists():
        sources.append(json.loads((CAP / "GPL7020-manifest.json").read_text()))
    planned, reports = [], {}
    for row in sources:
        platform = row["file"].split(".")[0]
        raw = (CAP / row["file"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ValueError("Official annotation checksum mismatch")
        per_dataset = {}
        for accession, filename in DATASETS[platform].items():
            frame = pd.read_parquet(ROOT / filename, columns=["ID_REF"])
            features = set(frame["ID_REF"].astype(str))
            parsed, report = parse_annotation(raw, platform, features)
            per_dataset[accession] = (parsed, report)
            reports[accession] = report
        planned.append((platform, row, per_dataset))
    path = ROOT / "nutrition_evidence.sqlite3"
    if not (ROOT / "before-platform-mapping.sqlite3").exists():
        backup(path, ROOT / "before-platform-mapping.sqlite3")
    db = connect(path)
    migrate_platform(db)
    with db:
        invalid = db.execute(
            "SELECT count(*) FROM ng_feature_gene WHERE dataset_id='GSE15284' AND platform_id='GPL570' AND release_id='GEO-annotation:d7cd44352127b1e34f3a'"
        ).fetchone()[0]
        if invalid:
            (ROOT / "reports/rejected_cross_platform_mapping.json").write_text(
                json.dumps(
                    {
                        "dataset": "GSE15284",
                        "rejected_platform": "GPL570",
                        "actual_platform": "GPL7020",
                        "removed_mapping_rows": invalid,
                        "reason": "Shared probe labels do not establish source-platform identity",
                        "quarantined_before_rejection": True,
                    },
                    indent=2,
                )
            )
            db.execute(
                "DELETE FROM ng_feature_gene WHERE dataset_id='GSE15284' AND platform_id='GPL570' AND release_id='GEO-annotation:d7cd44352127b1e34f3a'"
            )
        for platform, row, datasets in planned:
            release = "GEO-annotation:" + row["sha256"][:20]
            insert(
                db,
                "ng_release",
                {
                    "id": release,
                    "source": "NCBI GEO official platform annotation",
                    "version": next(iter(datasets.values()))[1]["annotation_date"],
                    "url": row["url"],
                    "retrieved_at": row["retrieved_at"],
                    "sha256": row["sha256"],
                    "local_path": str(CAP / row["file"]),
                    "license": "Public read access; source-specific redistribution unverified",
                    "access_status": "public",
                },
            )
            put_context(
                db,
                "ng_rights",
                {
                    "release_id": release,
                    "status": "quarantined",
                    "attribution": "NCBI GEO annotation / original platform providers",
                    "basis": "Historical 2016 source annotation; no current identifier status or redistribution grant assumed",
                    "reviewed_at": row["retrieved_at"],
                },
            )
            for accession, (parsed, report) in datasets.items():
                dataset = db.execute(
                    "SELECT metadata_json FROM ng_dataset WHERE accession=?", (accession,)
                ).fetchone()
                metadata = json.loads(dataset[0])
                declared = metadata.get("platform_id", metadata.get("platform"))
                if declared not in ([platform], platform):
                    raise ValueError("Dataset-platform identity mismatch: " + accession)
                records = []
                for annotation in parsed:
                    for gene in annotation["gene_identifiers"]:
                        identifier = "NCBIGene:" + gene
                        old = db.execute(
                            "SELECT kind,namespace,identifier FROM ng_entity WHERE id=?",
                            (identifier,),
                        ).fetchone()
                        if old:
                            if tuple(old) != ("gene", "NCBIGene", gene):
                                raise ValueError("Existing gene identifier conflict")
                        else:
                            insert(
                                db,
                                "ng_entity",
                                {
                                    "id": identifier,
                                    "kind": "gene",
                                    "label": "NCBI Gene " + gene,
                                    "namespace": "NCBIGene",
                                    "identifier": gene,
                                    "metadata_json": json.dumps(
                                        {
                                            "current_status_unverified": True,
                                            "first_observed_source": release,
                                            "source_annotation_date": annotation["annotation_date"],
                                        },
                                        sort_keys=True,
                                    ),
                                },
                            )
                        count = len(annotation["gene_identifiers"])
                        records.append(
                            (
                                accession,
                                platform,
                                annotation["feature_id"],
                                identifier,
                                count,
                                "source_single_gene" if count == 1 else "source_multiple_genes",
                                release,
                                annotation["annotation_date"],
                                annotation["source_gene_field"],
                                platform + "#ID=" + annotation["feature_id"],
                            )
                        )
                if len(records) > 150_000:
                    raise ValueError("Per-dataset mapping row cap exceeded")
                existing = db.execute(
                    "SELECT * FROM ng_feature_gene WHERE dataset_id=? AND release_id=?",
                    (accession, release),
                ).fetchall()
                if existing:
                    if {tuple(record) for record in existing} != set(records):
                        raise ValueError("Conflicting repeat platform mapping")
                else:
                    db.executemany(
                        "INSERT INTO ng_feature_gene VALUES (?,?,?,?,?,?,?,?,?,?)", records
                    )
                report["mapping_rows"] = len(records)
                report["release_id"] = release
        if validate(db) + validate_platform(db):
            raise ValueError(validate(db) + validate_platform(db))
    report = {
        "datasets": reports,
        "total_mapping_rows": db.execute("SELECT count(*) FROM ng_feature_gene").fetchone()[0],
        "joined_to_measured_features_only": True,
        "ambiguous_mappings_preserved": True,
        "inference_performed": False,
        "source_rights_quarantined": True,
        "gene_expression_effects_imported": False,
    }
    (ROOT / "reports/platform_mapping_validation.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
