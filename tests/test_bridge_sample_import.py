"""Preserve DIRECT PLUS biological samples, channel rows and positional SDRF fields."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from nutrigenomics.db import create_schema, get_session_factory
from nutrigenomics.models import GeoSample, GeoSeries, ResearchStudy
from scripts.import_bridge_samples import direct_plus_samples

ACCESSION = "E-MTAB-12527"
HEADER = [
    "Source Name",
    "Characteristics[individual]",
    "Characteristics[age]",
    "Unit",
    "Characteristics[sex]",
    "Comment[Sample_Name]",
    "Protocol REF",
    "Protocol REF",
    "Factor Value[time]",
    "Unit",
    "Factor Value[diet]",
    "Array Design REF",
    "Label",
    "Array Data File",
]
SOURCE_URL = "https://www.ebi.ac.uk/biostudies/files/E-MTAB-12527/E-MTAB-12527.sdrf.txt"


def _write_sdrf(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(HEADER)
        writer.writerows(rows)


@pytest.fixture
def direct_bundle(tmp_path: Path) -> tuple[Path, list[list[str]], dict, str]:
    root = tmp_path / "expansion"
    raw = root / "raw" / ACCESSION
    raw.mkdir(parents=True)
    rows = []
    for person, diet in [("P01", "HDG"), ("P02", "greenMED")]:
        for time in ["0", "18"]:
            sample = f"{person}_{time}"
            for label, channel in [("Cy3", "Grn"), ("Cy5", "Red")]:
                rows.append(
                    [
                        sample,
                        person,
                        "54",
                        "year",
                        "female",
                        "sample_" + sample,
                        "P-DIRECT-sample",
                        "P-DIRECT-array",
                        time,
                        "month",
                        diet,
                        "A-MEXP-2256",
                        label,
                        f"{sample}_{channel}.idat",
                    ]
                )
    path = raw / (ACCESSION + ".sdrf.txt")
    _write_sdrf(path, rows)
    sources = {
        path.name: {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "url": SOURCE_URL,
        }
    }
    database_url = f"sqlite:///{(tmp_path / 'samples.sqlite3').as_posix()}"
    create_schema(database_url)
    with get_session_factory(database_url)() as session:
        session.add(ResearchStudy(id="N25", title="DIRECT PLUS", description="Synthetic registry"))
        session.flush()
        session.add(
            GeoSeries(
                accession=ACCESSION,
                study_id="N25",
                title="DIRECT PLUS public deposit",
                metadata_json={"registry_only": True, "paper_people": 260},
            )
        )
        session.commit()
    return root, rows, sources, database_url


def _import(root: Path, sources: dict, database_url: str) -> int:
    # Mirror the public importer caller's commit boundary. Closing on any helper
    # exception rolls back staged modifications to earlier biological samples.
    with get_session_factory(database_url)() as session:
        count = direct_plus_samples(session, root, sources)
        session.commit()
    return count


def _snapshot(database_url: str) -> str:
    with get_session_factory(database_url)() as session:
        result = {}
        for model in (GeoSeries, GeoSample):
            result[model.__tablename__] = [
                {column.name: getattr(row, column.name) for column in model.__table__.columns}
                for row in session.scalars(
                    select(model).order_by(*model.__table__.primary_key.columns)
                )
            ]
    return json.dumps(result, sort_keys=True, default=str)


def test_direct_sdrf_import_counts_people_samples_channels_and_preserves_duplicate_headers(
    direct_bundle,
) -> None:
    root, rows, sources, database_url = direct_bundle
    assert _import(root, sources, database_url) == 4
    with get_session_factory(database_url)() as session:
        samples = list(session.scalars(select(GeoSample).order_by(GeoSample.sample_accession)))
        series = session.get(GeoSeries, ACCESSION)
        assert len(samples) == 4 and series is not None
        assert len({sample.metadata_json["person_id"] for sample in samples}) == 2
        assert sum(sample.metadata_json["channel_count"] for sample in samples) == 8
        assert series.sample_count == 4
        assert series.metadata_json["independent_people"] == 2
        assert series.metadata_json["biological_sample_count"] == 4
        assert series.metadata_json["channel_count"] == 8
        assert series.metadata_json["paper_people"] == 260
        assert series.metadata_json["registry_only"] is False
        assert series.metadata_verified is True
        assert series.platform_ids == ["A-MEXP-2256"]
        for sample in samples:
            metadata = sample.metadata_json
            assert metadata["sdrf_columns"] == HEADER
            assert metadata["sdrf_columns"].count("Unit") == 2
            assert metadata["sdrf_columns"].count("Protocol REF") == 2
            assert metadata["time_unit"] == "month"
            assert metadata["source_sha256"] == sources[ACCESSION + ".sdrf.txt"]["sha256"]
            assert metadata["source_url"] == SOURCE_URL
            assert metadata["sdrf_records"] == [r for r in rows if r[0] == sample.sample_accession]
            assert {r[12] for r in metadata["sdrf_records"]} == {"Cy3", "Cy5"}
            assert all(r[3] == "year" and r[9] == "month" for r in metadata["sdrf_records"])
            assert all(r[6] != r[7] for r in metadata["sdrf_records"])
            assert f"time: {metadata['time_value']} month" in sample.characteristics
    before = _snapshot(database_url)
    assert _import(root, sources, database_url) == 4
    assert _snapshot(database_url) == before


@pytest.mark.parametrize(
    "failure",
    [
        "pair_time",
        "pair_diet",
        "channel_identity",
        "duplicate_channel",
        "missing_channel",
        "source_hash",
        "column_count",
    ],
)
def test_bad_direct_metadata_rolls_back_and_preserves_prior_samples(direct_bundle, failure) -> None:
    root, rows, sources, database_url = direct_bundle
    _import(root, sources, database_url)
    before = _snapshot(database_url)
    changed = [list(row) for row in rows]
    if failure == "pair_time":
        changed[-2][8] = changed[-1][8] = "12"
    elif failure == "pair_diet":
        changed[-2][10] = changed[-1][10] = "MED"
    elif failure == "channel_identity":
        # First-row pairing remains valid; the last channel fails only after
        # preceding sample objects have been staged with the replacement hash.
        changed[-1][1] = "P99"
    elif failure == "duplicate_channel":
        changed[-1][12] = "Cy3"
    elif failure == "missing_channel":
        changed.pop()
    elif failure == "column_count":
        changed[-1].pop()
    else:
        changed[-1][5] = "unverified replacement title"
    path = root / "raw" / ACCESSION / (ACCESSION + ".sdrf.txt")
    _write_sdrf(path, changed)
    replacement_sources = {path.name: {**sources[path.name]}}
    if failure != "source_hash":
        replacement_sources[path.name]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="DIRECT PLUS"):
        _import(root, replacement_sources, database_url)
    assert _snapshot(database_url) == before
