import pytest

from nutrigenomics.context_joins import migrate_context, put_context
from nutrigenomics.expansion import connect, insert, migrate, rollback
from nutrigenomics.release_rights import export_verified, migrate_rights


def fixture_db(tmp_path):
    path = tmp_path / "source.sqlite3"
    db = connect(path)
    migrate(db)
    migrate_rights(db)
    for identifier in ["safe", "quarantine"]:
        insert(
            db,
            "ng_release",
            {
                "id": identifier,
                "source": identifier,
                "version": "1",
                "url": "https://example.test",
                "retrieved_at": "2026-10-01",
                "sha256": "a" * 64,
                "local_path": "private/local/path",
                "license": "fixture",
                "access_status": "public",
            },
        )
        for kind in ["food", "nutrient"]:
            insert(
                db,
                "ng_entity",
                {
                    "id": identifier + kind,
                    "kind": kind,
                    "label": identifier + kind,
                    "namespace": "fixture",
                    "identifier": identifier + kind,
                    "metadata_json": '{"sentinel":"EXCLUDED_PAYLOAD_SENTINEL"}'
                    if identifier == "quarantine"
                    else "{}",
                },
            )
        insert(
            db,
            "ng_evidence",
            {
                "id": identifier,
                "subject_id": identifier + "food",
                "object_id": identifier + "nutrient",
                "relation": "contains",
                "layer": "composition",
                "release_id": identifier,
                "source_ref": "fixture",
                "tier": "composition",
                "inference": "descriptive",
                "effect": 0,
                "effect_unit": "mg/100g",
                "value_status": "measured_zero",
            },
        )
        db.execute(
            "INSERT INTO ng_rights VALUES (?,?,?,?,?,?,?,?)",
            (
                identifier,
                "verified" if identifier == "safe" else "quarantined",
                "fixture",
                None,
                None,
                None,
                "explicit assessment",
                "2026-10-01",
            ),
        )
    db.commit()
    db.close()
    return path


def test_verified_component_cannot_borrow_quarantined_arm(tmp_path):
    source = fixture_db(tmp_path)
    db = connect(source)
    migrate_context(db)
    with db:
        insert(
            db,
            "ng_study",
            {
                "id": "trial",
                "title": "trial",
                "population": "test",
                "design": "randomized",
                "participant_group": "trial",
                "independence_status": "verified",
                "independence_basis": "test",
            },
        )
        insert(
            db,
            "ng_entity",
            {
                "id": "compound",
                "kind": "compound",
                "label": "compound",
                "namespace": "test",
                "identifier": "compound",
            },
        )
        put_context(
            db,
            "ng_arm",
            {
                "id": "arm",
                "study_id": "trial",
                "exposure_id": "safefood",
                "source_label": "source arm",
                "release_id": "quarantine",
                "context_json": "{}",
            },
        )
        put_context(
            db,
            "ng_arm_component",
            {
                "id": "component",
                "arm_id": "arm",
                "compound_id": "compound",
                "source_label": "component",
                "nominal_dose": 1,
                "dose_unit": "g/day nominal",
                "release_id": "safe",
                "structure_release_id": "safe",
                "chemical_form_measured": 0,
                "molecule_effect_attribution": 0,
                "context_json": '{"sentinel":"ARM_COMPONENT_EXCLUDED"}',
            },
        )
    db.close()
    destination = tmp_path / "component-release.sqlite3"
    report = export_verified(source, destination)
    assert not report["foreign_key_errors"]
    release = connect(destination)
    assert release.execute("SELECT count(*) FROM ng_arm_component").fetchone()[0] == 0
    assert release.execute("SELECT count(*) FROM ng_arm").fetchone()[0] == 0
    assert b"ARM_COMPONENT_EXCLUDED" not in destination.read_bytes()


def test_export_dependency_closure_and_no_deleted_payload(tmp_path):
    source = fixture_db(tmp_path)
    destination = tmp_path / "release.sqlite3"
    report = export_verified(source, destination)
    assert report["counts"]["ng_evidence"] == 1 and report["counts"]["ng_entity"] == 2
    assert not report["foreign_key_errors"]
    assert b"EXCLUDED_PAYLOAD_SENTINEL" not in destination.read_bytes()
    db = connect(destination)
    assert db.execute("SELECT local_path FROM ng_release").fetchone()[0] == ""
    db.close()
    original = connect(source)
    assert original.execute("SELECT count(*) FROM ng_evidence").fetchone()[0] == 2
    original.close()


def test_export_requires_explicit_verified_rights(tmp_path):
    source = fixture_db(tmp_path)
    db = connect(source)
    db.execute("UPDATE ng_rights SET status='quarantined'")
    db.commit()
    db.close()
    with pytest.raises(ValueError, match="No verified"):
        export_verified(source, tmp_path / "blocked.sqlite3")
    assert not (tmp_path / "blocked.sqlite3").exists()


def test_unassessed_legacy_table_blocks_export(tmp_path):
    source = fixture_db(tmp_path)
    db = connect(source)
    db.execute("CREATE TABLE legacy(secret TEXT)")
    db.commit()
    db.close()
    with pytest.raises(ValueError, match="Unassessed tables"):
        export_verified(source, tmp_path / "legacy-export.sqlite3")


def test_rollback_rights_preserves_legacy(tmp_path):
    source = fixture_db(tmp_path)
    db = connect(source)
    db.execute("CREATE TABLE legacy(value TEXT)")
    db.execute("INSERT INTO legacy VALUES ('negative result')")
    db.commit()
    rollback(db)
    assert db.execute("SELECT value FROM legacy").fetchone()[0] == "negative result"
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='ng_rights'").fetchone()
    db.close()
