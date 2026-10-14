"""Identifier and trial-context joins without individual-outcome attribution."""

import csv
import io
import json
import zipfile

SCHEMA = """
CREATE TABLE IF NOT EXISTS ng_nutrient_definition (
 entity_id TEXT PRIMARY KEY REFERENCES ng_entity(id), fdc_id TEXT NOT NULL,
 nutrient_number TEXT NOT NULL, source_name TEXT NOT NULL, unit TEXT NOT NULL,
 release_id TEXT NOT NULL REFERENCES ng_release(id), source_ref TEXT NOT NULL,
 UNIQUE(fdc_id,nutrient_number)
);
CREATE TABLE IF NOT EXISTS ng_study_link (
 id TEXT PRIMARY KEY, from_study TEXT NOT NULL REFERENCES ng_study(id),
 to_study TEXT NOT NULL REFERENCES ng_study(id), relation TEXT NOT NULL,
 review_status TEXT NOT NULL CHECK(review_status IN ('verified','candidate')),
 participant_join_allowed INTEGER NOT NULL DEFAULT 0 CHECK(participant_join_allowed=0),
 release_id TEXT NOT NULL REFERENCES ng_release(id), basis TEXT NOT NULL,
 UNIQUE(from_study,to_study,relation)
);
CREATE TABLE IF NOT EXISTS ng_arm (
 id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES ng_study(id),
 exposure_id TEXT NOT NULL REFERENCES ng_entity(id), source_label TEXT NOT NULL,
 dose REAL, dose_unit TEXT, duration TEXT,
 release_id TEXT NOT NULL REFERENCES ng_release(id), context_json TEXT NOT NULL,
 CHECK((dose IS NULL AND dose_unit IS NULL) OR (dose IS NOT NULL AND dose_unit IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS ng_assay_context (
 accession TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES ng_dataset(id),
 participant_key TEXT NOT NULL, arm_id TEXT NOT NULL REFERENCES ng_arm(id),
 period_code TEXT, time_label TEXT, tissue TEXT NOT NULL, organism TEXT NOT NULL,
 processing TEXT NOT NULL, unit_status TEXT NOT NULL,
 release_id TEXT NOT NULL REFERENCES ng_release(id), source_ref TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ng_arm_component (
 id TEXT PRIMARY KEY, arm_id TEXT NOT NULL REFERENCES ng_arm(id),
 compound_id TEXT NOT NULL REFERENCES ng_entity(id), source_label TEXT NOT NULL,
 nominal_dose REAL NOT NULL CHECK(nominal_dose>=0), dose_unit TEXT NOT NULL,
 release_id TEXT NOT NULL REFERENCES ng_release(id),
 structure_release_id TEXT NOT NULL REFERENCES ng_release(id),
 chemical_form_measured INTEGER NOT NULL CHECK(chemical_form_measured=0),
 molecule_effect_attribution INTEGER NOT NULL CHECK(molecule_effect_attribution=0),
 context_json TEXT NOT NULL
);
"""


def migrate_context(db):
    db.executescript(SCHEMA)
    with db:
        db.execute(
            "INSERT OR IGNORE INTO ng_migration VALUES ('nutrition-context-joins-v1',datetime('now'))"
        )
        db.execute(
            "INSERT OR IGNORE INTO ng_migration VALUES ('nutrition-arm-components-v1',datetime('now'))"
        )


def unit_key(value):
    return value.strip().upper().replace("µ", "U").replace("Μ", "U").replace("MCG", "UG")


def put_context(db, table, row):
    """Idempotent context insert; never silently replace a reviewed record."""
    keys = {
        "ng_arm_component": "id",
        "ng_arm": "id",
        "ng_study_link": "id",
        "ng_assay_context": "accession",
        "ng_rights": "release_id",
    }
    if table not in keys:
        raise ValueError("Unreviewed context table")
    allowed = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
    if not row or not set(row) <= allowed:
        raise ValueError("Unknown context columns")
    key = keys[table]
    existing = db.execute(f"SELECT * FROM {table} WHERE {key}=?", (row[key],)).fetchone()
    if existing:
        if any(existing[name] != value for name, value in row.items()):
            raise ValueError("Conflicting context: " + str(row[key]))
        return
    db.execute(
        f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
        list(row.values()),
    )


def nutrient_codebook(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        matches = [
            row for row in archive.infolist() if row.filename.split("/")[-1] == "nutrient.csv"
        ]
        if len(matches) != 1 or matches[0].file_size > 100_000:
            raise ValueError("Expected one bounded official nutrient.csv")
        payload = archive.read(matches[0])
    rows = list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"))))
    result = {}
    for row in rows:
        if not all(row.get(key) for key in ["id", "name", "unit_name"]):
            raise ValueError("Incomplete source definition")
        if row["id"] in result:
            raise ValueError("Duplicate source nutrient ID")
        result[row["id"]] = row
    return result, payload


def verify_definition(entity, definition):
    metadata = json.loads(entity["metadata_json"])
    if entity["identifier"] != definition["id"]:
        raise ValueError("Nutrient ID mismatch")
    if str(metadata["number"]) != definition["nutrient_nbr"]:
        raise ValueError("Nutrient number mismatch")
    if unit_key(metadata["unit"]) != unit_key(definition["unit_name"]):
        raise ValueError("Nutrient unit mismatch")
    # Renamed source labels remain visible; source numeric keys establish identity.
    return {
        "fdc_id": definition["id"],
        "nutrient_number": definition["nutrient_nbr"],
        "source_name": definition["name"],
        "unit": definition["unit_name"],
        "label_changed": entity["label"] != definition["name"],
    }


def trial_context_health(db, dataset_id):
    """Trial-level view: never multiply an aggregate effect over participants."""
    dataset = db.execute("SELECT study_id FROM ng_dataset WHERE id=?", (dataset_id,)).fetchone()
    if not dataset:
        raise ValueError("Unknown dataset")
    links = db.execute(
        "SELECT * FROM ng_study_link WHERE from_study=? AND review_status='verified'",
        (dataset["study_id"],),
    ).fetchall()
    results = []
    for link in links:
        if link["participant_join_allowed"] != 0:
            raise ValueError("Individual linkage not authorized by source")
        effects = db.execute(
            "SELECT * FROM ng_evidence WHERE study_id=? AND layer='health_outcome' AND relation!='reported_arm_mean'",
            (link["to_study"],),
        ).fetchall()
        for effect in effects:
            arms = db.execute(
                "SELECT id FROM ng_arm WHERE study_id=? AND exposure_id=?",
                (dataset["study_id"], effect["subject_id"]),
            ).fetchall()
            if not arms:
                continue
            results.append(
                {
                    "aggregate_evidence_id": effect["id"],
                    "subject_id": effect["subject_id"],
                    "outcome_id": effect["object_id"],
                    "effect": effect["effect"],
                    "unit": effect["effect_unit"],
                    "ci_low": effect["ci_low"],
                    "ci_high": effect["ci_high"],
                    "source_study_id": link["to_study"],
                    "omics_study_id": dataset["study_id"],
                    "join_level": "named_trial_and_arm_context_only",
                    "individual_outcome_join": False,
                    "molecule_effect_attribution": False,
                    "endpoint_class": json.loads(effect["context_json"])["endpoint_class"],
                    "source_ref": effect["source_ref"],
                    "estimate_relation": effect["relation"],
                }
            )
    quantitative = {
        (r["source_study_id"], r["subject_id"], r["outcome_id"], r["source_ref"].split("#")[0])
        for r in results
        if r["effect"] is not None
    }
    kept = []
    for row in results:
        key = (
            row["source_study_id"],
            row["subject_id"],
            row["outcome_id"],
            row["source_ref"].split("#")[0],
        )
        if row["effect"] is None and key in quantitative:
            continue
        row["corroborating_qualitative_evidence_ids"] = (
            [
                r["aggregate_evidence_id"]
                for r in results
                if r["effect"] is None
                and (
                    r["source_study_id"],
                    r["subject_id"],
                    r["outcome_id"],
                    r["source_ref"].split("#")[0],
                )
                == key
            ]
            if row["effect"] is not None
            else []
        )
        kept.append(row)
    return kept


def validate_context(db):
    errors = []
    for row in db.execute("""SELECT a.accession,d.study_id,r.study_id,a.participant_key
                             FROM ng_assay_context a JOIN ng_dataset d ON d.id=a.dataset_id
                             JOIN ng_arm r ON r.id=a.arm_id"""):
        if row[1] != row[2]:
            errors.append(row[0] + ": cross-study assay arm")
        if not row[3].startswith(row[1] + ":"):
            errors.append(row[0] + ": participant namespace missing")
    return errors


def analyte_reference_paths(db):
    """Inspectable composition-to-mechanism references, never concentration transfer."""
    rows = db.execute("""SELECT c.id composition_id,c.subject_id food_id,c.object_id nutrient_id,
                          c.effect composition_value,c.effect_unit composition_unit,c.value_status,
                          a.id analyte_mapping_id,a.to_id reference_compound,a.method,
                          s.id structure_mapping_id,s.to_id mechanism_compound,s.relation structure_relation,
                          t.id target_evidence_id,t.object_id protein_id,t.relation mechanism_relation,
                          t.release_id mechanism_source
                          FROM ng_evidence c JOIN ng_mapping a ON a.from_id=c.object_id
                          JOIN ng_mapping s ON s.from_id=a.to_id
                          JOIN ng_evidence t ON t.subject_id=s.to_id
                          WHERE c.layer='composition' AND a.review_status='verified'
                          AND a.relation='related' AND a.id LIKE 'USDA-assay-reference:%'
                          AND s.review_status='verified' AND t.layer='molecular_target'""").fetchall()
    results = []
    for row in rows:
        method = json.loads(row["method"])
        if method["quantity_transfer_allowed"] or method["exact_chemical_identity"]:
            raise ValueError(
                "Analyte reference unexpectedly permits chemical identity or dose transfer"
            )
        result = dict(row)
        result.pop("method")
        result.update(
            join_level="analyte_reference_and_curated_mechanism_only",
            compound_amount=None,
            quantity_transfer_allowed=False,
            participant_measurement_join=False,
            health_effect_attribution=False,
        )
        result["biological_process_annotations"] = [
            dict(annotation)
            for annotation in db.execute(
                "SELECT id,object_id,release_id,source_ref,context_json FROM ng_evidence WHERE subject_id=? AND layer='pathway'",
                (row["protein_id"],),
            )
        ]
        result["encoded_gene_references"] = [
            dict(mapping)
            for mapping in db.execute(
                "SELECT id,from_id,release_id,method FROM ng_mapping WHERE to_id=? AND review_status='verified' AND relation='related' AND from_id LIKE 'NCBIGene:%'",
                (row["protein_id"],),
            )
        ]
        if db.execute("SELECT name FROM sqlite_master WHERE name='ng_feature_gene'").fetchone():
            result["measured_dataset_gene_annotation_references"] = [
                dict(mapping)
                for mapping in db.execute(
                    "SELECT f.dataset_id,f.platform_id,f.feature_id,f.gene_id,f.mapping_status,f.annotation_date,f.release_id FROM ng_feature_gene f JOIN ng_mapping g ON g.from_id=f.gene_id WHERE g.to_id=? AND g.review_status='verified' AND g.relation='related'",
                    (row["protein_id"],),
                )
            ]
            result["probe_annotation_presence_is_not_expression_regulation"] = True
            result["dietary_contexts_not_matched"] = True
        results.append(result)
    return results
