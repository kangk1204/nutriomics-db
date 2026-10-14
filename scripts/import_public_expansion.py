"""Replay checksum-verified snapshots into the reversible evidence extension."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import backup, connect, coverage, insert, migrate, validate


def soft_fields(text):
    fields = {}
    for line in text.splitlines():
        if line.startswith("!Series_") and " = " in line:
            key, value = line.split(" = ", 1)
            fields.setdefault(key.removeprefix("!Series_"), []).append(value)
    return fields


def entity(db, identifier, kind, label, namespace, source_identifier, metadata=None):
    insert(
        db,
        "ng_entity",
        {
            "id": identifier,
            "kind": kind,
            "label": label,
            "namespace": namespace,
            "identifier": source_identifier,
            "metadata_json": json.dumps(metadata or {}, sort_keys=True),
        },
    )


def source_release(db, row):
    identifier = row["source"] + ":" + row["sha256"][:20]
    insert(
        db,
        "ng_release",
        {
            "id": identifier,
            "source": row["source"],
            "version": row["version"],
            "url": row["url"],
            "retrieved_at": row["retrieved_at"],
            "sha256": row["sha256"],
            "local_path": row["file"],
            "license": row["license"],
            "access_status": row["access_status"],
        },
    )
    return identifier


def import_fdc(db, data, release):
    identifier = "FDC:" + str(data["fdcId"])
    if data["dataType"] not in {"SR Legacy", "Foundation"}:
        raise ValueError("only source-standardized per-100g FDC types are supported")
    entity(
        db,
        identifier,
        "food",
        data["description"],
        "USDA-FDC",
        str(data["fdcId"]),
        {
            "dataType": data["dataType"],
            "publicationDate": data.get("publicationDate"),
            "foodCategory": data.get("foodCategory"),
            "preparation_mapping": "source label retained",
        },
    )
    for item in data.get("foodNutrients", []):
        nutrient = item["nutrient"]
        nid = "FDC-nutrient:" + str(nutrient["id"])
        entity(
            db,
            nid,
            "nutrient",
            nutrient["name"],
            "USDA-FDC-nutrient",
            str(nutrient["id"]),
            {"unit": nutrient["unitName"], "number": nutrient.get("number")},
        )
        amount = item.get("amount")
        derivation = item.get("foodNutrientDerivation") or {}
        code = derivation.get("code", "")
        # Analytical provenance only. Calculated/imputed values never become measured zeros.
        analytical = code.startswith("A") and item.get("dataPoints", 0) > 0
        status = ("measured_zero" if amount == 0 else "measured") if analytical else "imputed"
        if amount is None:
            status = "missing"
        if amount is not None and amount < 0:
            raise ValueError("negative source composition amount")
        insert(
            db,
            "ng_evidence",
            {
                "id": f"{release}:{identifier}:{nid}",
                "subject_id": identifier,
                "object_id": nid,
                "relation": "contains",
                "layer": "composition",
                "release_id": release,
                "source_ref": str(item.get("id", nutrient["id"])),
                "tier": "composition",
                "inference": "descriptive",
                "effect": amount,
                "effect_unit": nutrient["unitName"] + "/100 g",
                "value_status": status,
                "context_json": json.dumps(
                    {
                        "basis": "source edible portion 100 g",
                        "source_derivation": derivation,
                        "source_data_points": item.get("dataPoints"),
                        "source_min": item.get("min"),
                        "source_max": item.get("max"),
                        "value_status_policy": "analytical code A plus positive dataPoints required; otherwise conservatively imputed",
                        "raw_source_amount": amount,
                    },
                    sort_keys=True,
                ),
            },
        )


GEO_GROUPS = {
    "GSE27387": ("N06", "fish-oil-fenofibrate-11", "randomized", 11),
    "GSE27384": ("N06", "fish-oil-fenofibrate-11", "randomized", 11),
    "GSE27385": ("N06", "fish-oil-fenofibrate-11", "randomized", 11),
    "GSE27386": ("N06", "fish-oil-fenofibrate-11", "randomized", 11),
    "GSE15284": ("N07", "FISHGASTRO", "nonrandomized_intervention", None),
    "GSE46761": ("N08", "david-2014-diet", "nonrandomized_intervention", None),
    "GSE323980": ("N09", "ketogenic-balanced-obesity-30d", "randomized", 24),
}


def import_geo(db, accession, raw, release):
    if not raw.startswith("^SERIES = " + accession):
        raise ValueError("GEO accession mismatch")
    fields = soft_fields(raw)
    sid, group, design, people = GEO_GROUPS[accession]
    description = " ".join(fields.get("summary", []) + fields.get("overall_design", []))
    if not description:
        raise ValueError("GEO design metadata missing")
    known_human = accession in GEO_GROUPS
    if not known_human:
        raise ValueError("unreviewed human cohort")
    # N06 uses three tissues and a container, never four independent studies.
    # N07 allocation and unique people are unconfirmed, kept candidate.
    independence = "verified" if sid == "N06" else "candidate"
    title = (
        "Fish oil/fenofibrate crossover across PBMC, adipose and muscle"
        if sid == "N06"
        else fields["title"][0]
    )
    study_row = {
        "id": sid,
        "title": title,
        "population": "overweight/obese human adults"
        if sid == "N06"
        else "human nutrition intervention; see dataset design",
        "design": design,
        "participant_group": group,
        "independence_status": independence,
        "independence_basis": (
            "GEO documents eleven people, same three-period crossover and three tissues; four Series count once; distinct intervention/population from S01-S05"
            if sid == "N06"
            else "distinct study description; participant IDs/overlap and allocation need publication or family metadata reconciliation"
        ),
        "n_people": people,
    }
    insert(db, "ng_study", study_row)
    current = db.execute("SELECT * FROM ng_dataset WHERE id=?", (accession,)).fetchone()
    status = "metadata"
    if current:
        previous = json.loads(current["metadata_json"])
        if "measurement_artifact" in previous:
            fields["measurement_artifact"] = previous["measurement_artifact"]
            status = current["ingest_status"]
    insert(
        db,
        "ng_dataset",
        {
            "id": accession,
            "study_id": sid,
            "accession": accession,
            "release_id": release,
            "modality": "; ".join(fields.get("type", [])),
            "access_status": "public",
            "ingest_status": status,
            "n_assays": len(fields.get("sample_id", [])),
            "metadata_json": json.dumps(fields, sort_keys=True),
        },
    )


def import_pubchem(db, data, release):
    for prop in data["PropertyTable"]["Properties"]:
        cid = str(prop["CID"])
        if not prop.get("InChIKey"):
            raise ValueError("PubChem structure identity missing")
        entity(db, "PubChem:" + cid, "compound", prop["Title"], "PubChem-CID", cid, prop)
        entity(
            db,
            "InChIKey:" + prop["InChIKey"],
            "compound",
            prop["Title"],
            "InChIKey",
            prop["InChIKey"],
        )
        insert(
            db,
            "ng_mapping",
            {
                "id": release + ":" + cid,
                "from_id": "PubChem:" + cid,
                "to_id": "InChIKey:" + prop["InChIKey"],
                "relation": "exact",
                "release_id": release,
                "source_ref": "PropertyTable/Properties/" + cid,
                "method": "source-reported CID and InChIKey, no name matching",
                "review_status": "verified",
            },
        )
        insert(
            db,
            "ng_evidence",
            {
                "id": "identity:" + release + ":" + cid,
                "subject_id": "PubChem:" + cid,
                "object_id": "InChIKey:" + prop["InChIKey"],
                "relation": "same_structure",
                "layer": "molecular_identity",
                "release_id": release,
                "source_ref": "PropertyTable/Properties/" + cid,
                "tier": "identity",
                "inference": "descriptive",
                "value_status": "qualitative",
            },
        )


def import_hall(db, raw, release):
    text = html.unescape(re.sub("<[^>]+>", " ", raw))
    text = re.sub(r"\s+", " ", text)
    if not re.search(r"508\s*[±\u00b1]\s*106", text):
        raise ValueError("publication numeric evidence anchor changed; review required")
    insert(
        db,
        "ng_study",
        {
            "id": "N10",
            "title": "Hall inpatient ultra-processed diet crossover",
            "population": "20 adult inpatients",
            "design": "randomized",
            "n_people": 20,
            "participant_group": "Hall-NCT03407053",
            "independence_status": "verified",
            "independence_basis": "Distinct registered inpatient crossover, different intervention and population from S01-S05 and N06",
            "pubmed_id": "31105044",
            "registration": "NCT03407053",
            "metadata_json": json.dumps(
                {
                    "corrections": ["31269427", "33027677"],
                    "correction_status": "numeric extraction provisional pending erratum text review",
                }
            ),
        },
    )
    # The release is a publication snapshot inspected for metadata. Explicitly
    # store only derived facts after review, not licensed text or participant data.
    insert(
        db,
        "ng_dataset",
        {
            "id": "PMID:31105044",
            "study_id": "N10",
            "accession": "PMID:31105044",
            "release_id": release,
            "modality": "published aggregate endpoints",
            "access_status": "public",
            "ingest_status": "metadata",
            "metadata_json": json.dumps(
                {
                    "individual_data": "not acquired",
                    "corrections_must_be_reviewed_before_effect_use": True,
                }
            ),
        },
    )
    entity(
        db,
        "diet:ultraprocessed",
        "diet",
        "ultra-processed dietary pattern",
        "local-reviewed",
        "ultraprocessed",
    )
    # No estimates are admitted until both published errata are reviewed.
    # Keeping an outcome catalogue does not imply an observed benefit.
    for identifier, label in [
        ("energy_intake", "ad libitum energy intake"),
        ("body_weight", "body weight change"),
    ]:
        entity(
            db,
            "outcome:" + identifier,
            "outcome",
            label,
            "local-reviewed",
            identifier,
            {
                "endpoint_class": "behavioral"
                if identifier == "energy_intake"
                else "anthropometric",
                "clinical_disease_endpoint": False,
            },
        )


def replay(snapshot: Path, database: Path, output: Path):
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    db = connect(database)
    migrate(db)
    imported = []
    with db:
        for row in manifest["files"]:
            path = snapshot / row["file"]
            if path.parent.resolve() != snapshot.resolve():
                raise ValueError("manifest path escapes snapshot")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != row["sha256"] or len(raw) != row["bytes"]:
                raise ValueError("snapshot checksum/size mismatch: " + row["file"])
            release = source_release(db, row)
            if row["source"] == "GEO":
                import_geo(db, path.stem, raw.decode(), release)
            elif row["source"] == "USDA-FDC":
                import_fdc(db, json.loads(raw), release)
            elif row["source"] == "PubChem":
                import_pubchem(db, json.loads(raw), release)
            elif row["source"] == "hall":
                import_hall(db, raw.decode(), release)
            imported.append(row["file"])
        # Total amino-acid content in food is related to a free molecular species,
        # not exactly identical. Never map aggregate protein/fiber to one molecule.
        for nutrient, cid in [(1210, 6305), (1211, 6288), (1213, 6106), (1162, 54670067)]:
            nid, molecule = "FDC-nutrient:" + str(nutrient), "PubChem:" + str(cid)
            if (
                db.execute("SELECT 1 FROM ng_entity WHERE id=?", (nid,)).fetchone()
                and db.execute("SELECT 1 FROM ng_entity WHERE id=?", (molecule,)).fetchone()
            ):
                release = db.execute(
                    "SELECT id FROM ng_release WHERE source='PubChem' AND local_path=?",
                    (f"pubchem-{cid}.json",),
                ).fetchone()[0]
                insert(
                    db,
                    "ng_mapping",
                    {
                        "id": f"curated:{nutrient}:{cid}",
                        "from_id": nid,
                        "to_id": molecule,
                        "relation": "related",
                        "release_id": release,
                        "source_ref": f"FDC nutrient {nutrient}; PubChem CID {cid}",
                        "method": "manual semantic review: source nutrient may aggregate chemical forms or bound amino acids; no exact equivalence",
                        "review_status": "candidate",
                    },
                )
        errors = validate(db)
        if errors:
            raise ValueError(errors)
    output.mkdir(parents=True, exist_ok=True)
    report = coverage(db)
    report.update(
        imported_files=imported,
        download_failures=manifest["failures"],
        blocked_sources=manifest["blocked_sources"],
        baseline_production_database="not accessed; extension sidecar only",
    )
    (output / "coverage.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    for table, filename in [
        ("ng_study", "cohort_inventory.csv"),
        ("ng_dataset", "dataset_inventory.csv"),
        ("ng_mapping", "identifier_mappings.csv"),
    ]:
        cursor = db.execute("SELECT * FROM " + table + " ORDER BY id")
        with (output / filename).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow([col[0] for col in cursor.description])
            writer.writerows(cursor)
    db.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot", type=Path, default=Path("research/2026-09-30/expansion/snapshots")
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("research/2026-09-30/expansion/nutrition_evidence.sqlite3"),
    )
    parser.add_argument(
        "--output", type=Path, default=Path("research/2026-09-30/expansion/reports")
    )
    args = parser.parse_args()
    if args.database.exists():
        # SQLite backup is safe for live WAL databases; never filesystem-copy it.
        backup(
            args.database,
            args.database.with_suffix(
                ".before-import-" + str(__import__("time").time_ns()) + ".sqlite3"
            ),
        )
    report = replay(args.snapshot, args.database, args.output)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
