import json

import pytest

from scripts.refresh_platform_snapshots import expansion_input


def test_refresh_does_not_report_search_only_as_metadata_complete(tmp_path):
    home = tmp_path / "runtime"
    path = home / "v8-expansion/version/search-manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"corpus_id": "v8", "query_version": "v1", "expected_papers": 2,
                                "pmids": ["1", "2"], "status": "search_complete"}))
    (path.parent.parent / "state.json").write_text(json.dumps({"manifest": str(path), "running": True,
                                                               "stage": "metadata", "metadata_complete": False}))
    value = expansion_input(home)
    assert value["status"] == "running"
    assert not value["metadata_complete"]
    assert value["pmids"] == ["1", "2"]


def test_refresh_owned_manifest_path_and_initial_search(tmp_path):
    home = tmp_path / "runtime"
    assert expansion_input(home) is None
    path = home / "v8-expansion/state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"manifest": str(home / "v8-expansion/missing.json")}))
    assert expansion_input(home) is None
    path.write_text(json.dumps({"manifest": str(tmp_path / "outside.json")}))
    with pytest.raises(ValueError, match="escaped"):
        expansion_input(home)
