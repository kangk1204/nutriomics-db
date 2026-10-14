import csv
import gzip
import json
import sqlite3

import pytest

from nutrigenomics.repository_bridge import build_bridge, validate_affinity


def record():
    return {
        "compound_id": "FULL-STEREO-KEY",
        "bindingdb_inchikey": "FULL-STEREO-KEY",
        "endpoint": "Kd",
        "relation": "=",
        "unit": "nM",
        "value_nm": "100",
        "pkd": "7",
        "reactant_set_id": "123",
        "protein_sequence": "MAA",
    }


def test_exact_kd_conversion():
    assert validate_affinity(record(), {"FULL-STEREO-KEY": {}}) == 100


@pytest.mark.parametrize(
    "field,value",
    [
        ("bindingdb_inchikey", "OTHER"),
        ("endpoint", "IC50"),
        ("relation", "<"),
        ("value_nm", "0"),
        ("value_nm", "nan"),
        ("pkd", "2"),
        ("reactant_set_id", ""),
    ],
)
def test_no_identity_endpoint_censor_or_construct_shortcuts(field, value):
    row = record()
    row[field] = value
    with pytest.raises(ValueError):
        validate_affinity(row, {"FULL-STEREO-KEY": {}})


def test_staging_preserves_zero_species_and_no_quantity_transfer(tmp_path):
    source = tmp_path / "source"
    (source / "raw").mkdir(parents=True)
    (source / "usda_tables").mkdir()
    (source / "bridge_source_manifest.json").write_text(json.dumps({"sources": []}))
    (source / "raw/pubchem_properties.json").write_text(
        json.dumps({"PropertyTable": {"Properties": [{"CID": 1, "InChIKey": "FULL-STEREO-KEY"}]}})
    )

    def write(name, data):
        opener = gzip.open if name.endswith(".gz") else open
        with opener(source / name, "wt", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(data[0]))
            writer.writeheader()
            writer.writerows(data)

    write(
        "verified_compounds.csv",
        [
            {
                "pubchem_cid": "1",
                "compound_id": "FULL-STEREO-KEY",
                "name": "Example",
                "food_basis": "aglycone_equivalent",
            }
        ],
    )
    native = {"NDB_No": "001", "Nutr_no": "2", "Flav_Val": "0", "SE": "", "n": "2"}
    write("usda_tables/FLAV_DAT.csv", [native])
    write("usda_tables/NUTR_DEF.csv", [{"Nutr_no": "2", "Unit": "mg"}])
    write(
        "food_composition.csv.gz",
        [
            dict(
                native,
                compound_id="FULL-STEREO-KEY",
                Long_Desc="Food",
                unit="mg_per_100g_edible_portion",
            )
        ],
    )
    affinity = dict(
        record(),
        uniprot="P00001",
        organism="Mus musculus",
        assay_id="A",
        source_group="assay:B",
        pmid="123",
        doi="",
        curation_source="ChEMBL",
    )
    write("food_native_kd_strict_original_key.csv.gz", [affinity])
    output = tmp_path / "staging.sqlite3"
    result = build_bridge(source, output)
    assert result["source_group_assay_disagreements"] == 1
    with sqlite3.connect(output) as db:
        row = db.execute(
            "SELECT status,species,mapping_relation,quantity_transfer_allowed,health_attribution_allowed FROM candidate_food_target"
        ).fetchone()
        assert row == (
            "reported_zero_detection_status_unknown",
            "Mus musculus",
            "related_aglycone_reference",
            0,
            0,
        )
        assert db.execute("SELECT primary_record_verified FROM affinity").fetchone()[0] == 0
    with pytest.raises(ValueError, match="new output"):
        build_bridge(source, output)
