from __future__ import annotations

import json

from sqlalchemy.orm import Session

from nutrigenomics.db import create_schema, get_engine
from nutrigenomics.ingest import ingest_research_metadata, sha256_file
from nutrigenomics.models import Resource


def test_catalog_reingestion_preserves_runtime_provenance_and_versions_catalog(tmp_path):
    research = tmp_path / "research"
    research.mkdir()
    catalog_path = research / "resource_catalog.json"
    catalog = {"schema_version": "0.1", "as_of": "2026-09-30", "resources": [{
        "id": "R02", "name": "K-FIND", "layer": "composition", "priority": "P0",
        "source_url": "https://example.org/planning", "verification": "not downloaded",
        "use_conditions": "conditions pending", "selection_version": "catalog-old",
    }]}
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    (research / "geo_metadata_manifest.json").write_text("[]", encoding="utf-8")
    groups = tmp_path / "study_groups.json"
    groups.write_text("[]", encoding="utf-8")
    url = f"sqlite:///{(tmp_path / 'db.sqlite3').as_posix()}"
    create_schema(url)
    with Session(get_engine(url)) as session:
        session.add(Resource(
            id="R02", name="K-FIND downloaded", layer="composition", release="2026-07",
            sha256="a" * 64, local_path="/data/raw/manifest.json",
            source_url="https://example.org/actual-source", verification="hashes checked",
            use_conditions="verified release-specific conditions",
            metadata_json={"kfind_dataset_releases": {"15100065": "2026-07"},
                           "selection_version": "runtime-v1", "files": [{"sha256": "b" * 64}]},
        ))
        session.commit()
    for _ in range(2):
        ingest_research_metadata(research, groups, url)
    first_hash = sha256_file(catalog_path)
    catalog["as_of"] = "2026-10-01"
    catalog["resources"][0]["verification"] = "new planning description"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    ingest_research_metadata(research, groups, url)
    with Session(get_engine(url)) as session:
        resource = session.get(Resource, "R02")
        assert resource.source_url == "https://example.org/actual-source"
        assert resource.verification == "hashes checked"
        assert resource.use_conditions == "verified release-specific conditions"
        assert resource.release == "2026-07"
        assert resource.sha256 == "a" * 64
        assert resource.local_path == "/data/raw/manifest.json"
        metadata = resource.metadata_json
        assert metadata["selection_version"] == "runtime-v1"
        assert metadata["kfind_dataset_releases"] == {"15100065": "2026-07"}
        assert metadata["files"] == [{"sha256": "b" * 64}]
        assert metadata["catalog_snapshot"]["catalog_sha256"] == sha256_file(catalog_path)
        assert metadata["catalog_snapshot"]["record"]["verification"] == "new planning description"
        assert len(metadata["catalog_history"]) == 1
        assert metadata["catalog_history"][0]["catalog_sha256"] == first_hash


def test_new_resource_gets_catalog_defaults_without_claiming_a_download(tmp_path):
    research = tmp_path / "research"
    research.mkdir()
    item = {"id": "R01", "name": "RDA", "layer": "composition",
            "verification": "metadata only", "use_conditions": "pending release check"}
    (research / "resource_catalog.json").write_text(json.dumps({"resources": [item]}), encoding="utf-8")
    (research / "geo_metadata_manifest.json").write_text("[]", encoding="utf-8")
    groups = tmp_path / "groups.json"
    groups.write_text("[]", encoding="utf-8")
    url = f"sqlite:///{(tmp_path / 'new.sqlite3').as_posix()}"
    ingest_research_metadata(research, groups, url)
    with Session(get_engine(url)) as session:
        resource = session.get(Resource, "R01")
        assert resource.verification == "metadata only"
        assert resource.use_conditions == "pending release check"
        assert resource.release is None and resource.local_path is None
        assert resource.metadata_json["catalog_snapshot"]["record"] == item
