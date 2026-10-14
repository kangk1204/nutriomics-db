import hashlib
import json
import tarfile

import pytest

from scripts.extract_geo_archives import _safe_member_path, extract_accession


def _make_archive(data_dir, members):
    accession = "GSE000001"
    raw_dir = data_dir / "raw" / "geo" / accession
    raw_dir.mkdir(parents=True)
    archive_path = raw_dir / f"{accession}_RAW.tar"
    with tarfile.open(archive_path, "w") as archive:
        for info, payload in members:
            archive.addfile(info, payload)
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    manifest_dir = data_dir / "raw" / "geo"
    (manifest_dir / "download_manifest.json").write_text(
        json.dumps(
            {
                "files": [
                    {
                        "name": archive_path.name,
                        "accession": accession,
                        "kind": "raw_array_archive",
                        "sha256": digest,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return accession, archive_path


def test_safe_member_path_rejects_traversal_and_windows_absolute():
    for name in ("../escape", "a/../../escape", "/etc/passwd", "C:/windows/file", "a//b", "a\\b"):
        with pytest.raises(ValueError):
            _safe_member_path(name)
    assert _safe_member_path("GSM1/GSM1.CEL.gz").as_posix() == "GSM1/GSM1.CEL.gz"


def test_extraction_is_hash_verified_and_idempotent(tmp_path):
    import io

    payload = b"not real CEL data, only an extraction test"
    info = tarfile.TarInfo("GSM1/GSM1.CEL.gz")
    info.size = len(payload)
    accession, _ = _make_archive(tmp_path, [(info, io.BytesIO(payload))])

    first = extract_accession(tmp_path, accession)
    second = extract_accession(tmp_path, accession)
    extracted = tmp_path / "processed" / "geo" / accession / "raw_files" / "GSM1" / "GSM1.CEL.gz"

    assert first["status"] == "extracted"
    assert second["status"] == "already_verified"
    assert extracted.read_bytes() == payload
    assert second["file_count"] == 1


def test_extraction_rejects_unsafe_member_and_preserves_archive(tmp_path):
    import io

    payload = b"escape"
    info = tarfile.TarInfo("../escape.txt")
    info.size = len(payload)
    accession, archive_path = _make_archive(tmp_path, [(info, io.BytesIO(payload))])

    with pytest.raises(ValueError, match="escapes"):
        extract_accession(tmp_path, accession)
    assert archive_path.is_file()
    assert not (tmp_path / "escape.txt").exists()


def test_extraction_rejects_links(tmp_path):
    accession, archive_path = _make_archive(tmp_path, [])
    archive_path.unlink()
    with tarfile.open(archive_path, "w") as archive:
        link = tarfile.TarInfo("linked.idat")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        archive.addfile(link)
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    (tmp_path / "raw" / "geo" / "download_manifest.json").write_text(
        json.dumps(
            {
                "files": [
                    {
                        "name": archive_path.name,
                        "accession": accession,
                        "kind": "raw_methylation_archive",
                        "sha256": digest,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="non-regular"):
        extract_accession(tmp_path, accession)
    assert archive_path.is_file()
