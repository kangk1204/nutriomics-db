import copy
import hashlib
import json
import sqlite3
import zipfile

import pytest

from nutrigenomics.db import create_schema, get_session_factory
from nutrigenomics.models import Resource
from scripts.import_fdc_json import DATASETS, import_fdc, repair_fdc_provenance


@pytest.fixture
def imported(tmp_path):
    folder = tmp_path / "raw/fdc"
    folder.mkdir(parents=True)
    files = []
    for index, dataset in enumerate(DATASETS):
        archive = folder / dataset["name"]
        payload = {dataset["root_keys"][0]: [{
            "fdcId": index + 100, "description": f"Food {index}",
            "foodNutrients": [{"nutrient": {"id": 1003, "name": "Protein", "unitName": "g"},
                               "amount": index + 1}],
        }]}
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("food.json", json.dumps(payload))
        files.append({"name": dataset["name"], "release": dataset["release"],
                      "url": "https://fdc.nal.usda.gov/fdc-datasets/" + dataset["name"],
                      "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                      "bytes": archive.stat().st_size})
    (folder / "download_manifest.json").write_text(json.dumps({"files": files}))
    database = tmp_path / "database.sqlite3"
    url = "sqlite:///" + database.as_posix()
    create_schema(url)
    with get_session_factory(url)() as s:
        s.add(Resource(id="R03", name="FDC", layer="composition", verification="bulk not collected",
                       use_conditions="Original retained terms", metadata_json={"custom": {"v": 7}}))
        s.commit()
    import_fdc(tmp_path, url)
    return tmp_path, database, url


def resource_snapshot(url):
    with get_session_factory(url)() as s:
        r = s.get(Resource, "R03")
        return {k: copy.deepcopy(getattr(r, k)) for k in (
            "verification", "source_url", "release", "sha256", "use_conditions", "metadata_json")}


def food_snapshot(database):
    with sqlite3.connect(database) as db:
        return {t: db.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()
                for t in ("food", "composition_measurement")}


def test_existing_resource_metadata_persists_across_reimport(imported):
    folder, _, url = imported
    first = resource_snapshot(url)
    assert first["metadata_json"]["custom"] == {"v": 7}
    assert set(first["metadata_json"]["local_archives"]) == {d["release"] for d in DATASETS}
    assert first["use_conditions"] == "Original retained terms"
    assert first["metadata_json"]["previous_source_summary"]["verification"] == "bulk not collected"
    assert first["sha256"] == hashlib.sha256((folder / "raw/fdc/download_manifest.json").read_bytes()).hexdigest()
    import_fdc(folder, url)
    assert resource_snapshot(url) == first


def test_provenance_only_preserves_exact_food_and_measurement_rows(imported):
    folder, database, url = imported
    before = food_snapshot(database)
    manifest = (folder / "raw/fdc/download_manifest.json").read_bytes()
    with get_session_factory(url)() as s:
        r = s.get(Resource, "R03")
        r.metadata_json = {"custom": {"v": 7}}
        r.verification = "bulk not collected"
        s.commit()
    result = repair_fdc_provenance(folder, url)
    assert result["before"] == result["after"]
    assert food_snapshot(database) == before
    assert (folder / "raw/fdc/download_manifest.json").read_bytes() == manifest
    assert len(resource_snapshot(url)["metadata_json"]["local_archives"]) == 2


@pytest.mark.parametrize("damage", ["archive", "release", "url", "bytes"])
def test_invalid_capture_never_changes_resource_or_measurements(imported, damage):
    folder, database, url = imported
    before, foods = resource_snapshot(url), food_snapshot(database)
    manifest_path = folder / "raw/fdc/download_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if damage == "archive":
        (folder / "raw/fdc" / DATASETS[1]["name"]).write_bytes(b"changed")
    else:
        manifest["files"][1][damage] = 1 if damage == "bytes" else "wrong"
        manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        repair_fdc_provenance(folder, url)
    assert resource_snapshot(url) == before
    assert food_snapshot(database) == foods


def test_import_failure_rolls_back_provenance_and_food_updates(imported):
    folder, database, url = imported
    before, foods = resource_snapshot(url), food_snapshot(database)
    archive = folder / "raw/fdc" / DATASETS[1]["name"]
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("food.json", "invalid json")
    path = folder / "raw/fdc/download_manifest.json"
    manifest = json.loads(path.read_text())
    manifest["files"][1].update(sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), bytes=archive.stat().st_size)
    path.write_text(json.dumps(manifest))
    with pytest.raises(json.JSONDecodeError):
        import_fdc(folder, url)
    assert resource_snapshot(url) == before
    assert food_snapshot(database) == foods
