import json
import zipfile

import pytest

from nutrigenomics.explorer_build import sha256
from scripts.prepare_deposition import prepare


def test_deposit_preparation_preserves_file_scope_and_rejects_changed_bytes(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    table = release / "values.parquet"
    table.write_bytes(b"fixture values")
    (release / "dataset-metadata.json").write_text(json.dumps({
        "release_id": "r", "counts": {"effect": 2}, "scope": "fixture scope"}))
    (release / "manifest.json").write_text(json.dumps({"release_id": "r", "files": [
        {"path": table.name, "bytes": table.stat().st_size, "sha256": sha256(table)}]}))
    code = tmp_path / "code.zip"
    with zipfile.ZipFile(code, "w") as archive:
        archive.writestr("LICENSE", "MIT fixture")
    result = prepare(release, code, tmp_path / "deposit", verify_files=True)
    assert not result["published"]
    draft = json.loads((tmp_path / "deposit/deposit-metadata-draft.json").read_text(encoding="utf-8"))
    assert draft["creators"] is None and draft["data_version_doi"] is None
    assert draft["counts"] == {"effect": 2} and draft["all_data_hashes_verified_now"]
    table.write_bytes(b"changed values")  # Same byte length, changed scientific payload.
    with pytest.raises(ValueError, match="hash mismatch"):
        prepare(release, code, tmp_path / "bad", verify_files=True)

