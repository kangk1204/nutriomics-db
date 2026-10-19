import hashlib
import json
from pathlib import Path

import pytest

from scripts import reacquire_clinical_sources as update


def registry(tmp_path, *, repeated=False):
    entry = {"id": "clinical-test", "filename": "source.xml", "expected_sha256": "a" * 64}
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"sources": [entry, entry] if repeated else [entry]}))
    return path


def test_changed_content_is_not_installed_or_approved(tmp_path, monkeypatch):
    obj = tmp_path / "changed-source"
    obj.write_bytes(b"changed licensed source; needs scientific review")
    actual = hashlib.sha256(obj.read_bytes()).hexdigest()
    def fetch(entry, destination):
        assert "expected_sha256" not in entry
        return {"object_path": str(obj), "sha256": actual}
    monkeypatch.setattr(update, "fetch_source", fetch)
    output = tmp_path / "update"
    report = update.reacquire(registry(tmp_path), output, check_latest=True)
    assert not (output / "source.xml").exists()
    assert report["fixed_source_matches"] == 0
    assert report["sources"][0]["status"] == "changed_requires_source_rights_and_scientific_review"
    assert report["automatic_expert_approval"] is False
    assert report["automatic_publication"] is False


def test_duplicate_source_identity_is_rejected_before_network(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "fetch_source", lambda *_: pytest.fail("must not fetch duplicates"))
    with pytest.raises(ValueError, match="Duplicate"):
        update.reacquire(registry(tmp_path, repeated=True), tmp_path / "out")


def test_fixed_snapshot_reproduction_matches_current_curation():
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "config/clinical_endpoints.v1.json").read_text(encoding="utf-8"))
    sources = json.loads((root / "config/clinical_source_downloads.v1.json").read_text(encoding="utf-8"))["sources"]
    assert len(sources) == 5
    assert len({s["id"] for s in sources}) == 5
    assert {s["expected_sha256"] for s in sources} == {s["sha256"] for s in config["sources"]}
    assert len(config["records"]) == 44
    assert all(r["expert_review_status"] == "pending" for r in config["records"])
