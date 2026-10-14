"""Unit fixtures below are synthetic repository records, never evaluation data."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.build_bridge_benchmark import ROOT, build
from scripts.evaluate_bridge_benchmark import digest, load_queries, source_integrity, wilson
from scripts.import_human_bridge import import_bridge


@pytest.fixture
def benchmark(tmp_path: Path):
    document = json.loads((ROOT / "config" / "human_exposures.v1.json").read_text(encoding="utf-8"))
    directory = tmp_path / "benchmark"
    sources = directory / "sources"
    sources.mkdir(parents=True)
    for study in document["studies"]:
        for accession in study["series"]:
            if accession.startswith("GSE"):
                (sources / f"{accession}.soft").write_text(
                    f"^SERIES = {accession}\n!Series_type = Synthetic fixture\n"
                    f"!Series_pubmed_id = {study['pubmed_id']}\n!Series_platform_id = TEST_PLATFORM\n",
                    encoding="utf-8",
                )
    (sources / "E-MTAB-12527.sdrf.txt").write_text(
        "Source Name\tCharacteristics[individual]\tFactor Value[time]\tFactor Value[diet]\tArray Data File\n"
        "TEST_SAMPLE_0\tTEST_PERSON\t0 month\tTEST_ARM\tTEST_FILE_RED\n"
        "TEST_SAMPLE_18\tTEST_PERSON\t18 month\tTEST_ARM\tTEST_FILE_GREEN\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "fixture-manifest.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    payload = build(manifest, directory, offline=True)
    return directory, payload, manifest


def test_fixed_trial_partition_and_no_padding(benchmark):
    directory, manifest, _ = benchmark
    assert manifest["queries"] == {"development": 70, "holdout": 30, "total": 100}
    assert not set(manifest["source_groups"]["development"]) & set(manifest["source_groups"]["holdout"])
    assert manifest["source_assertions"]["actual"] < 200
    assert manifest["source_assertions"]["missing"] > 0
    assert manifest["source_assertions"]["expert_verified"] == 0
    assert manifest["scientific_accuracy"] == "not_evaluated"
    facts = json.loads((directory / "source_facts.candidate.json").read_text())
    assert len(facts) == len({f["id"] for f in facts})
    assert all(f["review_status"] == "expert_review_pending" for f in facts)


def test_source_fields_and_hashes_are_verified(benchmark):
    directory, _, _ = benchmark
    result = source_integrity(directory)
    assert not result["errors"]
    assert all(result["files_verified"].values())
    assert result["source_field_checks"]["successes"] == result["atomic_facts"]
    source = next((directory / "sources").glob("*.soft"))
    source.write_text("changed fixture source", encoding="utf-8")
    changed = source_integrity(directory)
    assert any(e["reason"] == "source_snapshot_hash_mismatch" for e in changed["errors"])


def test_holdout_requires_each_query_and_fact_expert_acceptance(benchmark):
    directory, _, _ = benchmark
    assert len(load_queries(directory, "development")) == 70
    with pytest.raises(PermissionError, match="sealed"):
        load_queries(directory, "holdout")
    acceptance = {
        "status": "accepted", "reviewer": "synthetic test reviewer", "reviewed_at": "fixture-only",
        "manifest_sha256": digest(directory / "manifest.json"), "query_reviews": [], "fact_reviews": [],
    }
    (directory / "expert_acceptance.json").write_text(json.dumps(acceptance), encoding="utf-8")
    with pytest.raises(PermissionError, match="every"):
        load_queries(directory, "holdout")


def test_frozen_benchmark_cannot_be_silently_rebuilt(benchmark):
    directory, _, path = benchmark
    with pytest.raises(FileExistsError, match="frozen"):
        build(path, directory, offline=True)


def test_wilson_reports_denominator_and_missing_interval():
    assert wilson(0, 0)["ci95"] is None
    interval = wilson(95, 100)
    assert interval["denominator"] == 100
    assert interval["estimate"] == 0.95
    assert interval["ci95"][0] < 0.95 < interval["ci95"][1]


def test_imported_exposure_assertions_obey_foreign_keys(benchmark, tmp_path):
    _, _, manifest = benchmark
    url = f"sqlite:///{(tmp_path / 'import.sqlite3').as_posix()}"
    imported = import_bridge(manifest, url)
    assert imported["studies"] == 9
    assert imported["exposures"] > 0
    assert imported["expert_review_status"] == "pending"
