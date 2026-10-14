"""Audit repository-derived food/affinity joins without promoting them to evidence."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import sqlite3
from pathlib import Path


def rows(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", newline="") as stream:
        yield from csv.DictReader(stream)


def validate_affinity(row, identities):
    """Original stereochemical key and exact finite Kd; never a prediction."""
    key = row["compound_id"]
    if key not in identities or row["bindingdb_inchikey"] != key:
        raise ValueError("Original complete chemical key mismatch")
    if row["endpoint"] != "Kd" or row["relation"] != "=" or row["unit"] != "nM":
        raise ValueError("Only exact Kd in nM is admitted")
    value = float(row["value_nm"])
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Nonpositive/nonfinite affinity")
    if not math.isclose(float(row["pkd"]), 9 - math.log10(value), abs_tol=1e-10):
        raise ValueError("pKd conversion mismatch")
    if not row["reactant_set_id"] or not row["protein_sequence"]:
        raise ValueError("Missing assay record or protein construct")
    return value


def build_bridge(source: Path, destination: Path):
    if destination.exists():
        raise ValueError("Use a new output path; preserve existing checkpoint")
    manifest = json.loads((source / "bridge_source_manifest.json").read_text())
    verified = []
    for record in manifest["sources"]:
        path = source / "raw" / record["file"]
        if path.exists():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != record["sha256"]:
                raise ValueError("Source hash mismatch: " + path.name)
            verified.append({"file": path.name, "sha256": digest, "url": record.get("url")})
    properties = json.loads((source / "raw/pubchem_properties.json").read_text())
    primary = {str(p["CID"]): p for p in properties["PropertyTable"]["Properties"]}
    identities = {}
    for row in rows(source / "verified_compounds.csv"):
        if primary[row["pubchem_cid"]]["InChIKey"] != row["compound_id"]:
            raise ValueError("PubChem identifier mismatch")
        identities[row["compound_id"]] = row
    original = {(r["NDB_No"], r["Nutr_no"]): r for r in rows(source / "usda_tables/FLAV_DAT.csv")}
    definitions = {r["Nutr_no"]: r for r in rows(source / "usda_tables/NUTR_DEF.csv")}
    destination.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(destination)
    db.executescript("""
    PRAGMA foreign_keys=ON;
    CREATE TABLE compound(inchikey TEXT PRIMARY KEY, cid TEXT UNIQUE NOT NULL, name TEXT, food_basis TEXT);
    CREATE TABLE food_composition(ndb TEXT, nutrient TEXT, compound TEXT REFERENCES compound(inchikey),
      label TEXT, value REAL, unit TEXT, status TEXT, reported_se REAL, reported_n INTEGER,
      source_row_json TEXT, PRIMARY KEY(ndb,nutrient));
    CREATE TABLE affinity(record TEXT PRIMARY KEY, compound TEXT REFERENCES compound(inchikey),
      target_uniprot TEXT, species TEXT, construct_sha256 TEXT, kd_nm REAL CHECK(kd_nm>0),
      pkd REAL, assay_id TEXT, source_group TEXT, pmid TEXT, doi TEXT, curation_source TEXT,
      primary_record_verified INTEGER CHECK(primary_record_verified=0), source_row_json TEXT);
    CREATE TABLE provenance(key TEXT PRIMARY KEY, value TEXT);
    CREATE VIEW candidate_food_target AS SELECT f.*, a.record, a.target_uniprot, a.species,
      a.construct_sha256, a.kd_nm, a.pmid, c.food_basis,
      'quarantined_pending_primary_assay_and_rights_verification' AS release_status,
      0 AS quantity_transfer_allowed, 0 AS health_attribution_allowed,
      CASE WHEN c.food_basis='aglycone_equivalent' THEN 'related_aglycone_reference'
        ELSE 'reported_analyte_reference_stereochemistry_unverified' END AS mapping_relation
      FROM food_composition f JOIN compound c ON c.inchikey=f.compound
      JOIN affinity a ON a.compound=f.compound;
    """)
    with db:
        for key, row in identities.items():
            db.execute(
                "INSERT INTO compound VALUES(?,?,?,?)",
                (key, row["pubchem_cid"], row["name"], row["food_basis"]),
            )
        for row in rows(source / "food_composition.csv.gz"):
            native = original[(row["NDB_No"], row["Nutr_no"])]
            if any(native[k] != row[k] for k in native):
                raise ValueError("Derived food record differs from source table")
            if (
                definitions[row["Nutr_no"]]["Unit"] != "mg"
                or row["unit"] != "mg_per_100g_edible_portion"
            ):
                raise ValueError("Food units mismatch")
            value = float(row["Flav_Val"])
            if not math.isfinite(value) or value < 0:
                raise ValueError("Invalid food concentration")
            db.execute(
                "INSERT INTO food_composition VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    row["NDB_No"],
                    row["Nutr_no"],
                    row["compound_id"],
                    row["Long_Desc"],
                    value,
                    row["unit"],
                    "reported_zero_detection_status_unknown" if value == 0 else "reported_mean",
                    float(row["SE"]) if row["SE"] else None,
                    int(row["n"]) if row["n"] else None,
                    json.dumps(row, sort_keys=True),
                ),
            )
        group_mismatches = 0
        for row in rows(source / "food_native_kd_strict_original_key.csv.gz"):
            value = validate_affinity(row, identities)
            group_mismatches += row["source_group"] != "assay:" + row["assay_id"]
            db.execute(
                "INSERT INTO affinity VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["reactant_set_id"],
                    row["compound_id"],
                    row["uniprot"],
                    row["organism"],
                    hashlib.sha256(row["protein_sequence"].encode()).hexdigest(),
                    value,
                    float(row["pkd"]),
                    row["assay_id"],
                    row["source_group"],
                    row["pmid"],
                    row["doi"],
                    row["curation_source"],
                    0,
                    json.dumps(row, sort_keys=True),
                ),
            )
        derived_hashes = {
            name: hashlib.sha256((source / name).read_bytes()).hexdigest()
            for name in [
                "food_composition.csv.gz",
                "food_native_kd_strict_original_key.csv.gz",
                "verified_compounds.csv",
                "bridge_source_manifest.json",
            ]
        }
        audit = {
            "source_capture_hashes_verified": verified,
            "derived_artifact_sha256": derived_hashes,
            "source_group_assay_disagreements": group_mismatches,
            "redistribution": "quarantined; inherited origin-specific rights not verified",
            "primary_assay_verification": "pending; local processed records are not independent primary replication",
            "health_inference": False,
            "ctd_imported": False,
            "model_predictions_imported": False,
            "food_stereochemistry_verified": False,
        }
        db.execute(
            "INSERT INTO provenance VALUES(?,?)", ("audit", json.dumps(audit, sort_keys=True))
        )
    audit["counts"] = {
        t: db.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
        for t in ["compound", "food_composition", "affinity", "candidate_food_target"]
    }
    audit["integrity"] = db.execute("PRAGMA integrity_check").fetchone()[0]
    audit["foreign_key_violations"] = len(db.execute("PRAGMA foreign_key_check").fetchall())
    db.close()
    return audit
