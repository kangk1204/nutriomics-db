import json
import sqlite3

from nutrigenomics.explorer_build import sha256
from scripts.collect_hgnc_identifiers import collect


def test_only_existing_unambiguous_exact_identifiers(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    db = sqlite3.connect(parent / "explorer.sqlite")
    db.execute("CREATE TABLE node(id TEXT,kind TEXT)")
    db.executemany(
        "INSERT INTO node VALUES(?,?)",
        [
            (i, "gene")
            for i in [
                "NCBIGene:1",
                "Ensembl:ENSG00001",
                "NCBIGene:2",
                "Ensembl:ENSG00002",
                "Ensembl:ENSG00003",
                "NCBIGene:4",
                "Ensembl:ENSG00004.1",
            ]
        ],
    )
    db.commit()
    db.close()
    raw = tmp_path / "hgnc.tsv"
    raw.write_text(
        "hgnc_id\tsymbol\tstatus\tentrez_id\tensembl_gene_id\n"
        "HGNC:1\tA\tApproved\t1\tENSG00001\n"
        "HGNC:2\tB\tApproved\t2\tENSG00002\n"
        "HGNC:3\tC\tApproved\t2\tENSG00003\n"
        "HGNC:4\tD\tApproved\t4\tENSG00004\n"
        "HGNC:5\tA\tEntry Withdrawn\t5\tENSG00005\n"
    )
    snapshot = {
        "object_path": str(raw),
        "sha256": sha256(raw),
        "url": "https://example.org/hgnc",
        "retrieved_at": "2026-10-09",
    }
    output = tmp_path / "mapping.sqlite"
    result = collect(snapshot, output, parent)
    assert result["counts"]["verified_exact_cross_references"] == 1
    assert result["counts"]["ambiguous_exact_mapping"] == 2
    assert result["counts"]["one_or_both_identifiers_absent_in_parent"] == 1
    with sqlite3.connect(output) as db:
        row = db.execute("SELECT subject_id,object_id,context_json FROM ng_evidence").fetchone()
    assert row[:2] == ("NCBIGene:1", "Ensembl:ENSG00001")
    assert json.loads(row[2])["export_policy_proof"]["license"] == "CC0-1.0"
