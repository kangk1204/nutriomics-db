import hashlib
import json

import pytest

from scripts.prepare_omics_replay import prepare


def test_historical_mismatch_stays_unresolved_without_claiming_execution(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    script = project / "analysis.R"
    script.write_text("# current, not the producer")
    lock = project / "environment.lock"
    lock.write_text("frozen")
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    data = {"runs": [{"series_accession": "GSE1", "analysis_version": "old",
                      "source_script_path": str(script), "source_script_sha256": "a" * 64,
                      "environment_lock_path": str(lock), "environment_lock_sha256": digest,
                      "output_manifest_json": [], "input_manifest_json": [], "metadata_json": {}}]}
    audit = project / "audit.json"
    audit.write_text(json.dumps(data))
    policy = project / "policy.json"
    policy.write_text(json.dumps({"analyses": [{"series_accession": "GSE1", "analysis_version": "old", "export_allowed": True}]}))
    output = project / "replay"
    result = prepare(audit, policy, project, output)
    assert result["analysis_count"] == 1 and result["executed_count"] == 0
    assert "source_script_historical_bytes_unresolved" in result["runs"][0]["issues"]
    assert not result["runs"][0]["exact_reproduction_claim_allowed"]
    assert script.read_text() == "# current, not the producer"
    with pytest.raises(FileExistsError):
        prepare(audit, policy, project, output)
