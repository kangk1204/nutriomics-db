import gzip
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nutrigenomics.expansion import connect, insert, migrate
from nutrigenomics.explorer import attach_explorer
from nutrigenomics.source_updates import (
    fetch_source,
    promote_release,
    validate_release,
    writer_lock,
)
from nutrigenomics.workbench_build import build_workbench_release
from scripts.collect_screen_complete import collect


def test_native_feature_type_alias_is_unique_and_not_pooled():
    from nutrigenomics.explorer_workbench import resolve_feature_type

    assert resolve_feature_type("gene_expression", ["gene", "pathway"]) == "gene"
    assert resolve_feature_type("gene_expression", ["gene_expression", "gene"]) == "gene_expression"
    assert resolve_feature_type("gene_expression", ["gene", "expression"]) is None
    assert resolve_feature_type("cpg", ["dmr"]) is None


@pytest.fixture
def sidecar(tmp_path):
    path = tmp_path / "annotations.sqlite3"
    db = connect(path)
    migrate(db)
    insert(
        db,
        "ng_release",
        {
            "id": "SRC",
            "source": "Test source",
            "version": "v1",
            "url": "https://example.org/source",
            "retrieved_at": "2026-10-09",
            "sha256": "a" * 64,
            "local_path": "source.tsv",
            "license": "Test only",
            "access_status": "public",
        },
    )
    for identifier, kind, ns in [
        ("Illumina:cg00000001", "molecular_feature", "Illumina"),
        ("Ensembl:ENSG00000000001", "gene", "Ensembl"),
        ("ENCODE-cCRE:EH38E1", "molecular_feature", "ENCODE-cCRE"),
    ]:
        insert(
            db,
            "ng_entity",
            {
                "id": identifier,
                "kind": kind,
                "label": identifier.split(":")[1],
                "namespace": ns,
                "identifier": identifier.split(":")[1],
                "metadata_json": "{}",
            },
        )
    for eid, subject, relation, tissue, context in [
        (
            "eqtm",
            "Illumina:cg00000001",
            "cis_eqtm",
            "whole blood",
            {"p_value_original": "<2.2e-16"},
        ),
        (
            "tested",
            "ENCODE-cCRE:EH38E1",
            "experimentally_tested_ccre_gene_pair",
            "K562",
            {"genome_build": "GRCh38/hg38", "source_interval": "chr1_100_200"},
        ),
        (
            "candidate",
            "Illumina:cg00000001",
            "cis_eqtm",
            "monocyte",
            {"review_status": "candidate"},
        ),
    ]:
        context = {"review_status": "source_checked", "export_allowed": False, **context}
        insert(
            db,
            "ng_evidence",
            {
                "id": eid,
                "subject_id": subject,
                "object_id": "Ensembl:ENSG00000000001",
                "relation": relation,
                "layer": "molecular_target",
                "release_id": "SRC",
                "source_ref": "Table row 1",
                "tier": "human_observational" if relation == "cis_eqtm" else "in_vitro",
                "inference": "association",
                "tissue": tissue,
                "value_status": "qualitative",
                "context_json": json.dumps(context),
            },
        )
    db.commit()
    db.close()
    return path


@pytest.fixture
def release(sidecar, tmp_path, monkeypatch):
    directory = tmp_path / "releases" / "r1"
    build_workbench_release(None, [sidecar], directory, "r1")
    monkeypatch.setenv("NUTRIGENOMICS_EXPLORER_DATABASE", str(directory / "explorer.sqlite"))
    app = FastAPI()
    attach_explorer(app)
    return directory, TestClient(app)


def test_gene_probe_and_regulation_stay_distinct(release):
    directory, client = release
    response = client.get(
        "/api/v1/explorer/evidence", params={"seed_id": "Ensembl:ENSG00000000001"}
    ).json()
    assert response["total"] == 2
    assert {r["layer"] for r in response["items"]} == {"eqtm", "regulatory_tested"}
    assert all(
        r["expert_review_status"] == "pending" and not r["export_allowed"]
        for r in response["items"]
    )
    assert (
        next(r for r in response["items"] if r["layer"] == "eqtm")["p_value_original"] == "<2.2e-16"
    )
    assert client.get("/api/v1/explorer/entities", params={"kind": "cpg"}).json()["total"] == 1
    assert (
        client.get("/api/v1/explorer/entities", params={"kind": "regulatory_region"}).json()[
            "total"
        ]
        == 1
    )
    assert validate_release(directory)["workbench"]["expert_review_status"] == "pending"


def test_tissue_filter_does_not_upgrade_k562_to_blood(release):
    _, client = release
    data = client.get("/api/v1/explorer/evidence", params={"tissue": "whole blood"}).json()
    assert data["total"] == 1 and data["items"][0]["layer"] == "eqtm"
    assert (
        client.get("/api/v1/explorer/evidence", params={"q": "%' OR 1=1 --"}).json()["total"] == 0
    )


def test_only_verified_genome_build_is_exposed(release):
    _, client = release
    result = client.get(
        "/api/v1/explorer/loci", params={"seed_id": "Ensembl:ENSG00000000001"}
    ).json()
    assert len(result["items"]) == 1 and result["unverified_or_unlocated_in_page"] == 0
    assert (
        client.get(
            "/api/v1/explorer/loci", params={"seed_id": "x", "genome_build": "hg19"}
        ).status_code
        == 422
    )


def test_release_hash_and_no_input_mutation(release, sidecar, tmp_path):
    directory, _ = release
    digest = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    next_dir = tmp_path / "releases" / "r2"
    build_workbench_release(directory, [sidecar], next_dir, "r2")
    assert hashlib.sha256(sidecar.read_bytes()).hexdigest() == digest
    assert validate_release(next_dir)["counts"]["workbench_by_layer"] == {
        "eqtm": 1,
        "regulatory_tested": 1,
    }
    with pytest.raises(FileExistsError):
        build_workbench_release(None, [sidecar], directory, "r1")


def test_omics_key_must_belong_to_release(release):
    _, client = release
    assert (
        client.get(
            "/api/v1/explorer/omics/results",
            params={
                "series": "GSEfake",
                "feature_type": "cpg",
                "contrast": "x",
                "analysis_version": "v1",
            },
        ).status_code
        == 404
    )


def test_original_clinical_assertion_wrapper_retains_its_estimand(release, tmp_path, monkeypatch):
    directory, _ = release
    from nutrigenomics.explorer_build import sha256

    assertion = {
        "layer": "health_outcome",
        "study_id": "fixture-clinical",
        "effect": -5.5,
        "effect_unit": "mmHg",
        "n_people": None,
        "source_ref": "fixture abstract",
        "value_status": "measured",
        "context_json": json.dumps(
            {"p_bound": "<0.001", "study_n_people": 459, "contrast_n_people": "not extracted"}
        ),
    }
    with sqlite3.connect(directory / "explorer.sqlite") as db:
        db.execute("INSERT INTO node VALUES('diet:test','diet','Fixture diet','','[]','{}','[]',0)")
        db.execute(
            "INSERT INTO node VALUES('outcome:sbp','outcome','Fixture SBP','','[]','{}','[]',0)"
        )
        db.execute(
            "INSERT INTO edge VALUES('clinical-source','diet:test','outcome:sbp','randomized_difference','human_randomized','source_checked','9606',?,'[\"SRC\"]','[]','[]')",
            (json.dumps({"original_assertion": assertion}),),
        )
        db.commit()
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["database_sha256"] = sha256(directory / "explorer.sqlite")
    (directory / "manifest.json").write_text(json.dumps(manifest))
    target = tmp_path / "releases/clinical"
    result = build_workbench_release(directory, [], target, "clinical")
    assert result["counts"]["workbench_by_layer"]["clinical"] == 1
    monkeypatch.setenv("NUTRIGENOMICS_EXPLORER_DATABASE", str(target / "explorer.sqlite"))
    app = FastAPI()
    attach_explorer(app)
    evidence = (
        TestClient(app)
        .get("/api/v1/explorer/evidence", params={"layer": "clinical"})
        .json()["items"][0]
    )
    assert evidence["effect"] == -5.5 and evidence["effect_unit"] == "mmHg"
    assert evidence["p_value_original"] == "<0.001" and evidence["n_people"] is None


def test_source_writer_lock(tmp_path):
    with writer_lock(tmp_path), pytest.raises(FileExistsError), writer_lock(tmp_path):
        pass
    assert not (tmp_path / ".update.lock").exists()


def test_screen_complete_has_no_panel_filter(tmp_path):
    lines = [
        "EH38E1\tENSG00000000001\tUNSELECTED\tprotein_coding\tGraphRegLR\tchr1_100_200\tACC\tK562\t0.2",
        "EH38E2\tENSG00000000002\tOTHER\tprotein_coding\trE2G\tchr1_200_300\tACC\tK562\t0.4",
    ]
    raw = tmp_path / "screen.gz"
    raw.write_bytes(gzip.compress(("\n".join(lines + [lines[0]]) + "\n").encode()))
    snapshot = {
        "object_path": str(raw),
        "sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
        "url": "https://example.org/source",
        "retrieved_at": "2026-10-09",
    }
    out = tmp_path / "full.sqlite3"
    report = collect(snapshot, out, "graphreg")
    assert report["counts"] == {
        "source_rows": 3,
        "admitted": 1,
        "excluded_other_method": 1,
        "duplicate": 1,
    }
    with sqlite3.connect(out) as db:
        assert db.execute("select effect,p_value,q_value from ng_evidence").fetchone() == (
            None,
            None,
            None,
        )
    partial = out.with_suffix(out.suffix + ".building")
    db.close()
    out.rename(partial)
    # An unfinished generated sidecar may require SQLite's hot-journal
    # recovery, whereas immutable input objects must stay read-only.
    import subprocess
    import sys

    writer = "import sqlite3,sys,os; d=sqlite3.connect(sys.argv[1]); d.execute('PRAGMA cache_size=1'); d.execute('BEGIN IMMEDIATE'); d.execute('UPDATE ng_entity SET label=?',('uncommitted'*20000,)); os._exit(0)"
    subprocess.run([sys.executable, "-c", writer, str(partial)], check=True)
    resumed = collect(snapshot, out, "graphreg", resume=True)
    assert resumed["resumed_partial"] and resumed["counts"] == report["counts"]
    with sqlite3.connect(out) as recovered:
        assert (
            recovered.execute("SELECT label FROM ng_entity WHERE kind='gene'").fetchone()[0]
            == "UNSELECTED"
        )


class Response:
    is_redirect = False
    status_code = 200

    def __init__(self, data, headers=None):
        self.data = data
        self.headers = {"Content-Length": str(len(data)), "ETag": "v1", **(headers or {})}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield self.data


class Session:
    def __init__(self, response):
        self.response = response
        self.headers = None

    def get(self, url, **kwargs):
        self.headers = kwargs["headers"]
        return self.response


def test_downloader_changed_source_preserves_old_bytes(tmp_path):
    source = {
        "id": "test",
        "url": "https://example.org/source",
        "allowed_hosts": ["example.org"],
        "max_bytes": 1000,
    }
    first = fetch_source(source, tmp_path, Session(Response(b"old")))
    second = fetch_source(source, tmp_path, Session(Response(b"new")))
    assert first["sha256"] != second["sha256"]
    assert Path(first["object_path"]).read_bytes() == b"old"
    assert Path(second["object_path"]).read_bytes() == b"new"


def test_downloader_bad_hash_does_not_replace_latest(tmp_path):
    source = {
        "id": "test",
        "url": "https://example.org/source",
        "allowed_hosts": ["example.org"],
        "max_bytes": 1000,
    }
    first = fetch_source(source, tmp_path, Session(Response(b"old")))
    with pytest.raises(ValueError):
        fetch_source({**source, "expected_sha256": "a" * 64}, tmp_path, Session(Response(b"wrong")))
    latest = json.loads((tmp_path / "receipts/test/latest.json").read_text())
    assert latest["sha256"] == first["sha256"]


def test_downloader_registry_rejects_cross_host(tmp_path):
    with pytest.raises(ValueError):
        fetch_source(
            {"id": "test", "url": "https://other.org/x", "allowed_hosts": ["example.org"]}, tmp_path
        )


def test_conditional_fetch_reuses_verified_object(tmp_path):
    source = {
        "id": "test",
        "url": "https://example.org/source",
        "allowed_hosts": ["example.org"],
        "max_bytes": 1000,
    }
    first = fetch_source(source, tmp_path, Session(Response(b"original")))
    response = Response(b"")
    response.status_code = 304
    session = Session(response)
    repeat = fetch_source(source, tmp_path, session)
    assert repeat["sha256"] == first["sha256"] and repeat["update_status"] == "unchanged"
    assert session.headers["If-None-Match"] == "v1"
    Path(first["object_path"]).write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="hash mismatch"):
        fetch_source(source, tmp_path, session)


def test_range_requires_same_validator_and_complete_bounds(tmp_path):
    source = {
        "id": "test",
        "url": "https://example.org/source",
        "allowed_hosts": ["example.org"],
        "max_bytes": 1000,
    }
    directory = tmp_path / "receipts/test"
    directory.mkdir(parents=True)
    (directory / "download.part").write_bytes(b"abc")
    (directory / "partial.json").write_text(json.dumps({"url": source["url"], "validator": "v1"}))
    response = Response(b"def", {"Content-Range": "bytes 3-5/6"})
    response.status_code = 206
    session = Session(response)
    result = fetch_source(source, tmp_path, session)
    assert (
        session.headers["Range"] == "bytes=3-"
        and Path(result["object_path"]).read_bytes() == b"abcdef"
    )


def test_changed_source_replaces_only_current_derivative(release, sidecar, tmp_path):
    directory, _ = release
    replacement = tmp_path / "replacement.sqlite3"
    replacement.write_bytes(sidecar.read_bytes())
    with sqlite3.connect(replacement) as db:
        db.execute("UPDATE ng_release SET id='SRC2',sha256=?", ("b" * 64,))
        db.execute("UPDATE ng_evidence SET id=id||'-v2',release_id='SRC2'")
        db.commit()
    new_dir = tmp_path / "releases/r2"
    manifest = build_workbench_release(directory, [replacement], new_dir, "r2")
    assert manifest["counts"]["workbench_by_layer"] == {"eqtm": 1, "regulatory_tested": 1}
    with sqlite3.connect(new_dir / "explorer.sqlite") as db:
        assert (
            db.execute("SELECT count(*) FROM workbench_evidence WHERE source_id='SRC'").fetchone()[
                0
            ]
            == 0
        )
    with sqlite3.connect(directory / "explorer.sqlite") as db:
        assert (
            db.execute("SELECT count(*) FROM workbench_evidence WHERE source_id='SRC'").fetchone()[
                0
            ]
            == 2
        )


def test_portable_package_keeps_permitted_composition_and_withholds_regulation(release, tmp_path):
    from nutrigenomics.explorer_build import sha256
    from scripts.package_workbench_dataset import package

    directory, _ = release
    with sqlite3.connect(directory / "explorer.sqlite") as db:
        db.execute(
            "INSERT INTO source VALUES('OPEN',?)",
            (json.dumps({"source": "test fixture", "export_allowed": True}),),
        )
        db.execute("INSERT INTO node VALUES('food:x','food','Fixture','','[]','{}','[]',0)")
        db.execute(
            "INSERT INTO node VALUES('nutrient:x','nutrient','Fixture component','','[]','{}','[]',0)"
        )
        db.execute(
            "INSERT INTO measurement VALUES('m','food:x','nutrient:x',5,'g','100g','measured','OPEN','{}')"
        )
        db.commit()
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["database_sha256"] = sha256(directory / "explorer.sqlite")
    (directory / "manifest.json").write_text(json.dumps(manifest))
    output = tmp_path / "package"
    result = package(directory, output)
    assert result["counts"]["measurement"] == 1 and result["counts"]["restricted_edges"] == 2
    with sqlite3.connect(output / "nutriomics-permitted.sqlite3") as db:
        assert db.execute("SELECT amount,unit,basis FROM measurement").fetchone() == (
            5,
            "g",
            "100g",
        )
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='workbench_evidence'"
        ).fetchone()
    assert not result["published"] and result["doi"] is None


def test_replication_missing_and_source_zero_are_not_numeric_zero_effects(tmp_path):
    import pandas as pd

    from scripts.collect_fhs_replication import collect as collect_replication

    workbook = tmp_path / "replication.xlsx"
    with pd.ExcelWriter(workbook) as writer:
        for sheet, probe in [("ST3", "cg00000001"), ("ST4", "cg00000002")]:
            rows = [[None] * 14 for _ in range(3)]
            rows[2][:2] = ["eGene", "CpG"]
            rows.append(
                [
                    "ENSG00000000001",
                    probe,
                    0.2,
                    0.1,
                    2.0,
                    0.04,
                    None,
                    None,
                    None,
                    None,
                    0.3,
                    0.05,
                    6.0,
                    0.0,
                ]
            )
            pd.DataFrame(rows).to_excel(writer, sheet_name=sheet, header=False, index=False)
    output = tmp_path / "replication.sqlite3"
    report = collect_replication(workbook, output)
    assert report["records"] == 4 and report["missing_results"] == 2
    with sqlite3.connect(output) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM ng_evidence WHERE effect IS NULL AND value_status='not_reported'"
            ).fetchone()[0]
            == 2
        )
        assert (
            db.execute(
                "SELECT count(*) FROM ng_evidence WHERE p_value=0 OR n_people IS NOT NULL"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.skipif(
    __import__("os").name == "nt", reason="atomic symlink promotion is verified on Linux"
)
def test_promote_and_rollback(release, sidecar, tmp_path):
    directory, _ = release
    second = tmp_path / "releases/r2"
    build_workbench_release(directory, [sidecar], second, "r2")
    promote_release(directory, tmp_path)
    receipt = promote_release(second, tmp_path)
    assert Path(receipt["previous"]) == directory
    promote_release(directory, tmp_path)
    assert (tmp_path / "current").resolve() == directory
