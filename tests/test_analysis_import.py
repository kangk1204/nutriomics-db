from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from nutrigenomics.api import download_analysis_auxiliary_file
from nutrigenomics.db import create_schema, get_session_factory
from nutrigenomics.models import AnalysisRun, GeoSeries, ResearchStudy, StudyEffect
from scripts.import_analysis_results import (
    _file_record,
    _import_cache_mib,
    _producer_output_matches,
    import_analysis_results,
)


@pytest.mark.parametrize("record,expected", [
    ("regions-v1", True),
    ("study-one/regions-v1", True),
    ("study-two/regions-v1", False),
    ("/original/expansion/outputs/study-one/regions-v1", True),
    ("/original/expansion/outputs/study-two/regions-v1", False),
    ("../study-one/regions-v1", False),
    (None, False),
])
def test_producer_output_keeps_nested_study_identity(record, expected):
    destination = Path("/production/outputs/study-one/regions-v1")
    assert _producer_output_matches(record, destination) is expected


@pytest.mark.parametrize("invalid", ["abc", "15", "8193", "0", "-1", "1.5"])
def test_import_cache_budget_rejects_invalid_configuration(monkeypatch, invalid):
    monkeypatch.setenv("NUTRIGENOMICS_IMPORT_CACHE_MIB", invalid)
    with pytest.raises(ValueError):
        _import_cache_mib()


def test_import_cache_budget_default_and_memory_guard(monkeypatch):
    monkeypatch.delenv("NUTRIGENOMICS_IMPORT_CACHE_MIB", raising=False)
    assert _import_cache_mib() == 512
    monkeypatch.setenv("NUTRIGENOMICS_IMPORT_CACHE_MIB", "8192")
    original = Path.read_text
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: "MemAvailable: 1024 kB\n"
                        if self.as_posix() == "/proc/meminfo" else original(self, *a, **k))
    with pytest.raises(MemoryError):
        _import_cache_mib()
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: "MemAvailable: 33554432 kB\n")
    assert _import_cache_mib() == 8192


def _write_tsv_gz(path: Path, rows: list[dict[str, str]]) -> None:
    headers = list(rows[0])
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        handle.write("\t".join(headers) + "\n")
        for row in rows:
            handle.write("\t".join(row.get(header, "") for header in headers) + "\n")


def test_imports_result_files_and_row_context_from_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite3"
    database_url = f"sqlite:///{db_path.as_posix()}"
    output_dir = tmp_path / "analysis"
    output_dir.mkdir()
    _write_tsv_gz(
        output_dir / "gene_effects.tsv.gz",
        [
            {
                "feature_type": "gene",
                "feature_id": "1234",
                "feature_label": "GENE1",
                "contrast": "post_6h_vs_fasting",
                "tissue": "whole blood cells",
                "timepoint": "6h",
                "intervention": "mixed test meal",
                "dose": "1000 kcal",
                "duration": "acute",
                "effect_estimate": "0.4",
                "standard_error": "0.1",
                "p_value": "0.002",
                "q_value": "0.02",
                "effect_unit": "log2 fold change",
                "status": "measured",
                "n_people": "14",
                "n_samples": "168",
                "metadata_json": '{"direction":"up"}',
            }
        ],
    )
    _write_tsv_gz(
        output_dir / "methylation_effects.tsv.gz",
        [
            {
                "feature_type": "cpg",
                "feature_id": "cg00000001",
                "contrast": "diet_arm_5y_vs_baseline",
                "effect_estimate": "-0.03",
                "p_value": "0.01",
                "q_value": "0.04",
                "effect_unit": "delta beta",
                "status": "measured",
                "n_people": "12",
                "n_samples": "24",
                "m_value_estimate": "-0.25",
                "genome_build": "hg19",
                "metadata_json": '{"inference_scope":"within-arm pre-post only"}',
            }
        ],
    )
    (output_dir / "sample_qc.tsv").write_text("sample\tqc\n", encoding="utf-8")
    script_path = tmp_path / "analysis_script.R"
    script_path.write_text("# changed after the run\n", encoding="utf-8")
    lock_path = tmp_path / "environment.lock"
    lock_path.write_text("changed after the run\n", encoding="utf-8")
    metadata = {
        "accession": "GSE56960",
        "analysis_version": "test-v1",
        "independent_people": 14,
        "sample_count": 168,
        "run_utc": "2026-09-30T00:00:00Z",
        "result_files": ["gene_effects.tsv.gz", "methylation_effects.tsv.gz"],
        "auxiliary_files": ["sample_qc.tsv"],
        "input_manifest": [{"path": "input.cel.gz", "sha256": "abc", "bytes": 123}],
        "effect_scope": "cohort-level repeated-measures result",
        "script_sha256": "a" * 64,
        "script_path": str(script_path),
        "environment_lock_path": str(lock_path),
        "environment_lock_sha256": "b" * 64,
    }
    (output_dir / "analysis_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    create_schema(database_url)
    session = get_session_factory(database_url)()
    session.add(
        ResearchStudy(
            id="S99",
            title="Test study",
            description="Synthetic study for importer validation",
        )
    )
    session.flush()
    session.add(
        GeoSeries(
            accession="GSE56960",
            study_id="S99",
            title="Test series",
        )
    )
    session.commit()
    session.close()

    counts = import_analysis_results(output_dir, database_url, "GSE56960")
    assert counts == {
        "inserted": 2,
        "updated": 0,
        "result_rows": 2,
        "gene_effects": 1,
        "cpg_effects": 1,
    }

    session = get_session_factory(database_url)()
    effects = list(session.scalars(select(StudyEffect).order_by(StudyEffect.feature_type)))
    run = session.scalar(select(AnalysisRun).where(AnalysisRun.series_accession == "GSE56960"))
    session.close()
    assert [(effect.feature_type, effect.feature_id) for effect in effects] == [
        ("cpg", "cg00000001"),
        ("gene", "1234"),
    ]
    gene = next(effect for effect in effects if effect.feature_type == "gene")
    assert gene.tissue == "whole blood cells"
    assert gene.timepoint == "6h"
    assert gene.dose == "1000 kcal"
    assert gene.n_people == 14
    assert gene.metadata_json["inference_scope"] == "cohort-level repeated-measures result"
    cpg = next(effect for effect in effects if effect.feature_type == "cpg")
    assert cpg.metadata_json["inference_scope"] == "within-arm pre-post only"
    assert cpg.metadata_json["m_value_estimate"] == "-0.25"
    assert cpg.metadata_json["genome_build"] == "hg19"
    assert run is not None
    assert run.source_script_sha256 == metadata["script_sha256"]
    assert run.metadata_json["script_file_current_sha256"] == _file_record(script_path)["sha256"]
    assert run.environment_lock_sha256 == metadata["environment_lock_sha256"]
    assert run.metadata_json["environment_lock_file_current_sha256"] == _file_record(lock_path)["sha256"]
    assert run.input_manifest_json == metadata["input_manifest"]
    assert len(run.output_manifest_json) == 4

    repeated = import_analysis_results(output_dir, database_url, "GSE56960")
    assert repeated["inserted"] == 0
    assert repeated["updated"] == 2


def test_import_rejects_result_path_traversal(tmp_path: Path) -> None:
    output_dir = tmp_path / "analysis"
    output_dir.mkdir()
    (output_dir / "analysis_metadata.json").write_text(
        json.dumps(
            {
                "accession": "GSE56960",
                "independent_people": 10,
                "result_files": ["../outside.tsv.gz"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="basenames"):
        import_analysis_results(
            output_dir,
            f"sqlite:///{(tmp_path / 'test.sqlite3').as_posix()}",
            "GSE56960",
        )


def test_new_release_rejects_incomplete_producer_marker_before_db_writes(tmp_path: Path) -> None:
    output=tmp_path/"new"
    output.mkdir()
    metadata={"accession":"GSE56960","analysis_version":"new-v1","independent_people":7,
              "run_utc":"2026-10-01T00:00:00Z","script_sha256":"a"*64,
              "result_files":["gene_effects.tsv.gz"]}
    (output/"analysis_metadata.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError,match="requires a DONE"):
        import_analysis_results(output, f"sqlite:///{(tmp_path/'blocked.sqlite3').as_posix()}", "GSE56960", True)
    (output/"DONE.json").write_text(json.dumps({"status":"completed","analysis_version":"new-v1",
        "accession":"GSE56960","files":[]}))
    with pytest.raises(ValueError,match="QC is not approved"):
        import_analysis_results(output, f"sqlite:///{(tmp_path/'blocked.sqlite3').as_posix()}", "GSE56960", True)
    assert not (tmp_path/'blocked.sqlite3').exists()


def test_auxiliary_download_is_allowlisted_and_confined_to_output_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nutrigenomics import api

    database_url = f"sqlite:///{(tmp_path / 'files.sqlite3').as_posix()}"
    output_root = tmp_path / "outputs"
    output_dir = output_root / "gse000001-v1"
    output_dir.mkdir(parents=True)
    qc_path = output_dir / "sample_qc.tsv"
    qc_path.write_text("sample\tqc\nGSM1\tpass\n", encoding="utf-8")
    monkeypatch.setattr(api, "DATA_DIR", tmp_path / "data")
    create_schema(database_url)
    session = get_session_factory(database_url)()
    session.add(ResearchStudy(id="S01", title="Test", description="File route"))
    session.flush()
    session.add(GeoSeries(accession="GSE000001", study_id="S01", title="Test series"))
    session.flush()
    session.add(
        AnalysisRun(
            series_accession="GSE000001",
            analysis_version="test-v1",
            status="completed",
            metadata_json={"auxiliary_files": ["sample_qc.tsv"]},
            output_manifest_json=[_file_record(qc_path)],
        )
    )
    session.commit()
    try:
        response = download_analysis_auxiliary_file(
            "GSE000001", "test-v1", "sample_qc.tsv", session
        )
        delivered = []

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            if message["type"] == "http.response.body":
                delivered.append(message.get("body", b""))

        asyncio.run(response(
            {"type": "http", "method": "GET", "headers": [], "asgi": {"spec_version": "2.4"}},
            receive, send,
        ))
        assert b"".join(delivered) == qc_path.read_bytes()
        with pytest.raises(HTTPException) as error:
            download_analysis_auxiliary_file("GSE000001", "test-v1", "../outside.tsv", session)
        assert getattr(error.value, "status_code", None) == 404
    finally:
        session.close()
