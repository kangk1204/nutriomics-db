"""Provenance-first evidence extension. No transitive health-benefit inference.

The optional SQLite migration only adds ng_* tables. Existing food and omics
tables remain owned by the original application. Can be used in a sidecar first.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import sqlite3
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

VERSION = "nutrition-evidence-v1"
SCHEMA = """
CREATE TABLE ng_release (
 id TEXT PRIMARY KEY, source TEXT NOT NULL, version TEXT NOT NULL,
 url TEXT NOT NULL, retrieved_at TEXT NOT NULL, sha256 TEXT NOT NULL,
 local_path TEXT NOT NULL, license TEXT NOT NULL, access_status TEXT NOT NULL
 CHECK(access_status IN ('public','metadata_only','controlled','agreement_required')),
 UNIQUE(source,version,sha256)
);
CREATE TABLE ng_entity (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN
 ('food','nutrient','compound','gene','protein','pathway','molecular_feature','outcome','diet')),
 label TEXT NOT NULL, namespace TEXT NOT NULL, identifier TEXT NOT NULL,
 organism TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
 UNIQUE(namespace,identifier,kind)
);
CREATE TABLE ng_mapping (
 id TEXT PRIMARY KEY, from_id TEXT NOT NULL REFERENCES ng_entity(id),
 to_id TEXT NOT NULL REFERENCES ng_entity(id),
 relation TEXT NOT NULL CHECK(relation IN ('exact','narrower','broader','related')),
 release_id TEXT NOT NULL REFERENCES ng_release(id), source_ref TEXT NOT NULL,
 method TEXT NOT NULL, review_status TEXT NOT NULL
 CHECK(review_status IN ('verified','candidate','rejected')),
 UNIQUE(from_id,to_id,relation,release_id)
);
CREATE TABLE ng_study (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, population TEXT NOT NULL,
 design TEXT NOT NULL CHECK(design IN
 ('randomized','nonrandomized_intervention','observational','in_vitro','animal','unknown')),
 participant_group TEXT NOT NULL, independence_status TEXT NOT NULL
 CHECK(independence_status IN ('verified','candidate','overlap','excluded')),
 independence_basis TEXT NOT NULL, n_people INTEGER CHECK(n_people > 0),
 pubmed_id TEXT, registration TEXT, metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE ng_dataset (
 id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES ng_study(id),
 accession TEXT NOT NULL UNIQUE, release_id TEXT NOT NULL REFERENCES ng_release(id),
 modality TEXT NOT NULL, access_status TEXT NOT NULL
 CHECK(access_status IN ('public','metadata_only','controlled','agreement_required')),
 ingest_status TEXT NOT NULL CHECK(ingest_status IN
 ('metadata','measurements','pending','blocked','excluded')),
 n_assays INTEGER CHECK(n_assays >= 0), metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE ng_evidence (
 id TEXT PRIMARY KEY, subject_id TEXT NOT NULL REFERENCES ng_entity(id),
 object_id TEXT NOT NULL REFERENCES ng_entity(id), relation TEXT NOT NULL,
 layer TEXT NOT NULL CHECK(layer IN
 ('composition','molecular_identity','molecular_target','pathway','molecular_measurement','health_outcome')),
 study_id TEXT REFERENCES ng_study(id), release_id TEXT NOT NULL REFERENCES ng_release(id),
 source_ref TEXT NOT NULL, tier TEXT NOT NULL CHECK(tier IN
 ('composition','identity','curated_mechanism','in_vitro','animal','human_observational','human_nonrandomized','human_randomized')),
 inference TEXT NOT NULL CHECK(inference IN ('descriptive','association','intervention','causal')),
 identification_strategy TEXT, organism TEXT, tissue TEXT, dose REAL CHECK(dose >= 0),
 dose_unit TEXT, dose_basis TEXT, duration TEXT, comparison TEXT,
 effect REAL, effect_unit TEXT, standard_error REAL CHECK(standard_error >= 0),
 ci_low REAL, ci_high REAL, ci_level REAL CHECK(ci_level > 0 AND ci_level < 1),
 uncertainty_type TEXT, p_value REAL CHECK(p_value >= 0 AND p_value <= 1),
 q_value REAL CHECK(q_value >= 0 AND q_value <= 1), hypothesis_family TEXT,
 value_status TEXT NOT NULL CHECK(value_status IN
 ('measured','measured_zero','missing','imputed','below_detection','not_reported','qualitative')),
 n_people INTEGER CHECK(n_people > 0), n_samples INTEGER CHECK(n_samples >= 0),
 covariates_json TEXT NOT NULL DEFAULT '[]', context_json TEXT NOT NULL DEFAULT '{}',
 CHECK((ci_low IS NULL AND ci_high IS NULL) OR
       (ci_low IS NOT NULL AND ci_high IS NOT NULL AND ci_low <= ci_high)),
 CHECK(inference != 'causal' OR identification_strategy IS NOT NULL),
 CHECK(value_status NOT IN ('measured','measured_zero','imputed') OR
       (effect IS NOT NULL AND effect_unit IS NOT NULL)),
 CHECK(value_status != 'measured_zero' OR effect = 0),
 CHECK(layer != 'health_outcome' OR (study_id IS NOT NULL AND tier IN
       ('human_observational','human_nonrandomized','human_randomized'))),
 CHECK((dose IS NULL AND dose_unit IS NULL) OR (dose IS NOT NULL AND dose_unit IS NOT NULL)),
 CHECK(uncertainty_type != 'confidence_interval' OR
       (ci_low IS NOT NULL AND ci_high IS NOT NULL AND ci_level IS NOT NULL))
);
CREATE INDEX ng_evidence_subject ON ng_evidence(subject_id,layer);
CREATE INDEX ng_dataset_study ON ng_dataset(study_id);
CREATE TABLE ng_migration (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
"""
TABLES = (
    "ng_evidence",
    "ng_mapping",
    "ng_dataset",
    "ng_study",
    "ng_entity",
    "ng_release",
    "ng_migration",
)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys=ON")
    db.row_factory = sqlite3.Row
    return db


def migrate(db: sqlite3.Connection) -> None:
    present = db.execute("SELECT name FROM sqlite_master WHERE name='ng_migration'").fetchone()
    if present:
        if not db.execute("SELECT 1 FROM ng_migration WHERE version=?", (VERSION,)).fetchone():
            raise ValueError("unknown extension migration state")
        return
    # executescript otherwise commits prior work: migration owns its transaction.
    db.executescript(
        "BEGIN IMMEDIATE;\n"
        + SCHEMA
        + "\nINSERT INTO ng_migration VALUES ('"
        + VERSION
        + "', datetime('now'));\nCOMMIT;"
    )


def rollback(db: sqlite3.Connection) -> None:
    """Explicit opt-in rollback of extension only. Back up populated DB first."""
    db.executescript(
        "BEGIN IMMEDIATE;\n"
        + "DROP VIEW IF EXISTS ng_food_urine_feature_path;\nDROP TABLE IF EXISTS ng_molecular_measurement;\nDROP TABLE IF EXISTS ng_molecular_sample;\n"
        + "DROP TABLE IF EXISTS ng_feature_gene;\nDROP TABLE IF EXISTS ng_arm_component;\nDROP TABLE IF EXISTS ng_assay_context;\nDROP TABLE IF EXISTS ng_arm;\n"
        + "DROP TABLE IF EXISTS ng_study_link;\nDROP TABLE IF EXISTS ng_nutrient_definition;\n"
        + "DROP TABLE IF EXISTS ng_rights;\n"
        + "\n".join(f"DROP TABLE IF EXISTS {name};" for name in TABLES)
        + "\nCOMMIT;"
    )


def backup(source: Path, destination: Path) -> None:
    if source.resolve() == destination.resolve() or destination.exists():
        raise ValueError("backup destination must be new and different")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src,
        closing(sqlite3.connect(destination)) as dst,
    ):
        src.backup(dst)


ALLOWED_HOSTS = {
    "www.ncbi.nlm.nih.gov",
    "eutils.ncbi.nlm.nih.gov",
    "pmc.ncbi.nlm.nih.gov",
    "pubchem.ncbi.nlm.nih.gov",
    "wwwn.cdc.gov",
    "www.cdc.gov",
    "www.ebi.ac.uk",
    "ftp.ncbi.nlm.nih.gov",
    "api.nal.usda.gov",
    "fdc.nal.usda.gov",
    "api.github.com",
    "raw.githubusercontent.com",
}


def fetch(url: str, destination: Path, max_bytes: int = 5_000_000) -> dict:
    """Fail closed: HTTPS allowlist, redirect check, disk reserve and byte cap."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError("unapproved public source URL")
    if not 0 < max_bytes <= 10_000_000:
        raise ValueError("per-file cap must be <=10 MB")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(destination.parent).free < max_bytes + 1_000_000_000:
        raise ValueError("less than 1 GB disk reserve")

    class SafeRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            target = urllib.parse.urlsplit(newurl)
            if target.scheme != "https" or target.hostname not in ALLOWED_HOSTS:
                raise ValueError("unapproved redirect")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(SafeRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "NutriGenomicsResearch/0.2"})
    with opener.open(request, timeout=30) as response:
        if int(response.headers.get("Content-Length", 0)) > max_bytes:
            raise ValueError("source exceeds byte cap")
        raw = response.read(max_bytes + 1)
        content_type = response.headers.get("Content-Type", "")
        final_url = response.url
    if len(raw) > max_bytes:
        raise ValueError("source exceeds byte cap")
    if any(
        token in raw[:20000].lower()
        for token in (
            b"checking your browser",
            b"recaptcha",
            b"challengepage",
            b"captcha challenge",
        )
    ):
        raise ValueError("source returned browser challenge")
    if destination.suffix.lower() == ".xpt" and not raw.startswith(b"HEADER RECORD"):
        raise ValueError("expected SAS transport data, received another format")
    if destination.suffix.lower() == ".json":
        json.loads(raw)
    if destination.suffix.lower() == ".soft" and not raw.startswith(b"^SERIES"):
        raise ValueError("expected GEO Series SOFT")
    # Only complete validated payloads replace a snapshot.
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.write_bytes(raw)
    temporary.replace(destination)
    return {
        "url": url,
        "final_url": final_url,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "content_type": content_type,
        "local_path": str(destination),
    }


def insert(db: sqlite3.Connection, table: str, row: dict) -> None:
    """Idempotent insert, rejects changed records rather than silently overwriting."""
    if table not in TABLES:
        raise ValueError("unknown extension table")
    allowed = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
    if not row or not set(row) <= allowed:
        raise ValueError("unknown columns")
    for value in row.values():
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite number")
    existing = db.execute(f"SELECT * FROM {table} WHERE id=?", (row["id"],)).fetchone()
    if existing:
        if any(existing[key] != value for key, value in row.items()):
            raise ValueError(f"conflicting record {row['id']}; use a new version")
        return
    keys = list(row)
    db.execute(
        f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
        [row[key] for key in keys],
    )


def validate(db: sqlite3.Connection) -> list[str]:
    errors = [f"foreign_key: {tuple(r)}" for r in db.execute("PRAGMA foreign_key_check")]
    for r in db.execute("""SELECT e.id,e.layer,a.kind,b.kind,e.tier,s.design,e.inference,
                            r.access_status,e.q_value,e.hypothesis_family
                            FROM ng_evidence e JOIN ng_entity a ON a.id=e.subject_id
                            JOIN ng_entity b ON b.id=e.object_id
                            JOIN ng_release r ON r.id=e.release_id
                            LEFT JOIN ng_study s ON s.id=e.study_id"""):
        identifier, layer, subject, target, tier, design, inference, access, q, family = r
        if layer == "composition" and (subject != "food" or target not in {"nutrient", "compound"}):
            errors.append(f"{identifier}: invalid composition endpoints")
        if layer == "health_outcome" and target != "outcome":
            errors.append(f"{identifier}: molecular endpoint cannot be health outcome")
        if tier == "human_randomized" and design != "randomized":
            errors.append(f"{identifier}: randomized tier lacks randomized design")
        if tier == "human_observational" and inference in {"causal", "intervention"}:
            errors.append(f"{identifier}: observational association promoted")
        if access != "public":
            errors.append(f"{identifier}: evidence ingested from restricted source")
        if q is not None and not family:
            errors.append(f"{identifier}: adjusted p-value lacks hypothesis family")
    return errors


def coverage(db: sqlite3.Connection) -> dict:
    return {
        "migration": VERSION,
        "tables": {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES},
        "evidence_layers": dict(
            db.execute("SELECT layer,count(*) FROM ng_evidence GROUP BY layer")
        ),
        "independence": dict(
            db.execute(
                "SELECT independence_status,count(*) FROM ng_study GROUP BY independence_status"
            )
        ),
        "verified_participant_groups": db.execute(
            "SELECT count(DISTINCT participant_group) FROM ng_study WHERE independence_status='verified'"
        ).fetchone()[0],
        "dataset_status": dict(
            db.execute("SELECT ingest_status,count(*) FROM ng_dataset GROUP BY ingest_status")
        ),
        "value_status": dict(
            db.execute("SELECT value_status,count(*) FROM ng_evidence GROUP BY value_status")
        ),
        "validation_errors": validate(db),
        "clinical_recommendations": False,
        "transitive_health_inference": False,
    }
