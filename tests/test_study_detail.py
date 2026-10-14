from __future__ import annotations

from nutrigenomics.api import get_study
from nutrigenomics.db import create_schema, get_session_factory
from nutrigenomics.models import AnalysisRun, GeoSample, GeoSeries, ResearchStudy


def test_study_detail_separates_people_assays_and_biological_samples(tmp_path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'study.sqlite3').as_posix()}"
    create_schema(database_url)
    session = get_session_factory(database_url)()
    session.add(
        ResearchStudy(
            id="S03",
            title="Flavanol",
            description="Synthetic study fixture",
            metadata_json={
                "series_design": {
                    "GSE000003": {
                        "reported_people": 7,
                        "biological_samples": 14,
                        "sample_count_basis": "GEO overall_design; 7 paired arrays",
                    }
                },
                "study_design": "Paired pre-post design",
                "limitations": "The assay samples do not identify independent people.",
                "primary_sources": [{"label": "GEO", "url": "https://example.org/geo"}],
            },
        )
    )
    session.flush()
    session.add(
        GeoSeries(
            accession="GSE000003",
            study_id="S03",
            title="Expression series",
            sample_count=7,
        )
    )
    session.flush()
    session.add_all(
        [
            GeoSample(series_accession="GSE000003", sample_accession=f"GSM{i}")
            for i in range(1, 8)
        ]
    )
    session.add(
        AnalysisRun(
            series_accession="GSE000003",
            analysis_version="expression-v1",
            status="completed",
            metadata_json={"independent_people": 7, "sample_count": 7},
        )
    )
    session.commit()
    try:
        detail = get_study("GSE000003", session)
        assert detail["counts"]["reported_people"] == 7
        assert detail["counts"]["analysis_runs"] == [
            {
                "analysis_version": "expression-v1",
                "status": "completed",
                "people": 7,
                "assay_samples": 7,
                "source": "analysis run metadata",
            }
        ]
        assert detail["counts"]["geo_sample_records"] == 7
        assert detail["counts"]["biological_samples_reported"] == 14
        assert "two-color" in detail["counts"]["sample_count_note"]
        assert detail["study_design"] == "Paired pre-post design"
        assert detail["series_design"]["biological_samples"] == 14
        assert detail["primary_sources"][0]["url"] == "https://example.org/geo"
    finally:
        session.close()


def test_study_detail_leaves_unreported_people_unknown(tmp_path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'unknown-study.sqlite3').as_posix()}"
    create_schema(database_url)
    session = get_session_factory(database_url)()
    session.add(ResearchStudy(id="S00", title="Unknown", description="No cohort count"))
    session.flush()
    session.add(GeoSeries(accession="GSE000000", study_id="S00", title="Series"))
    session.commit()
    try:
        detail = get_study("GSE000000", session)
        assert detail["counts"]["reported_people"] is None
        assert detail["counts"]["analysis_runs"] == []
        assert detail["counts"]["geo_sample_records"] == 0
    finally:
        session.close()
