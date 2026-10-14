from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from nutrigenomics.api import export_evidence, search_evidence
from nutrigenomics.db import create_schema, get_session_factory
from nutrigenomics.models import GeoSeries, ResearchStudy, StudyEffect


def _database(tmp_path: Path) -> tuple[str, Session]:
    database_url = f"sqlite:///{(tmp_path / 'evidence.sqlite3').as_posix()}"
    create_schema(database_url)
    session = get_session_factory(database_url)()
    session.add(
        ResearchStudy(id="S01", title="Test", description="Evidence pagination test")
    )
    session.flush()
    session.add(GeoSeries(accession="GSE000001", study_id="S01", title="Test series"))
    session.flush()
    session.add_all(
        [
            StudyEffect(
                series_accession="GSE000001",
                feature_type="cpg",
                feature_id=f"cg{index:08d}",
                contrast="post_vs_baseline",
                analysis_version="test-v1",
                effect_estimate=index / 1000,
                q_value=index / 100,
            )
            for index in range(5)
        ]
    )
    session.commit()
    return database_url, session


def test_evidence_search_returns_stable_pages_and_total(tmp_path: Path) -> None:
    _, session = _database(tmp_path)
    try:
        indexes = inspect(session.get_bind()).get_indexes("study_effect")
        assert "ix_study_effect_series_type_q_id" in {index["name"] for index in indexes}
        result = search_evidence(
            query="",
            feature_type="cpg",
            accession="GSE000001",
            limit=2,
            offset=2,
            session=session,
        )
        assert result["total_effects"] == 5
        assert result["count"] == 2
        assert result["offset"] == 2
        assert [row["feature_id"] for row in result["effects"]] == [
            "cg00000002",
            "cg00000003",
        ]
    finally:
        session.close()


def test_evidence_export_honors_offset_and_limit(tmp_path: Path) -> None:
    _, session = _database(tmp_path)
    try:
        response = export_evidence(
            query="",
            feature_type="cpg",
            accession="GSE000001",
            format="csv",
            limit=2,
            offset=3,
            session=session,
        )
        body = response.body.decode("utf-8")
        assert "cg00000003" in body
        assert "cg00000004" in body
        assert "cg00000002" not in body
    finally:
        session.close()
