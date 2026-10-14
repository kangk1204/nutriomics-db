"""Synthetic version isolation; no human review or scientific accuracy evidence."""

import csv
import io
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nutrigenomics import api
from nutrigenomics.bridge_models import BridgeSource, EvidenceAssertion, Exposure, StudyContrast
from nutrigenomics.db import create_schema, get_engine, get_session_factory
from nutrigenomics.models import AnalysisRun, GeoSeries, KnowledgeLink, ResearchStudy, StudyEffect
from sqlalchemy import select


@pytest.fixture
def workspace(tmp_path):
    url = f"sqlite:///{(tmp_path / 'synthetic-version-export.sqlite3').as_posix()}"
    create_schema(url)
    with get_session_factory(url)() as session:
        session.add(ResearchStudy(
            id="SYNTHETIC", title="Synthetic fixture, not a real study",
            description="Synthetic data only; no participant or scientific validation evidence",
        ))
        session.flush()
        session.add(GeoSeries(accession="GSEFIXTURE", study_id="SYNTHETIC", title="Synthetic"))
        session.add_all([
            BridgeSource(
                id="open-source", url="https://example.invalid/synthetic-source", version="fixture",
                sha256="a" * 64, source_location="Synthetic fixture only",
                license="Synthetic fixture, no real publication", export_allowed=True,
            ),
            BridgeSource(
                id="restricted-source", url="https://example.invalid/restricted", version="fixture",
                sha256="b" * 64, source_location="Restricted synthetic fixture only",
                license="No export, synthetic policy fixture", export_allowed=False,
            ),
        ])
        session.flush()
        session.add(Exposure(
            id="exposure", study_id="SYNTHETIC", label="NOS3 synthetic exposure", kind="nutrient",
            formulation="Synthetic test material", dose_basis="unknown", source_id="open-source",
        ))
        session.flush()
        for version in ("v1", "v2"):
            session.add(StudyContrast(
                id=f"contrast-{version}", study_id="SYNTHETIC", series_accession="GSEFIXTURE",
                contrast="post_minus_pre", analysis_version=version,
                comparison="Synthetic comparison", design="uncontrolled_before_after", tissue="PBMC",
                exposure_ids=["exposure"], analysis_status="reanalyzed",
            ))
            session.add(StudyEffect(
                series_accession="GSEFIXTURE", feature_type="gene", feature_id=f"NOS3-{version}",
                contrast="post_minus_pre", analysis_version=version, effect_estimate=-0.2,
                p_value=0.7, q_value=0.8,
                metadata_json={"fixture_only": True, "export_allowed": True},
            ))
            session.add(AnalysisRun(
                series_accession="GSEFIXTURE", analysis_version=version,
                metadata_json={"fixture_only": True, "qc_scope": "Synthetic only"},
                input_manifest_json=[{"path": "synthetic.tsv", "sha256": "c" * 64}],
                output_manifest_json=[{"path": "synthetic-effects.tsv", "sha256": "d" * 64}],
            ))
        session.flush()
        for version, source in (("v1", "open-source"), ("v2", "open-source"),
                                ("v2", "restricted-source")):
            session.add(EvidenceAssertion(
                id=f"assertion-{version}-{source}", study_id="SYNTHETIC", exposure_id="exposure",
                contrast_id=f"contrast-{version}", source_id=source, layer="molecular_measurement",
                assertion="NOS3 synthetic result", review_status="source_checked",
                organism="Homo sapiens", inference="observed",
                limitations=["Synthetic fixture only; no independent clinical validation"],
                metadata_json={"fixture_only": True},
            ))
        for ref, metadata in (("legacy-missing", {}), ("legacy-v1", {"analysis_version": "v1"}),
                              ("legacy-v2", {"analysis_version": "v2"})):
            session.add(KnowledgeLink(
                source="synthetic-legacy", source_ref=ref, subject_type="gene", subject_id="NOS3",
                relation="associated_with", target_type="pathway", target_id="synthetic-pathway",
                evidence_kind="untyped_legacy", metadata_json={"export_allowed": True, **metadata},
            ))
        session.commit()
        before = _stored_records(session)

        # Reuse the public handlers and dependency contract without production startup.
        application = FastAPI()
        application.get("/api/v1/evidence")(api.search_evidence)
        application.get("/api/v1/export/evidence")(api.export_evidence)
        application.dependency_overrides[api.get_session] = lambda: session
        with TestClient(application) as client:
            yield client
        assert _stored_records(session) == before
    get_engine(url).dispose()


def _stored_records(session):
    return {
        model.__tablename__: [
            {column.key: getattr(row, column.key) for column in model.__table__.columns}
            for row in session.scalars(select(model).order_by(model.id))
        ]
        for model in (StudyEffect, AnalysisRun, EvidenceAssertion, KnowledgeLink)
    }


def _request(client, surface, version=None):
    params = {"query": "NOS3"}
    if version is not None:
        params["analysis_version"] = version
    if surface == "search":
        response = client.get("/api/v1/evidence", params=params)
    else:
        params["format"] = surface
        response = client.get("/api/v1/export/evidence", params=params)
    assert response.status_code == 200
    if surface == "csv":
        rows = list(csv.DictReader(io.StringIO(response.text)))
        return {
            "effects": [row for row in rows if row["record_type"] == "study_effect"],
            "links": [row for row in rows if row["record_type"] == "knowledge_link"],
            "assertions": [row for row in rows if row["record_type"] == "evidence_assertion"],
            "csv_rows": rows,
        }
    return response.json()


def _assert_preserved(row, surface):
    selector = json.loads(row["effect_selector"]) if surface == "csv" else row["effect_selector"]
    results = json.loads(row["results"]) if surface == "csv" else row["results"]
    provenance = json.loads(row["provenance"]) if surface == "csv" else row["provenance"]
    limitations = json.loads(row["limitations"]) if surface == "csv" else row["limitations"]
    expected_version = row["id"].split("-")[1]
    expected_source = row["id"].removeprefix(f"assertion-{expected_version}-")
    assert selector == {
        "accession": "GSEFIXTURE", "contrast": "post_minus_pre", "analysis_version": expected_version
    }
    assert results[0]["analysis_version"] == expected_version
    assert results[0]["feature_id"] == f"NOS3-{expected_version}"
    assert results[0]["q_value"] == 0.8 and results[0]["effect_estimate"] == -0.2
    assert provenance["id"] == expected_source
    assert provenance["sha256"] == ("a" * 64 if expected_source == "open-source" else "b" * 64)
    assert provenance["source_location"].endswith("fixture only")
    assert limitations == ["Synthetic fixture only; no independent clinical validation"]
    assert row["individual_prediction"] == ("False" if surface == "csv" else False)


@pytest.mark.parametrize("surface", ["search", "json", "csv"])
@pytest.mark.parametrize("version", ["v1", "v2", "unavailable-version"])
def test_version_only_excludes_all_untyped_links(workspace, surface, version):
    result = _request(workspace, surface, version)
    assert result["links"] == []  # Metadata v2 cannot establish typed, reviewed version linkage.
    if surface != "csv":
        assert result["total_links"] == 0
    if version == "unavailable-version":
        assert result["effects"] == [] and result["assertions"] == []
        return
    assert [row["analysis_version"] for row in result["effects"]] == [version]
    assert float(result["effects"][0]["q_value"]) == 0.8
    expected = [f"assertion-{version}-open-source"]
    if surface == "search" and version == "v2":
        expected.append("assertion-v2-restricted-source")
    assert [row["id"] for row in result["assertions"]] == expected
    for assertion in result["assertions"]:
        _assert_preserved(assertion, surface)
    if surface != "search":
        expected_withheld = int(version == "v2")
        if surface == "json":
            assert result["export_withheld_records"] == expected_withheld
        else:
            assert {row["export_withheld_records"] for row in result["csv_rows"]} == {
                str(expected_withheld)
            }


@pytest.mark.parametrize("surface", ["search", "json", "csv"])
def test_no_version_retains_legacy_links_and_typed_results(workspace, surface):
    result = _request(workspace, surface)
    assert [row["source_ref"] for row in result["links"]] == [
        "legacy-missing", "legacy-v1", "legacy-v2"
    ]
    assert {row["analysis_version"] for row in result["effects"]} == {"v1", "v2"}
    assert len(result["assertions"]) == (3 if surface == "search" else 2)
    for assertion in result["assertions"]:
        _assert_preserved(assertion, surface)
    if surface == "json":
        assert result["total_links"] == 3 and result["export_withheld_records"] == 1
    elif surface == "csv":
        assert {row["export_withheld_records"] for row in result["csv_rows"]} == {"1"}
