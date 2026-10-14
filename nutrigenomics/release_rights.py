"""Explicit redistribution gate independent of public access; offline export only."""

import json
import sqlite3
from pathlib import Path

from nutrigenomics.expansion import TABLES, backup, connect, validate

SCHEMA = """
CREATE TABLE IF NOT EXISTS ng_rights (
 release_id TEXT PRIMARY KEY REFERENCES ng_release(id),
 status TEXT NOT NULL CHECK(status IN ('verified','quarantined','agreement_required')),
 license_identifier TEXT, policy_url TEXT, policy_sha256 TEXT,
 attribution TEXT, basis TEXT NOT NULL, reviewed_at TEXT NOT NULL
);
"""


def migrate_rights(db):
    db.executescript(SCHEMA)
    with db:
        db.execute(
            "INSERT OR IGNORE INTO ng_migration VALUES ('nutrition-rights-v1',datetime('now'))"
        )


def export_verified(source: Path, destination: Path):
    """New local artifact with verified dependency closure; never publish."""
    if destination.exists():
        raise ValueError("Export destination must be new")
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as original:
        tables = {
            row[0]
            for row in original.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if (
            tables
            - set(TABLES)
            - {
                "ng_rights",
                "ng_nutrient_definition",
                "ng_study_link",
                "ng_arm",
                "ng_assay_context",
            "ng_arm_component",
            "ng_feature_gene",
            }
        ):
            raise ValueError("Unassessed tables cannot enter a shareable export")
    temporary = destination.with_suffix(".building")
    backup(source, temporary)
    db = connect(temporary)
    try:
        db.execute("PRAGMA secure_delete=ON")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='ng_rights'").fetchone():
            raise ValueError("Rights assessments required before export")
        allowed = {
            row[0] for row in db.execute("SELECT release_id FROM ng_rights WHERE status='verified'")
        }
        if not allowed:
            raise ValueError("No verified releases")
        slots = ",".join("?" for _ in allowed)
        with db:
            if 'ng_feature_gene' in tables:
                db.execute(f'DELETE FROM ng_feature_gene WHERE release_id NOT IN ({slots}) OR dataset_id NOT IN (SELECT id FROM ng_dataset WHERE release_id IN ({slots}))', tuple(allowed) + tuple(allowed))
            if "ng_arm_component" in tables:
                db.execute(
                    f"DELETE FROM ng_arm_component WHERE release_id NOT IN ({slots}) OR structure_release_id NOT IN ({slots})",
                    tuple(allowed) + tuple(allowed),
                )
                db.execute(
                    f"DELETE FROM ng_arm_component WHERE arm_id NOT IN (SELECT id FROM ng_arm WHERE release_id IN ({slots}))",
                    tuple(allowed),
                )
            if "ng_assay_context" in tables:
                db.execute(
                    f"DELETE FROM ng_assay_context WHERE dataset_id NOT IN (SELECT id FROM ng_dataset WHERE release_id IN ({slots})) OR arm_id NOT IN (SELECT id FROM ng_arm WHERE release_id IN ({slots}))",
                    tuple(allowed) + tuple(allowed),
                )
            for table in ("ng_assay_context", "ng_arm", "ng_study_link", "ng_nutrient_definition"):
                if table in tables:
                    db.execute(
                        f"DELETE FROM {table} WHERE release_id NOT IN ({slots})", tuple(allowed)
                    )
            for table in ("ng_evidence", "ng_mapping", "ng_dataset", "ng_rights"):
                db.execute(f"DELETE FROM {table} WHERE release_id NOT IN ({slots})", tuple(allowed))
            # A study/context must not borrow a quarantined dataset into the release.
            study_refs = "SELECT study_id FROM ng_dataset UNION SELECT study_id FROM ng_evidence WHERE study_id IS NOT NULL"
            if "ng_arm" in tables:
                study_refs += " UNION SELECT study_id FROM ng_arm"
            if "ng_study_link" in tables:
                db.execute(
                    f"DELETE FROM ng_study_link WHERE from_study NOT IN ({study_refs}) OR to_study NOT IN ({study_refs})"
                )
            db.execute(f"DELETE FROM ng_study WHERE id NOT IN ({study_refs})")
            entity_refs = "SELECT subject_id FROM ng_evidence UNION SELECT object_id FROM ng_evidence UNION SELECT from_id FROM ng_mapping UNION SELECT to_id FROM ng_mapping"
            if "ng_nutrient_definition" in tables:
                entity_refs += " UNION SELECT entity_id FROM ng_nutrient_definition"
            if "ng_arm" in tables:
                entity_refs += " UNION SELECT exposure_id FROM ng_arm"
            if "ng_arm_component" in tables:
                entity_refs += " UNION SELECT compound_id FROM ng_arm_component"
            if 'ng_feature_gene' in tables:
                entity_refs += ' UNION SELECT gene_id FROM ng_feature_gene'
            db.execute(f"DELETE FROM ng_entity WHERE id NOT IN ({entity_refs})")
            db.execute(f"DELETE FROM ng_release WHERE id NOT IN ({slots})", tuple(allowed))
            # Local paths and embedded operational metadata are not redistribution artifacts.
            db.execute("UPDATE ng_release SET local_path='' ")
        # Remove excluded payloads from freelist pages in the distributable file.
        db.execute("VACUUM")
        errors = validate(db)
        if errors:
            raise ValueError(errors)
        report = {
            "artifact": str(destination),
            "published": False,
            "verified_releases": sorted(allowed),
            "counts": {
                table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (
                    "ng_release",
                    "ng_entity",
                    "ng_evidence",
                    "ng_mapping",
                    "ng_dataset",
                    "ng_study",
                )
            },
            "foreign_key_errors": list(db.execute("PRAGMA foreign_key_check")),
        }
        db.close()
        temporary.replace(destination)
        destination.with_suffix(".rights.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        return report
    except Exception:
        db.close()
        temporary.unlink(missing_ok=True)
        raise
    finally:
        db.close()
