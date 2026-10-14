import json
import shutil

import pytest
from test_explorer_api import explorer as explorer  # noqa: PLC0414 -- shared pytest fixture

from scripts.verify_explorer_performance_parity import verify


def test_http_failure_preserves_completed_table_comparisons(explorer, tmp_path, monkeypatch):
    candidate = tmp_path / "candidate.sqlite"
    shutil.copyfile(explorer["db"], candidate)
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps({"cases": [], "case_sha256": "fixture"}))
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    output = tmp_path / "parity.json"
    def fail(*args, **kwargs):
        raise TimeoutError("intentional fixture timeout")
    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(TimeoutError):
        verify(explorer["db"], candidate, tmp_path, tmp_path, "http://127.0.0.1:1", cases, manifest, output)
    report = json.loads(output.read_text())
    assert report["status"] == "failed" and report["phase"] == "meal_calculation"
    assert report["all_original_tables_equal"] is True
    assert report["table_comparisons"] and report["mismatches"] == []
    assert report["error_type"] == "TimeoutError"
