from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3

import pytest

from nutrigenomics.expansion import connect, coverage, fetch, insert, migrate, rollback, validate
from scripts.import_public_expansion import import_fdc, import_geo, import_pubchem, replay
from scripts.import_public_outcomes import extract
from scripts.ingest_public_matrix import parse_matrix


@pytest.fixture
def db(tmp_path):
    connection = connect(tmp_path / "test.sqlite3")
    migrate(connection)
    insert(
        connection,
        "ng_release",
        {
            "id": "r",
            "source": "test",
            "version": "1",
            "url": "https://example.test",
            "retrieved_at": "2026-10-01T00:00:00Z",
            "sha256": "a" * 64,
            "local_path": "fixture",
            "license": "test",
            "access_status": "public",
        },
    )
    for identifier, kind in [
        ("food", "food"),
        ("nutrient", "nutrient"),
        ("molecule", "compound"),
        ("outcome", "outcome"),
    ]:
        insert(
            connection,
            "ng_entity",
            {
                "id": identifier,
                "kind": kind,
                "label": identifier,
                "namespace": "fixture",
                "identifier": identifier,
            },
        )
    connection.commit()
    yield connection
    connection.close()


def evidence(**changes):
    row = {
        "id": "e",
        "subject_id": "food",
        "object_id": "nutrient",
        "relation": "contains",
        "layer": "composition",
        "release_id": "r",
        "source_ref": "fixture",
        "tier": "composition",
        "inference": "descriptive",
        "effect": 0,
        "effect_unit": "mg/100 g",
        "value_status": "measured_zero",
    }
    row.update(changes)
    return row


def test_migration_idempotent_and_rollback_preserves_legacy(db):
    db.execute("CREATE TABLE legacy(value TEXT)")
    db.execute("INSERT INTO legacy VALUES ('preserve negative result')")
    db.commit()
    migrate(db)
    assert db.execute("SELECT count(*) FROM ng_migration").fetchone()[0] == 1
    rollback(db)
    assert db.execute("SELECT value FROM legacy").fetchone()[0] == "preserve negative result"
    migrate(db)
    assert not validate(db)


@pytest.mark.parametrize(
    "changes",
    [
        {"effect": 1},
        {"effect": None},
        {"effect_unit": None},
        {"ci_low": 2, "ci_high": 1},
        {"ci_low": 1},
        {"p_value": 1.1},
        {"q_value": -0.1},
        {"dose": 1, "dose_unit": None},
        {"inference": "causal"},
        {"uncertainty_type": "confidence_interval"},
        {"object_id": "nonexistent"},
    ],
)
def test_semantically_invalid_measurements_rejected(db, changes):
    with pytest.raises(sqlite3.IntegrityError):
        insert(db, "ng_evidence", evidence(**changes))


def test_missing_zero_and_imputed_zero_are_distinct(db):
    insert(db, "ng_evidence", evidence())
    insert(
        db,
        "ng_evidence",
        evidence(id="missing", effect=None, effect_unit=None, value_status="missing"),
    )
    insert(db, "ng_evidence", evidence(id="imputed", value_status="imputed"))
    assert coverage(db)["value_status"] == {"imputed": 1, "measured_zero": 1, "missing": 1}


def test_nan_not_silently_coerced_to_null(db):
    with pytest.raises(ValueError, match="non-finite"):
        insert(db, "ng_evidence", evidence(effect=float("nan")))


def test_conflicting_replay_not_silent_overwrite(db):
    insert(db, "ng_evidence", evidence())
    insert(db, "ng_evidence", evidence())
    with pytest.raises(ValueError, match="conflicting"):
        insert(db, "ng_evidence", evidence(source_ref="different provenance"))


def test_molecular_endpoint_cannot_be_health_outcome(db):
    insert(
        db,
        "ng_study",
        {
            "id": "s",
            "title": "trial",
            "population": "humans",
            "design": "randomized",
            "participant_group": "group",
            "independence_status": "verified",
            "independence_basis": "fixture",
        },
    )
    insert(
        db,
        "ng_evidence",
        evidence(
            object_id="molecule",
            layer="health_outcome",
            study_id="s",
            tier="human_randomized",
            inference="intervention",
        ),
    )
    assert any("molecular endpoint" in error for error in validate(db))


def test_observational_association_not_promoted(db):
    insert(
        db,
        "ng_study",
        {
            "id": "s",
            "title": "cohort",
            "population": "humans",
            "design": "observational",
            "participant_group": "group",
            "independence_status": "verified",
            "independence_basis": "fixture",
        },
    )
    insert(
        db,
        "ng_evidence",
        evidence(
            object_id="outcome",
            layer="health_outcome",
            study_id="s",
            tier="human_observational",
            inference="intervention",
        ),
    )
    assert any("observational association" in error for error in validate(db))


def test_restricted_source_and_missing_hypothesis_family_rejected(db):
    db.execute("UPDATE ng_release SET access_status='controlled'")
    insert(db, "ng_evidence", evidence(q_value=0.9))
    errors = validate(db)
    assert any("restricted" in error for error in errors)
    assert any("hypothesis family" in error for error in errors)


def test_fdc_assumed_zero_not_promoted_to_measured(db):
    food = {
        "fdcId": 123,
        "dataType": "SR Legacy",
        "description": "fixture",
        "foodNutrients": [
            {
                "id": 1,
                "nutrient": {"id": 999, "name": "test", "unitName": "MG"},
                "amount": 0,
                "dataPoints": 20,
                "foodNutrientDerivation": {"code": "Z", "description": "Assumed zero"},
            }
        ],
    }
    import_fdc(db, food, "r")
    assert db.execute("SELECT value_status FROM ng_evidence").fetchone()[0] == "imputed"


def test_fdc_unknown_basis_not_accepted(db):
    with pytest.raises(ValueError, match="per-100g"):
        import_fdc(db, {"fdcId": 1, "dataType": "Branded"}, "r")


def test_three_tissues_count_one_participant_group(db):
    for accession in ["GSE27384", "GSE27385", "GSE27386"]:
        text = f"^SERIES = {accession}\n!Series_title = trial\n!Series_summary = Eleven people same crossover\n!Series_sample_id = GSM1"
        import_geo(db, accession, text, "r")
    report = coverage(db)
    assert report["verified_participant_groups"] == 1
    assert report["tables"]["ng_dataset"] == 3
    assert db.execute("SELECT n_people FROM ng_study").fetchone()[0] == 11


def test_no_pubchem_name_only_identity(db):
    with pytest.raises(ValueError, match="identity missing"):
        import_pubchem(db, {"PropertyTable": {"Properties": [{"CID": 1, "Title": "guess"}]}}, "r")


@pytest.mark.parametrize(
    "url", ["http://www.ncbi.nlm.nih.gov/x", "https://unreviewed.test/data", "file:///secret"]
)
def test_fetch_disallows_unreviewed_source_before_network(tmp_path, url):
    with pytest.raises(ValueError, match="unapproved"):
        fetch(url, tmp_path / "data")


def test_snapshot_tamper_rejected_transactionally(tmp_path):
    snapshot = tmp_path / "snapshots"
    snapshot.mkdir()
    (snapshot / "data.json").write_text("{}")
    (snapshot / "manifest.json").write_text(
        json.dumps(
            {
                "files": [{"file": "data.json", "sha256": "0" * 64, "bytes": 2}],
                "failures": [],
                "blocked_sources": [],
            }
        )
    )
    with pytest.raises(ValueError, match="checksum"):
        replay(snapshot, tmp_path / "db.sqlite3", tmp_path / "report")
    with connect(tmp_path / "db.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM ng_release").fetchone()[0] == 0


def matrix(columns="GSM1", feature="probe1\t0"):
    return gzip.compress(
        f'!Series_geo_accession\t"GSE1"\n!series_matrix_table_begin\nID_REF\t{columns}\n{feature}\n!series_matrix_table_end\n'.encode()
    )


def test_processed_values_zero_retained_no_imputation():
    frame, metadata = parse_matrix(matrix(), "GSE1", {"GSM1"})
    assert frame.iloc[0, 1] == 0
    assert metadata["reported_zero_values"] == 1
    assert not metadata["effect_analysis_performed"]
    assert not metadata["imputation_performed"]


def test_matrix_sample_mismatch_rejected():
    with pytest.raises(ValueError, match="samples"):
        parse_matrix(matrix(), "GSE1", {"GSM2"})


def test_matrix_duplicate_features_rejected():
    with pytest.raises(ValueError, match="feature"):
        parse_matrix(matrix(feature="probe1\t0\nprobe1\t1"), "GSE1", {"GSM1"})


def test_diet_outcomes_stop_on_linked_erratum():
    raw = b'<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>9099655</PMID><Article><Abstract><AbstractText>459 randomized eight weeks 5.5 3.0 2.8 1.1 0.07</AbstractText></Abstract></Article><CommentsCorrectionsList><CommentsCorrections RefType="ErratumIn"/></CommentsCorrectionsList></MedlineCitation></PubmedArticle></PubmedArticleSet>'
    with pytest.raises(ValueError, match="erratum"):
        extract(raw)


def test_actual_snapshots_have_verified_hashes():
    from pathlib import Path

    snapshot = Path("research/2026-09-30/expansion/snapshots")
    if not (snapshot / "manifest.json").exists():
        pytest.skip("downloaded artifact not present")
    manifest = json.loads((snapshot / "manifest.json").read_text())
    for row in manifest["files"]:
        assert hashlib.sha256((snapshot / row["file"]).read_bytes()).hexdigest() == row["sha256"]


def test_readonly_queries_preserve_units_and_zero(tmp_path, db):
    from nutrigenomics.evidence_query import query

    insert(db, "ng_evidence", evidence())
    db.commit()
    result = query(tmp_path / "test.sqlite3", subject_id="food", layer="composition")
    assert result["total"] == 1
    assert result["records"][0]["effect"] == 0
    assert result["records"][0]["effect_unit"] == "mg/100 g"
    assert not result["transitive_health_inference"]


def test_readonly_query_requires_existing_database(tmp_path):
    from nutrigenomics.evidence_query import query

    with pytest.raises(FileNotFoundError):
        query(tmp_path / "absent.sqlite3")
    assert not (tmp_path / "absent.sqlite3").exists()


def test_queries_do_not_accept_sql_as_layer(tmp_path, db):
    from nutrigenomics.evidence_query import query

    with pytest.raises(ValueError, match="layer"):
        query(tmp_path / "test.sqlite3", layer="composition' OR 1=1")
