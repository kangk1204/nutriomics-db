"""Rights-aware exposure snapshots; actual JSON/HTML export routes, not browser QA."""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from test_explorer_api import PRIVATE, RELEASE, SECRET
from test_explorer_api import (
    explorer as explorer,  # noqa: PLC0414 -- independent SQLite/API fixture
)

from nutrigenomics.explorer import _clean_node


def add_exposure(explorer, *, source_id="open", original_id="source-exposure", node_id=None,
                 terms=None, kind="diet", entity_type="exposure"):
    original = {"id": original_id, "study_id": "S03", "label": "Original grape-seed extract",
                "kind": "component_group", "amount": 200, "unit": "mg", "duration_days": 56,
                "dose_basis": "assigned 200 mg/day mixture", "formulation": "two capsules/day",
                "source_id": source_id, "original_record_sha256": "b" * 64,
                "publication_review_status": "pending", "expert_review_status": "pending",
                "food_terms": json.dumps(["포도씨 추출물", "grape seed extract"]) if terms is None else terms,
                "metadata_json": json.dumps({"dose_frequency": "per_day", "expert_review_status": "pending",
                                             "local_path": PRIVATE, "private_extra": SECRET,
                                             "reported_components": {"hidden": SECRET}}),
                "local_path": PRIVATE, "private_payload": SECRET}
    identifier = node_id or "Exposure:" + original_id
    metadata = {"entity_type": entity_type, "original_exposure": original,
                "source_ids": [source_id], "expert_review_status": "pending", "private_payload": SECRET}
    with sqlite3.connect(explorer["db"]) as db:
        db.execute("INSERT INTO node VALUES(?,?,?,?,?,?,?,?)",
                   (identifier, kind, "Research exposure", None,
                    json.dumps(["포도씨 추출물", "grape seed extract"]), json.dumps(metadata), "[]", 0))
    return identifier


def exported(explorer, identifier, format):
    response = explorer["client"].post("/api/v1/explorer/export", json={
        "format": format, "release_id": RELEASE, "node_ids": [identifier], "edge_ids": []})
    assert response.status_code == 200, response.text
    if format == "json":
        return response.json(), response.text
    match = re.search(r"window\.__NUTRIOMICS_SNAPSHOT__=(.*?);</script>", response.text, re.DOTALL)
    assert match
    return json.loads(match.group(1)), response.text


@pytest.mark.parametrize("format", ["json", "html"])
def test_permitted_exact_exposure_keeps_dose_aliases_and_review_without_raw_fields(explorer, format):
    identifier = add_exposure(explorer)
    snapshot, body = exported(explorer, identifier, format)
    compact = snapshot["nodes"][0]["metadata"]
    detail = snapshot["node_details"][identifier]["metadata"]
    assert compact == detail
    assert detail["exposure_export_status"] == "permitted"
    original = detail["original_exposure"]
    assert original["id"] == "source-exposure"
    assert original["study_id"] == "S03"
    assert (original["amount"], original["unit"], original["duration_days"]) == (200, "mg", 56)
    assert original["dose_basis"] == "assigned 200 mg/day mixture"
    assert original["food_terms"] == ["포도씨 추출물", "grape seed extract"]
    assert original["metadata"] == {"dose_frequency": "per_day", "expert_review_status": "pending"}
    assert original["original_record_sha256"] == "b" * 64
    assert original["publication_review_status"] == "pending"
    assert "metadata_json" not in original
    assert PRIVATE not in body and SECRET not in body
    assert "reported_components" not in body and "private_payload" not in body
    assert not any(key in detail for key in ("core_food_id", "calculation_food_id", "food_id"))
    if format == "html":
        assert "connect-src 'none'" in body
        assert "<script src=" not in body


@pytest.mark.parametrize("source_id", ["closed", "unknown", "missing-source"])
@pytest.mark.parametrize("format", ["json", "html"])
def test_source_deny_or_unknown_never_exports_exposure_values(explorer, source_id, format):
    identifier = add_exposure(explorer, source_id=source_id)
    snapshot, body = exported(explorer, identifier, format)
    for node in (snapshot["nodes"][0], snapshot["node_details"][identifier]):
        assert "original_exposure" not in node["metadata"]
        assert node["metadata"]["exposure_export_status"] == "withheld"
        assert node["metadata"]["exposure_withheld_reason"] == "exposure_source_redistribution_not_permitted"
    assert "assigned 200 mg/day mixture" not in body
    assert "two capsules/day" not in body
    assert PRIVATE not in body and SECRET not in body


@pytest.mark.parametrize("changes", [
    {"node_id": "Exposure:wrong-source"}, {"kind": "food"}, {"entity_type": "clinical_study"},
])
def test_source_permission_does_not_replace_exact_exposure_identity(explorer, changes):
    identifier = add_exposure(explorer, **changes)
    snapshot, _ = exported(explorer, identifier, "json")
    metadata = snapshot["node_details"][identifier]["metadata"]
    assert "original_exposure" not in metadata
    assert metadata["exposure_withheld_reason"] == "canonical_exposure_identifier_mismatch"


@pytest.mark.parametrize("terms", [{"alias": "hidden"}, ["x"] * 51, ["x" * 251], [1], "invalid-json"])
def test_invalid_or_oversized_aliases_are_withheld_without_corrupting_permitted_dose(explorer, terms):
    identifier = add_exposure(explorer, terms=terms)
    snapshot, _ = exported(explorer, identifier, "json")
    metadata = snapshot["node_details"][identifier]["metadata"]
    assert metadata["original_exposure"]["amount"] == 200
    assert "food_terms" not in metadata["original_exposure"]
    assert metadata["exposure_export_status"] == "partial"
    assert metadata["exposure_withheld_fields"] == ["food_terms"]


def test_clean_node_without_permission_context_fails_closed():
    result = _clean_node({"id": "Exposure:s1", "kind": "diet", "metadata": {
        "entity_type": "exposure", "original_exposure": {"id": "s1", "source_id": "open", "amount": 200}}})
    assert "original_exposure" not in result["metadata"]
    assert result["metadata"]["exposure_withheld_reason"] == "exposure_source_permission_unavailable"


def test_permitted_zero_and_unreported_duration_are_distinct(explorer):
    identifier = add_exposure(explorer)
    with sqlite3.connect(explorer["db"]) as db:
        metadata = json.loads(db.execute("SELECT metadata_json FROM node WHERE id=?", (identifier,)).fetchone()[0])
        metadata["original_exposure"].update(amount=0, duration_days=None)
        db.execute("UPDATE node SET metadata_json=? WHERE id=?", (json.dumps(metadata), identifier))
    snapshot, _ = exported(explorer, identifier, "json")
    original = snapshot["node_details"][identifier]["metadata"]["original_exposure"]
    assert original["amount"] == 0
    assert original["duration_days"] is None


def test_actual_export_snapshot_offline_adapter_finds_source_alias_without_http(explorer, tmp_path):
    frontend = Path(__file__).resolve().parents[1] / "frontend" / "explorer"
    node = shutil.which("node")
    if node is None or not (frontend / "node_modules" / "esbuild").is_dir():
        pytest.skip("Actual frontend adapter check requires Node and frozen frontend dependencies")
    identifier = add_exposure(explorer)
    snapshot, _ = exported(explorer, identifier, "html")
    path = tmp_path / "actual-export-snapshot.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    script = """
      const fs=await import('node:fs');const Module=(await import('node:module')).default;
      const path=await import('node:path');const assert=(await import('node:assert/strict')).default;
      const {build}=await import('esbuild');
      const snapshot=JSON.parse(fs.readFileSync(process.argv[1],'utf8'));
      globalThis.window={__NUTRIOMICS_SNAPSHOT__:snapshot};let requests=0;
      globalThis.fetch=()=>{requests++;throw new Error('Offline adapter may not fetch')};
      const compiled=await build({entryPoints:['src/api.ts'],bundle:true,write:false,platform:'node',format:'cjs'});
      const filename=path.resolve('src/__export_alias_runtime.cjs');const instance=new Module(filename);
      instance.filename=filename;instance.paths=Module._nodeModulePaths(process.cwd());
      instance._compile(compiled.outputFiles[0].text,filename);
      const result=await instance.exports.getInterventions('포도씨 추출물');
      assert.equal(result.total,1);assert.equal(result.items[0].id,'Exposure:source-exposure');
      assert.equal(result.items[0].metadata.original_exposure.amount,200);assert.equal(requests,0);
      console.log(JSON.stringify({actual_export_offline_alias_adapter:'pass',requests,browser_execution:false}));
    """
    completed = subprocess.run([node, "--input-type=module", "-e", script, str(path)], cwd=frontend,
                               capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["requests"] == 0
