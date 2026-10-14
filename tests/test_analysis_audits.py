"""External audit attachments preserve signed sources and exact analysis scope."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from nutrigenomics.bridge_models import StudyContrast
from nutrigenomics.db import create_schema, get_session_factory
from nutrigenomics.models import AnalysisRun, GeoSeries, ResearchStudy
from scripts import attach_analysis_audits as attachment

ACCESSION = "GSE56960"
VERSION = "gse56960-rma-limma-dose-change-v3"
CONTRAST = "dose_NormalWeight_1000_vs500_6h_change"


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _source(path: Path, scope: str) -> dict:
    return {
        "source_path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_scope": scope,
    }


def _db_snapshot(database_url: str) -> str:
    with get_session_factory(database_url)() as session:
        state = {}
        for model in (AnalysisRun, StudyContrast):
            state[model.__tablename__] = [
                {column.name: getattr(row, column.name) for column in model.__table__.columns}
                for row in session.scalars(
                    select(model).order_by(*model.__table__.primary_key.columns)
                )
            ]
    return json.dumps(state, sort_keys=True, default=str)


@pytest.fixture
def audit_bundle(tmp_path: Path) -> dict:
    workspace = tmp_path / "approved-project"
    workspace.mkdir()
    sample_path = workspace / "sample_scope_audit.json"
    reactome_path = workspace / "reactome_release_audit.json"
    model_scope = {"independent_people": 14, "fitted_model_assays": 168}
    contrast_scope = {
        "independent_people": 7,
        "direct_contrast_assays": 28,
        "weight_group_assays": 84,
        "fitted_model_assays": 168,
        "fitted_model_people": 14,
    }
    _write_json(
        sample_path,
        {
            "scope": "S02 sample mapping",
            "model": model_scope,
            "contrast": contrast_scope,
            "manual_expert_review_status": "pending",
        },
    )
    _write_json(
        reactome_path,
        {
            "release": "95",
            "matched_exactly": True,
            "official_file_sha256": "a" * 64,
            "frozen_file_sha256": "b" * 64,
            "source_url": "https://download.reactome.org/95/NCBI2Reactome_All_Levels.txt",
        },
    )
    signed = workspace / "original-analysis"
    signed.mkdir()
    for name, data in {
        "analysis_metadata.json": {
            "accession": ACCESSION,
            "analysis_version": VERSION,
            "sample_count": 168,
        },
        "DONE.json": {"status": "completed", "source_sha256": "c" * 64},
        "RELEASE_APPROVAL.json": {"scientific_qc_status": "passed_with_declared_limitations"},
    }.items():
        _write_json(signed / name, data)
    manifest_path = workspace / "attachment_manifest.json"
    contrast_id = f"{ACCESSION}:{VERSION}:{CONTRAST}"
    manifest = {
        "version": "external-audits-v1",
        "analyses": [
            {
                "accession": ACCESSION,
                "analysis_version": VERSION,
                "audits": [
                    _source(sample_path, "sample_scope"),
                    _source(reactome_path, "reactome_release"),
                ],
                "sample_scope": model_scope,
                "contrast_overrides": {contrast_id: {"sample_scope": contrast_scope}},
            }
        ],
    }
    _write_json(manifest_path, manifest)
    database_url = f"sqlite:///{(workspace / 'audit.sqlite3').as_posix()}"
    create_schema(database_url)
    with get_session_factory(database_url)() as session:
        session.add(
            ResearchStudy(id="S02", title="Meal energy study", description="Synthetic test")
        )
        session.flush()
        for accession in (ACCESSION, "GSE98645"):
            session.add(GeoSeries(accession=accession, study_id="S02", title="Synthetic series"))
        session.flush()
        for accession, version in [
            (ACCESSION, VERSION),
            (ACCESSION, "old-v2"),
            ("GSE98645", VERSION),
            ("GSE98645", "acute-v2"),
            ("GSE98645", "chronic-v2"),
        ]:
            session.add(
                AnalysisRun(
                    series_accession=accession,
                    analysis_version=version,
                    status="completed",
                    metadata_json={
                        "original_signed_metadata": {"sample_count": 168},
                        "manual_expert_review_status": "pending",
                    },
                )
            )
        for version in (VERSION, "old-v2"):
            session.add(
                StudyContrast(
                    id=f"{ACCESSION}:{version}:{CONTRAST}",
                    study_id="S02",
                    series_accession=ACCESSION,
                    contrast=CONTRAST,
                    analysis_version=version,
                    comparison=CONTRAST,
                    design="randomized crossover",
                    tissue="whole blood",
                    exposure_ids=[],
                    arm_ids=[],
                    n_people=7,
                    n_samples=168,
                    analysis_status="reanalyzed",
                    metadata_json={
                        "n_samples_scope": "source reported fitted assays",
                        "existing": "preserve",
                    },
                )
            )
        session.commit()
    return {
        "workspace": workspace,
        "manifest_path": manifest_path,
        "manifest": manifest,
        "database_url": database_url,
        "sample_path": sample_path,
        "reactome_path": reactome_path,
        "signed_path": signed,
        "contrast_id": contrast_id,
        "model_scope": model_scope,
        "contrast_scope": contrast_scope,
    }


def _run(bundle: dict) -> dict:
    return attachment.run(bundle["manifest_path"], bundle["database_url"], [bundle["workspace"]])


def test_attachment_is_version_exact_additive_traceable_and_idempotent(audit_bundle) -> None:
    bundle = audit_bundle
    original_bytes = {path: path.read_bytes() for path in bundle["workspace"].rglob("*.json")}
    result = _run(bundle)
    assert result["analyses_updated"] == 1 and result["contrasts_updated"] == 1
    assert result["audits_attached"] == 4
    with get_session_factory(bundle["database_url"])() as session:
        run = session.scalar(
            select(AnalysisRun).where(
                AnalysisRun.series_accession == ACCESSION, AnalysisRun.analysis_version == VERSION
            )
        )
        contrast = session.get(StudyContrast, bundle["contrast_id"])
        assert run is not None and contrast is not None
        assert run.metadata_json["sample_scope"] == bundle["model_scope"]
        assert contrast.metadata_json["sample_scope"] == bundle["contrast_scope"]
        assert contrast.n_people == 7 and contrast.n_samples == 168
        assert contrast.metadata_json["existing"] == "preserve"
        assert run.metadata_json["original_signed_metadata"] == {"sample_count": 168}
        assert run.metadata_json["manual_expert_review_status"] == "pending"
        attached = list(run.metadata_json["audit_attachments"].values())
        reactome = next(row for row in attached if row["source_scope"] == "reactome_release")
        assert (
            reactome["source_sha256"]
            == hashlib.sha256(bundle["reactome_path"].read_bytes()).hexdigest()
        )
        assert reactome["content"]["release"] == "95"
        assert reactome["content"]["source_url"].startswith("https://download.reactome.org/95/")
        assert reactome["attachment_manifest"]["sha256"] == result["manifest_sha256"]
        other_runs = session.scalars(
            select(AnalysisRun).where(
                (AnalysisRun.series_accession != ACCESSION)
                | (AnalysisRun.analysis_version != VERSION)
            )
        )
        assert all("audit_attachments" not in row.metadata_json for row in other_runs)
        assert (
            "audit_attachments"
            not in session.get(StudyContrast, f"{ACCESSION}:old-v2:{CONTRAST}").metadata_json
        )
    before = _db_snapshot(bundle["database_url"])
    retry = _run(bundle)
    assert retry["analyses_updated"] == retry["contrasts_updated"] == retry["audits_attached"] == 0
    assert retry["unchanged_records"] == 2
    assert _db_snapshot(bundle["database_url"]) == before
    assert all(path.read_bytes() == blob for path, blob in original_bytes.items())


def test_shared_n15_baselines_and_clinical_people_have_distinct_scopes(audit_bundle) -> None:
    bundle = audit_bundle
    audit_path = bundle["workspace"] / "n15_scope.json"
    common = {
        "RNA_people": 7,
        "clinical_people": 13,
        "raw_RNA_assays": 77,
        "shared_product_baselines": 14,
        "unique_modeled_assays_across_acute_chronic": 70,
        "initial_baseline_assays_excluded": 7,
    }
    _write_json(audit_path, common)
    manifest = copy.deepcopy(bundle["manifest"])
    manifest["analyses"] = [
        {
            "accession": "GSE98645",
            "analysis_version": version,
            "audits": [_source(audit_path, "sample_scope")],
            "sample_scope": {**common, "fitted_model_assays": assays},
        }
        for version, assays in [("acute-v2", 56), ("chronic-v2", 28)]
    ]
    _write_json(bundle["manifest_path"], manifest)
    assert _run(bundle)["analyses_updated"] == 2
    with get_session_factory(bundle["database_url"])() as session:
        scopes = {
            row.analysis_version: row.metadata_json["sample_scope"]
            for row in session.scalars(
                select(AnalysisRun).where(
                    AnalysisRun.analysis_version.in_(["acute-v2", "chronic-v2"])
                )
            )
        }
    assert scopes["acute-v2"]["fitted_model_assays"] == 56
    assert scopes["chronic-v2"]["fitted_model_assays"] == 28
    assert all(
        scope["unique_modeled_assays_across_acute_chronic"] == 70 for scope in scopes.values()
    )
    assert all(
        scope["clinical_people"] == 13 and scope["RNA_people"] == 7 for scope in scopes.values()
    )


def test_source_hash_mismatch_rejects_without_altering_db(audit_bundle) -> None:
    bundle = audit_bundle
    before = _db_snapshot(bundle["database_url"])
    bundle["sample_path"].write_text('{"replaced": true}', encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        _run(bundle)
    assert _db_snapshot(bundle["database_url"]) == before


def test_unknown_exact_version_rolls_back_prior_analysis_attachments(audit_bundle) -> None:
    bundle = audit_bundle
    before = _db_snapshot(bundle["database_url"])
    manifest = copy.deepcopy(bundle["manifest"])
    invalid = copy.deepcopy(manifest["analyses"][0])
    invalid["analysis_version"] = "unimported-v99"
    invalid["contrast_overrides"] = {}
    manifest["analyses"].append(invalid)
    _write_json(bundle["manifest_path"], manifest)
    with pytest.raises(ValueError, match="exact accession/analysis_version"):
        _run(bundle)
    assert _db_snapshot(bundle["database_url"]) == before


def test_existing_audit_identity_cannot_have_its_contrast_scope_changed(audit_bundle) -> None:
    bundle = audit_bundle
    _run(bundle)
    before = _db_snapshot(bundle["database_url"])
    manifest = copy.deepcopy(bundle["manifest"])
    manifest["analyses"][0]["contrast_overrides"][bundle["contrast_id"]]["sample_scope"][
        "independent_people"
    ] = 8
    _write_json(bundle["manifest_path"], manifest)
    with pytest.raises(ValueError, match="Immutable audit identity"):
        _run(bundle)
    assert _db_snapshot(bundle["database_url"]) == before


def test_unknown_contrast_override_cannot_modify_db(audit_bundle) -> None:
    bundle = audit_bundle
    before = _db_snapshot(bundle["database_url"])
    manifest = copy.deepcopy(bundle["manifest"])
    manifest["analyses"][0]["contrast_overrides"] = {
        "unknown-contrast": {"sample_scope": {"n_people": 7}}
    }
    _write_json(bundle["manifest_path"], manifest)
    with pytest.raises(ValueError, match="Contrast override is unknown"):
        _run(bundle)
    assert _db_snapshot(bundle["database_url"]) == before


def test_external_path_is_rejected_even_when_expected_hash_is_valid(audit_bundle, tmp_path) -> None:
    bundle = audit_bundle
    before = _db_snapshot(bundle["database_url"])
    outside = tmp_path / "outside.json"
    _write_json(outside, {"looks": "valid"})
    manifest = copy.deepcopy(bundle["manifest"])
    manifest["analyses"][0]["audits"] = [_source(outside, "sample_scope")]
    _write_json(bundle["manifest_path"], manifest)
    with pytest.raises(ValueError, match="outside approved project roots"):
        _run(bundle)
    assert _db_snapshot(bundle["database_url"]) == before


@pytest.mark.parametrize("changed_file", ["sample_path", "manifest_path"])
def test_file_change_during_attachment_rolls_back_flushed_rows(
    audit_bundle, monkeypatch, changed_file
):
    bundle = audit_bundle
    before = _db_snapshot(bundle["database_url"])
    original = attachment._verify_unchanged

    def changed_before_final_check(files, roots):
        bundle[changed_file].write_text('{"changed_during_transaction": true}', encoding="utf-8")
        original(files, roots)

    monkeypatch.setattr(attachment, "_verify_unchanged", changed_before_final_check)
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        _run(bundle)
    assert _db_snapshot(bundle["database_url"]) == before
