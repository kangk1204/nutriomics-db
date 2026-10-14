from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nutrigenomics.annotations import annotation_provenance, mechanisms_for_keys, search_annotations
from nutrigenomics.expansion import connect, insert, migrate, validate
from scripts.collect_bridge_annotations import (
    COMPOUNDS,
    FHS_URL,
    activity_rejection,
    chembl_assays,
    chemical_identities,
    fhs_eqtms,
    fhs_row,
)


@pytest.fixture
def annotation_db(tmp_path):
    path = tmp_path / "annotations.sqlite3"
    db = connect(path)
    migrate(db)
    yield db, path
    db.close()


class FakeSnapshots:
    """Deterministic source fixtures, never presented as live collected evidence."""
    def __init__(self, folder):
        self.folder = folder
        self.failures = []

    def fetch(self, url, slug, extension=".json"):
        if "pubchem" in url:
            data = {"PropertyTable": {"Properties": [
                {"CID": c["cid"], "InChIKey": c["inchikey"], "Title": c["label"]}
                for c in COMPOUNDS]}}
        elif "status.json" in url:
            data = {"chembl_db_version": "CHEMBL_TEST"}
        elif "molecule.json" in url:
            data = {"molecules": [{"molecule_chembl_id": "CHEMBL_EPA",
                "molecule_structures": {"standard_inchi_key": COMPOUNDS[0]["inchikey"]}}]}
        elif "activity.json" in url:
            data = {"page_meta": {"next": None}, "activities": [
                {"activity_id": 1, "assay_chembl_id": "ASSAY_B", "target_chembl_id": "TARGET",
                 "target_tax_id": "9606", "assay_type": "B", "standard_units": "nM",
                 "standard_flag": 1, "standard_relation": "=", "standard_value": "100",
                 "standard_type": "Ki", "data_validity_comment": None},
                {"activity_id": 2, "assay_chembl_id": "ASSAY_F", "target_chembl_id": "TARGET",
                 "target_tax_id": "9606", "assay_type": "F", "standard_units": "nM",
                 "standard_flag": 1, "standard_relation": ">", "standard_value": "5000",
                 "standard_type": "EC50", "data_validity_comment": None},
            ]}
        elif "assay.json" in url:
            data = {"page_meta": {"next": None}, "assays": [
                {"assay_chembl_id": key, "confidence_score": 9, "target_chembl_id": "TARGET",
                 "assay_tissue": None, "assay_organism": "Mus musculus"}
                for key in ["ASSAY_B", "ASSAY_F"]]}
        elif "target.json" in url:
            data = {"page_meta": {"next": None}, "targets": [
                {"target_chembl_id": "TARGET", "target_type": "SINGLE PROTEIN", "tax_id": 9606,
                 "organism": "Homo sapiens", "pref_name": "Human receptor"}]}
        elif "gitbook" in url:
            raw = b"CC BY-SA ShareAlike"
            data = None
        elif url == FHS_URL:
            raw = b'https://media.springernature.com/test/39936_MOESM1_ESM.xlsx'
            data = None
        elif url.endswith(".xlsx"):
            path = self.folder / "fhs.xlsx"
            columns = ["CpG", "eGene", "DNA methylation gene", "Estimate", "p-value"]
            with pd.ExcelWriter(path) as writer:
                pd.DataFrame([
                    ["cg00000001", "ENSG00000000001.2", "NEAREST_GENE", -2.0, 0.0001],
                    ["invalid", "ENSG00000000001.2", "NEAREST_GENE", 1.0, 0.1],
                ], columns=columns).to_excel(writer, sheet_name="ST1", startrow=1, index=False)
                pd.DataFrame([
                    ["cg00000002", "ENSG00000000002.3", "OTHER_NEAREST", 3.0, 1e-15],
                ], columns=columns).to_excel(writer, sheet_name="ST2", startrow=1, index=False)
            raw = path.read_bytes()
            data = None
        else:
            raise AssertionError(url)
        if data is not None:
            raw = json.dumps(data).encode()
        path = self.folder / f"{slug}{extension}"
        path.write_bytes(raw)
        return {"url": url, "retrieved_at": "2026-10-01T00:00:00Z", "local_path": str(path),
                "sha256": hashlib.sha256(raw).hexdigest()}, raw

    def failure(self, stage, error):
        self.failures.append((stage, str(error)))


def test_fhs_import_preserves_measured_gene_and_nearest_gene_is_not_a_link(annotation_db, tmp_path):
    db, path = annotation_db
    result = fhs_eqtms(db, FakeSnapshots(tmp_path))
    db.commit()
    assert result["admitted"] == {"cis": 1, "trans": 1}
    assert len(result["exclusions"]) == 1
    assert validate(db) == []
    assert db.execute("SELECT count(*) FROM ng_entity WHERE label='NEAREST_GENE'").fetchone()[0] == 0
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    with Session(engine) as session:
        rows = mechanisms_for_keys(session, {"cg00000001"})
        assert len(rows) == 1
        row = rows[0]
        assert row["object_id"] == "Ensembl:ENSG00000000001.2"
        assert row["inference"] == "association"
        assert row["tissue"] == "whole blood"
        assert row["q_value"] is None
        assert row["n_people"] == 2115
        assert row["context"]["source_row"]["DNA methylation gene"] == "NEAREST_GENE"
        provenance = annotation_provenance(session, row["id"])
        assert provenance["sha256"] == row["provenance"]["sha256"]
        assert provenance["source_ref"] == "Supplementary ST1, row 3"
        assert provenance["inference"] == "association"
        assert provenance["export_allowed"] is True
        release = annotation_provenance(session, row["provenance"]["release_id"])
        assert release["hash_scope"] == "source_snapshot"
        assert release["export_allowed"] is False  # permissions are evidence-specific
        assert annotation_provenance(session, "nonexistent") is None
        assert mechanisms_for_keys(session, {"NEAREST_GENE"}) == []
        assert mechanisms_for_keys(session, {"cg99999999"}) == []
        page = search_annotations(session, layer="molecular_measurement", limit=1)
        assert page["total"] == 2 and len(page["records"]) == 1
        second = search_annotations(session, layer="molecular_measurement", limit=1, offset=1)
        assert second["records"][0]["id"] != page["records"][0]["id"]
    engine.dispose()


def test_invalid_eqtm_values_rejected():
    row = {"CpG": "cg00000001", "eGene": "ENSG00000000001.1", "Estimate": float("inf"), "p-value": 0.01}
    with pytest.raises(ValueError):
        fhs_row(row, "cis")


def test_ch_probe_identifier_is_preserved_without_becoming_a_cpg():
    row = {"CpG": "ch.17.41650897R", "eGene": "ENSG00000185829.17", "Estimate": -2.0, "p-value": 1e-8}
    parsed = fhs_row(row, "cis")
    assert parsed["cpg"] == "ch.17.41650897R"
    assert parsed["probe_kind"] == "non_CpG_CH"


def test_candidate_annotation_provenance_is_not_exposed(annotation_db, tmp_path):
    db, path = annotation_db
    fhs_eqtms(db, FakeSnapshots(tmp_path))
    row = db.execute("SELECT id,context_json FROM ng_evidence ORDER BY id LIMIT 1").fetchone()
    context = json.loads(row["context_json"])
    context["review_status"] = "candidate"
    db.execute("UPDATE ng_evidence SET context_json=? WHERE id=?", (json.dumps(context), row["id"]))
    db.commit()
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    with Session(engine) as session:
        assert annotation_provenance(session, row["id"]) is None
        assert search_annotations(session)["total"] == 1
    engine.dispose()


def test_activity_filters_confidence_and_target_taxonomy_independently():
    activity = {"assay_type": "B", "target_chembl_id": "T", "standard_units": "nM",
                "standard_flag": 1, "standard_value": "10", "standard_relation": "="}
    assay = {"confidence_score": 9, "target_chembl_id": "T", "assay_organism": "Mus musculus"}
    target = {"target_type": "SINGLE PROTEIN", "tax_id": 9606, "organism": "Homo sapiens"}
    assert activity_rejection(activity, assay, target) == []
    assert "target_assignment_not_direct_single_protein" in activity_rejection(activity, {**assay, "confidence_score": 8}, target)
    assert "not_human_target" in activity_rejection(activity, assay, {**target, "tax_id": 10090})
    assert "not_single_protein" in activity_rejection(activity, assay, {**target, "target_type": "PROTEIN FAMILY"})


def test_related_nutrient_assay_stays_qualified_and_function_is_not_binding(annotation_db, tmp_path):
    db, path = annotation_db
    snapshots = FakeSnapshots(tmp_path)
    compounds = chemical_identities(db, snapshots)
    assert chembl_assays(db, snapshots, compounds[:1])["admitted"] == 2
    db.commit()
    assert db.execute("SELECT DISTINCT relation FROM ng_mapping").fetchone()[0] == "related"
    assert validate(db) == []
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    with Session(engine) as session:
        rows = mechanisms_for_keys(session, {"FDC-NUTRIENT:1278"})
        assays = [row for row in rows if row["layer"] == "molecular_target"]
        assert len(assays) == 2
        assert all(row["identity_mapping"]["relation"] == "related" for row in assays)
        functional = next(row for row in assays if row["context"]["activity"]["assay_type"] == "F")
        binding = next(row for row in assays if row["context"]["activity"]["assay_type"] == "B")
        assert functional["effect"] is None
        assert functional["value_status"] == "qualitative"
        assert "functional_assay" in functional["assertion"]
        assert binding["effect"] == 100
        assert binding["context"]["assay"]["assay_organism"] == "Mus musculus"
        assert binding["organism"] == "Homo sapiens"
        assert search_annotations(session, query="EC50")["total"] == 0  # query searches entity/record IDs
    engine.dispose()


def test_mixture_related_mapping_is_not_traversed(annotation_db, tmp_path):
    db, path = annotation_db
    snapshots = FakeSnapshots(tmp_path)
    compounds = chemical_identities(db, snapshots)
    chembl_assays(db, snapshots, compounds[:1])
    rid = db.execute("SELECT id FROM ng_release LIMIT 1").fetchone()[0]
    insert(db, "ng_entity", {"id": "total-fat", "kind": "nutrient", "label": "Total fat",
        "namespace": "test", "identifier": "total-fat", "metadata_json": '{"is_mixture":true}'})
    insert(db, "ng_mapping", {"id": "bad-mixture-mapping", "from_id": "total-fat", "to_id": compounds[0]["compound_id"],
        "relation": "exact", "release_id": rid, "source_ref": "test", "method": "test", "review_status": "verified"})
    db.commit()
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    with Session(engine) as session:
        assert mechanisms_for_keys(session, {"total-fat"}) == []
    engine.dispose()


def test_absent_optional_tables_and_invalid_pagination():
    engine = create_engine("sqlite:///:memory:")
    with Session(engine) as session:
        assert mechanisms_for_keys(session, {"cg00000001"}) == []
        assert search_annotations(session)["total"] == 0
        assert annotation_provenance(session, "nonexistent") is None
        with pytest.raises(ValueError):
            search_annotations(session, limit=201)
    engine.dispose()
