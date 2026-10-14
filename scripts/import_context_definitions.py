"""Offline official nutrient-key verification; never equates analytes with molecules."""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.context_joins import migrate_context, nutrient_codebook, verify_definition
from nutrigenomics.expansion import backup, connect, insert, validate

ROOT = Path("research/2026-09-30/expansion")


def insert_definition(db, row):
    existing = db.execute(
        "SELECT * FROM ng_nutrient_definition WHERE entity_id=?", (row["entity_id"],)
    ).fetchone()
    if existing:
        if dict(existing) != row:
            raise ValueError("Conflicting nutrient definition")
        return
    db.execute(
        "INSERT INTO ng_nutrient_definition VALUES (?,?,?,?,?,?,?)",
        [
            row[key]
            for key in [
                "entity_id",
                "fdc_id",
                "nutrient_number",
                "source_name",
                "unit",
                "release_id",
                "source_ref",
            ]
        ],
    )


def main():
    cap = ROOT / "context-joins"
    source = json.loads((cap / "definitions-manifest.json").read_text())[0]
    raw = (cap / source["file"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != source["sha256"]:
        raise ValueError("Official codebook capture checksum mismatch")
    definitions, _ = nutrient_codebook(raw)
    db = connect(ROOT / "nutrition_evidence.sqlite3")
    # Validate all keys before any schema or data mutation.
    matched = []
    for entity in db.execute("SELECT * FROM ng_entity WHERE kind='nutrient'"):
        if entity["namespace"] != "USDA-FDC-nutrient":
            raise ValueError("Unreviewed nutrient namespace: " + entity["namespace"])
        matched.append((entity["id"], verify_definition(entity, definitions[entity["identifier"]])))
    checkpoint = ROOT / "before-context-definitions.sqlite3"
    if not checkpoint.exists():
        backup(ROOT / "nutrition_evidence.sqlite3", checkpoint)
    migrate_context(db)
    release = "USDA-codebook:" + source["sha256"][:20]
    with db:
        insert(
            db,
            "ng_release",
            {
                "id": release,
                "source": "USDA FoodData Central",
                "version": "Supporting Data 2022-10-28",
                "url": source["url"],
                "retrieved_at": source["retrieved_at"],
                "sha256": source["sha256"],
                "local_path": str(cap / source["file"]),
                "license": "Public domain / CC0; see inherited USDA rights assessment",
                "access_status": "public",
            },
        )
        for identifier, definition in matched:
            insert_definition(
                db,
                dict(
                    entity_id=identifier,
                    **{
                        key: definition[key]
                        for key in ["fdc_id", "nutrient_number", "source_name", "unit"]
                    },
                    release_id=release,
                    source_ref="nutrient.csv:id=" + definition["fdc_id"],
                ),
            )
        # Propagate the assessed source policy, not a generic public-access assumption.
        policy = db.execute(
            "SELECT r.* FROM ng_rights r JOIN ng_release s ON s.id=r.release_id WHERE r.status='verified' AND s.source LIKE '%USDA%' LIMIT 1"
        ).fetchone()
        if not policy:
            raise ValueError("Missing verified USDA rights assessment")
        rights = dict(policy)
        rights["release_id"] = release
        existing = db.execute("SELECT * FROM ng_rights WHERE release_id=?", (release,)).fetchone()
        if existing and dict(existing) != rights:
            raise ValueError("Conflicting rights assessment")
        if not existing:
            columns = list(rights)
            db.execute(
                "INSERT INTO ng_rights ("
                + ",".join(columns)
                + ") VALUES ("
                + ",".join("?" for _ in columns)
                + ")",
                list(rights.values()),
            )
    errors = validate(db)
    if errors:
        raise ValueError(errors)
    report = {
        "verified_nutrient_keys": len(matched),
        "codebook_source_release": release,
        "renamed_labels": [identifier for identifier, row in matched if row["label_changed"]],
        "basis": "Exact FDC numeric ID + nutrient number + normalized source unit",
        "molecule_equivalence": False,
        "quantity_transfer_to_free_compounds": False,
        "complete_food_molecule_health_chains": 0,
    }
    (ROOT / "reports/context_definition_validation.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
