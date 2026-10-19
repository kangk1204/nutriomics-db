"""Freeze real small-release cases and reject fast empty clinical responses."""
import copy
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nutrigenomics.clinical_endpoints import attach_clinical_endpoints, load_clinical_config
from nutrigenomics.explorer import attach_explorer
from nutrigenomics.explorer_build import SCHEMA, canonical, sha256
from nutrigenomics.public_service import create_app
from nutrigenomics.release_contract import build_release_contract, install_release_contract
from nutrigenomics.workbench_build import WORKBENCH_SCHEMA
from scripts.freeze_candidate_performance import freeze_candidates
from scripts.freeze_final_upgrade_performance import freeze_final_upgrade, verify_recorded_responses
from scripts.benchmark_explorer_performance import digest


def release_fixture(root, public):
    root.mkdir()
    config_path = Path(__file__).resolve().parents[1] / "config/clinical_endpoints.v1.json"
    config = load_clinical_config(config_path)
    filename = "nutriomics.sqlite3" if public else "explorer.sqlite"
    database = root / filename
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA + WORKBENCH_SCHEMA + """
          CREATE TABLE science_record(id TEXT,kind TEXT,metadata_json TEXT,PRIMARY KEY(kind,id));
          CREATE TABLE effect_selector(series_accession TEXT,analysis_version TEXT,contrast TEXT,feature_type TEXT,total INTEGER);
          CREATE TABLE perf_effect_selector(selector TEXT PRIMARY KEY,series TEXT,version TEXT,contrast TEXT,total INTEGER);
          CREATE TABLE perf_effect_type(selector TEXT,feature_type TEXT,total INTEGER);
          CREATE TABLE candidate_document(pmid TEXT PRIMARY KEY,source_id TEXT,source_sha256 TEXT,
            extraction_version TEXT,candidate_count INTEGER,retracted INTEGER,metadata_json TEXT);
        """)
        db.execute("INSERT INTO meta VALUES('release_id',?)", (canonical("fixture-final-r"),))
        db.execute("INSERT INTO source VALUES('own',?)", (canonical({"id": "own", "url": "https://example.org/own",
            "version": "v1", "export_allowed": True}),))
        nodes = [("K-FIND:" + str(i).zfill(3), "food", "Rice " + str(i)) for i in range(50)]
        nodes += [("USDA-FDC:2705418", "food", "Yogurt, whole milk, plain"),
                  ("NCBIGene:348", "gene", "APOE"), ("nutrient:fat", "nutrient", "Total fat"),
                  ("ENCODE-cCRE:EH38E1", "regulatory_region", "EH38E1")]
        for identifier, kind, label in nodes:
            db.execute("INSERT INTO node VALUES(?,?,?,?,?,?,?,?)", (identifier, kind, label, label, "[]", "{}", "[]", 1))
        db.execute("INSERT INTO edge VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("prediction", "ENCODE-cCRE:EH38E1", "NCBIGene:348",
            "predicted_ccre_gene_link", "predicted", "candidate", "9606", "{}", '["own"]', "[]", "[]"))
        db.execute("INSERT INTO workbench_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("prediction", "ENCODE-cCRE:EH38E1",
            "NCBIGene:348", "predicted_ccre_gene_link", "regulatory_predicted", None, "own", "K562", "9606", "candidate", 1, "{}"))
        if not public:
            db.execute("INSERT INTO edge VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("literature", "K-FIND:000", "NCBIGene:348",
                "candidate_relationship", "extracted", "candidate", None, "{}", '["own"]', "[]", "[]"))
            db.execute("INSERT INTO workbench_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("literature", "K-FIND:000",
                "NCBIGene:348", "candidate_relationship", "literature_candidate", None, "own", None, None, "candidate", 0, "{}"))
        db.execute("INSERT INTO candidate_document VALUES(?,?,?,?,?,?,?)", ("1", "own", "a" * 64, "fixture-v1", 1, 0,
            canonical({"assertion_visibility": "withheld" if public else "internal_review"})))
        for audit in config["study_audits"]:
            study = {"id": audit["study_id"], "title": audit["study_id"], "metadata": {"series": []}}
            db.execute("INSERT INTO science_record VALUES(?,?,?)", (study["id"], "study", canonical(study)))
        selectors = [("GSE98645", "yogurt-v1", "acute", "gene", 2),
                     ("GSE107205", "methyl-v1", "baseline", "cpg", 2),
                     ("GSE107205", "between-arm-v4-rebuild20261017", "change", "cpg", 3)]
        for index, (series, version, contrast, feature, total) in enumerate(selectors):
            db.execute("INSERT INTO effect_selector VALUES(?,?,?,?,?)", (series, version, contrast, feature, total))
            db.execute("INSERT INTO perf_effect_selector VALUES(?,?,?,?,?)", (str(index), series, version, contrast, total))
            db.execute("INSERT INTO perf_effect_type VALUES(?,?,?)", (str(index), feature, total))
        attach_clinical_endpoints(db, config)
        contract = build_release_contract(db, verify_effect_counts=False)
        install_release_contract(db, contract)
    db.close()
    files = []
    if public:
        (root / "review").mkdir()
        for name, payload in (("clinical-endpoints.json", config), ("release-contract.json", contract),
                              ("review/manifest.json", {"expert_review_status": "pending"})):
            (root / name).write_text(canonical(payload), encoding="utf-8")
        for path in [root / name for name in (filename, "clinical-endpoints.json", "release-contract.json", "review/manifest.json")]:
            files.append({"path": path.relative_to(root).as_posix(), "sha256": sha256(path), "bytes": path.stat().st_size})
        (root / "dataset-metadata.json").write_text(canonical(contract), encoding="utf-8")
    (root / "manifest.json").write_text(canonical({"release_id": "fixture-final-r", "database_sha256": sha256(database), "files": files}), encoding="utf-8")
    return database, config_path


@pytest.mark.parametrize("public", [False, True])
def test_all_inherited_prediction_and_literature_cases_and_caps_are_preserved(tmp_path, public):
    release = tmp_path / "release"
    database, config = release_fixture(release, public)
    before = sha256(database)
    inherited = freeze_candidates(release, tmp_path / "base.json", public)
    spec = freeze_final_upgrade(release, tmp_path / "final.json", public, config)
    assert spec["cases"][:len(inherited["cases"])] == inherited["cases"]
    assert spec["final_upgrade_scope"]["inherited_case_sha256"] == inherited["case_sha256"]
    assert sha256(database) == before
    assert spec["case_sha256"] == digest(spec["cases"])
    added = spec["cases"][len(inherited["cases"]):]
    assert len(added) == (18 if public else 14)
    assert all(case["params"]["max_nodes"] == 250 and case["params"]["max_edges"] == 750
               for case in added if case["id"].endswith("maximum"))
    assert "public-manifest-file-index" in {c["id"] for c in added} if public else "public-manifest-file-index" not in {c["id"] for c in added}
    assert spec["final_upgrade_scope"]["clinical_result_records"] == 44
    with pytest.raises(FileExistsError):
        freeze_final_upgrade(release, tmp_path / "final.json", public, config)


def test_prior_domain_regressions_are_preserved_and_changed_baselines_rejected(tmp_path):
    release = tmp_path / "release"
    _, config = release_fixture(release, False)
    base = freeze_candidates(release, tmp_path / "base.json")
    extra = {"id": "post-browser-domain-regression", "method": "GET", "path": "/api/v1/explorer/graph",
             "params": {"seed_id": "USDA-FDC:2705418", "domain": "lipids"}, "target_s": 1}
    base["cases"].append(extra)
    base["case_sha256"] = digest(base["cases"])
    prior = tmp_path / "prior.json"
    prior.write_text(canonical(base), encoding="utf-8")
    spec = freeze_final_upgrade(release, tmp_path / "final.json", False, config, prior)
    assert spec["cases"][:len(base["cases"])] == base["cases"]
    base["cases"][0]["params"]["q"] = "changed query"
    base["case_sha256"] = digest(base["cases"])
    prior.write_text(canonical(base), encoding="utf-8")
    with pytest.raises(ValueError, match="Inherited frozen case changed"):
        freeze_final_upgrade(release, tmp_path / "changed.json", False, config, prior)
    assert not (tmp_path / "changed.json").exists()


@pytest.mark.parametrize("sql", [
    "DELETE FROM science_record WHERE kind='clinical_endpoint_result' AND id=(SELECT min(id) FROM science_record WHERE kind='clinical_endpoint_result')",
    "DELETE FROM node WHERE id='ClinicalEndpoint:bmi'",
    "DELETE FROM workbench_evidence WHERE layer='clinical' AND id=(SELECT min(id) FROM workbench_evidence WHERE layer='clinical')",
])
def test_absent_expected_clinical_data_cannot_leave_a_valid_frozen_spec(tmp_path, sql):
    release = tmp_path / "release"
    database, config = release_fixture(release, False)
    with sqlite3.connect(database) as db:
        db.execute(sql)
    with pytest.raises(ValueError, match="[Cc]linical|[Hh]ealth"):
        freeze_final_upgrade(release, tmp_path / "invalid.json", False, config)
    assert not (tmp_path / "invalid.json").exists()


@pytest.mark.parametrize("public", [False, True])
def test_actual_clinical_api_responses_are_checked_and_fast_empty_results_fail(tmp_path, monkeypatch, public):
    release = tmp_path / "release"
    database, config = release_fixture(release, public)
    spec_path = tmp_path / "cases.json"
    spec = freeze_final_upgrade(release, spec_path, public, config)
    monkeypatch.setenv("NUTRIGENOMICS_EXPLORER_DATABASE", str(database))
    monkeypatch.setenv("NUTRIGENOMICS_EXPLORER_CORE_DATABASE", str(database))
    if public:
        app = create_app(release, allow_candidate=True)
    else:
        app = FastAPI()
        attach_explorer(app)
    responses = tmp_path / "responses"
    responses.mkdir()
    # Timing correctness is independently tested by the benchmark module. Here
    # actual API responses exercise all newly added semantic expectations.
    with TestClient(app) as client:
        for case in spec["cases"]:
            if "expected_response" in case:
                response = client.get(case["path"], params=case["params"])
                assert response.status_code == 200, response.text
                value = response.json()
            else:
                value = {"release_id": spec["baseline_release"]}
            (responses / (case["id"] + "-response.json")).write_text(canonical(value), encoding="utf-8")
    (responses / "performance.json").write_text(canonical({"case_sha256": spec["case_sha256"]}))
    verified = verify_recorded_responses(spec_path, responses)
    assert verified["passed"], verified["issues"]
    path = responses / "clinical-study-n15-response.json"
    empty = json.loads(path.read_text(encoding="utf-8"))
    empty["clinical_endpoints"] = []
    path.write_text(canonical(empty), encoding="utf-8")
    verified = verify_recorded_responses(spec_path, responses)
    assert not verified["passed"] and any(i["code"] == "clinical_result_ids_or_values_changed" for i in verified["issues"])
    spec["cases"][0]["target_s"] = .01
    spec_path.write_text(canonical(spec), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA mismatch"):
        verify_recorded_responses(spec_path, responses)
