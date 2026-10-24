import json
from pathlib import Path

import pytest

from nutrigenomics.submission_package import sha256
from scripts import promote_verified_release as tool


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "nutriomics.sqlite3").write_bytes(b"immutable fixture native database bytes")
    (root / "effects").mkdir()
    (root / "effects/result.parquet").write_bytes(b"public typed immutable result")
    files = [{"path": p.relative_to(root).as_posix(), "bytes": p.stat().st_size, "sha256": sha256(p)} for p in root.rglob("*") if p.is_file()]
    manifest = {"release_id": "new-pending", "submission_ready": False, "database_sha256": sha256(root / "nutriomics.sqlite3"), "files": files}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return {"source": root, "output": tmp_path / "permanent", "expected_manifest_sha256": sha256(root / "manifest.json"), "receipt": tmp_path / "proof/materialization.json"}


def test_exact_byte_materialization_preserves_manifest_and_is_not_activation(source):
    result = tool.promote(**source)
    assert result["passed"] and result["source_unchanged"] and result["activated"] is False
    assert result["files"] == 3 and result["publication_readiness_inferred"] is False
    for p in source["source"].rglob("*"):
        if p.is_file():
            assert p.read_bytes() == (source["output"] / p.relative_to(source["source"])).read_bytes()
    assert source["receipt"].exists()
    with pytest.raises(FileExistsError):
        tool.promote(**source)


@pytest.mark.parametrize("fault", ["manifest", "partial", "same_size_changed", "wal", "escape", "duplicate"])
def test_untrusted_or_partial_source_is_not_published(source, fault):
    root = source["source"]
    if fault == "manifest":
        (root / "manifest.json").write_text("{}")
    elif fault == "partial":
        (root / "effects/result.parquet").unlink()
    elif fault == "same_size_changed":
        path = root / "effects/result.parquet"
        path.write_bytes(b"x" * path.stat().st_size)
    elif fault == "wal":
        Path(str(root / "nutriomics.sqlite3") + "-wal").write_bytes(b"page")
    else:
        path = root / "manifest.json"
        manifest = json.loads(path.read_text())
        if fault == "duplicate":
            manifest["files"].append(manifest["files"][0])
        else:
            manifest["files"][0]["path"] = "../private.sqlite3"
        path.write_text(json.dumps(manifest))
        source["expected_manifest_sha256"] = sha256(path)
    with pytest.raises((ValueError, FileNotFoundError)):
        tool.promote(**source)
    assert not source["output"].exists()


def test_late_source_replacement_is_detected_and_partial_staging_retained(source, monkeypatch):
    original = tool._copy_verified
    def copy_then_mutate(path, target, binding):
        original(path, target, binding)
        if path.name == "manifest.json":
            (source["source"] / "effects/result.parquet").write_bytes(b"late-change")
    monkeypatch.setattr(tool, "_copy_verified", copy_then_mutate)
    with pytest.raises(ValueError, match="changed during"):
        tool.promote(**source)
    assert not source["output"].exists()
    assert (source["output"].parent / ".permanent.promoting").is_dir()
    assert json.loads(source["receipt"].read_text())["passed"] is False


def test_receipt_cannot_mutate_either_release(source):
    for folder in [source["source"], source["output"]]:
        with pytest.raises(ValueError, match="receipt must be outside"):
            tool.promote(**{**source, "receipt": folder / "operator.json"})
