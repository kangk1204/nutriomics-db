"""Versioned probe-to-source-GeneID annotations; no effects or GO imports."""

import csv
import gzip
import io
import re

SCHEMA = """
CREATE TABLE IF NOT EXISTS ng_feature_gene (
 dataset_id TEXT NOT NULL REFERENCES ng_dataset(id), platform_id TEXT NOT NULL,
 feature_id TEXT NOT NULL, gene_id TEXT NOT NULL REFERENCES ng_entity(id),
 n_genes_for_feature INTEGER NOT NULL CHECK(n_genes_for_feature>0),
 mapping_status TEXT NOT NULL CHECK(mapping_status IN ('source_single_gene','source_multiple_genes')),
 release_id TEXT NOT NULL REFERENCES ng_release(id), annotation_date TEXT NOT NULL,
 source_gene_field TEXT NOT NULL, source_ref TEXT NOT NULL,
 PRIMARY KEY(dataset_id,platform_id,feature_id,gene_id,release_id)
);
CREATE INDEX IF NOT EXISTS ng_feature_gene_gene ON ng_feature_gene(gene_id,dataset_id);
"""


def migrate_platform(db):
    db.executescript(SCHEMA)
    with db:
        db.execute(
            "INSERT OR IGNORE INTO ng_migration VALUES ('nutrition-platform-gene-v1',datetime('now'))"
        )


def validate_platform(db):
    import json

    errors = []
    for row in db.execute(
        "SELECT DISTINCT f.dataset_id,f.platform_id,d.metadata_json FROM ng_feature_gene f JOIN ng_dataset d ON d.id=f.dataset_id"
    ):
        metadata = json.loads(row[2])
        declared = metadata.get("platform_id", metadata.get("platform"))
        if declared not in ([row[1]], row[1]):
            errors.append(row[0] + ": source platform mismatch")
    for row in db.execute(
        "SELECT DISTINCT f.gene_id,e.kind,e.namespace,e.identifier FROM ng_feature_gene f JOIN ng_entity e ON e.id=f.gene_id"
    ):
        if row[1] != "gene" or row[2] != "NCBIGene" or not re.fullmatch(r"[1-9]\d*", row[3]):
            errors.append(row[0] + ": incompatible source GeneID namespace")
    for row in db.execute(
        "SELECT dataset_id,platform_id,feature_id,release_id,n_genes_for_feature,count(*) FROM ng_feature_gene GROUP BY dataset_id,platform_id,feature_id,release_id HAVING n_genes_for_feature!=count(*)"
    ):
        errors.append(row[0] + ":" + row[2] + ": mapping multiplicity mismatch")
    return errors


def parse_annotation(raw, expected_platform, measured_features, max_expanded=128_000_000):
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        expanded = stream.read(max_expanded + 1)
    if len(expanded) > max_expanded:
        raise ValueError("Annotation decompression cap exceeded")
    text = expanded.decode("utf-8")
    header = text.split("!platform_table_begin", 1)[0]
    if (
        f"!Annotation_platform = {expected_platform}\n" not in header
        or "!Annotation_platform_organism = Homo sapiens" not in header
    ):
        raise ValueError("Platform or organism mismatch")
    dates = re.findall(r"^!Annotation_date = (.+)$", header, re.MULTILINE)
    if (
        len(dates) != 1
        or text.count("!platform_table_begin") != 1
        or text.count("!platform_table_end") != 1
    ):
        raise ValueError("Annotation date/table boundary ambiguity")
    table = text.split("!platform_table_begin\n", 1)[1].split("!platform_table_end", 1)[0]
    reader = csv.DictReader(io.StringIO(table), delimiter="\t")
    if not {"ID", "Gene ID"} <= set(reader.fieldnames or []):
        raise ValueError("Missing explicit source Gene ID column")
    seen, rows, unmapped, ambiguous = set(), [], [], []
    for index, row in enumerate(reader):
        if index >= 100_000:
            raise ValueError("Annotation feature cap")
        feature = row["ID"]
        if feature in seen:
            raise ValueError("Duplicate platform feature")
        seen.add(feature)
        if feature not in measured_features:
            continue
        field = row["Gene ID"].strip()
        if not field:
            unmapped.append(feature)
            continue
        genes = sorted({part.strip() for part in field.split("///")})
        if any(not re.fullmatch(r"[1-9]\d*", gene) for gene in genes):
            raise ValueError("Source Gene ID is not a numeric Entrez identifier")
        if len(genes) > 50:
            raise ValueError("Unreviewed excessive mapping multiplicity")
        if len(genes) > 1:
            ambiguous.append(feature)
        rows.append(
            {
                "feature_id": feature,
                "gene_identifiers": genes,
                "source_gene_field": field,
                "annotation_date": dates[0],
            }
        )
    missing_from_annotation = sorted(measured_features - seen)
    return rows, {
        "annotation_date": dates[0],
        "measured_features": len(measured_features),
        "annotated_features": len(rows),
        "source_unmapped_features": len(unmapped),
        "source_multiple_gene_features": len(ambiguous),
        "features_absent_from_annotation": len(missing_from_annotation),
        "absent_feature_examples": missing_from_annotation[:20],
        "mapping_namespace": "NCBI Entrez Gene numeric identifier",
        "current_gene_status_unverified": True,
        "GO_annotations_not_imported": True,
    }
