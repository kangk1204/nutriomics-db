"""Acquisition must never silently omit current BioStudies IDATs."""
import io
import json
from unittest.mock import patch

import pytest

from scripts.direct_plus_source import (
    STUDY,
    audit_sdrf_rows,
    resolve_files,
    safe_file_path,
    validate_raw_mapping,
)
from scripts.download_bridge_studies import fetch


def info_and_entries():
    entries = [{"path": f"{STUDY}.{suffix}.txt", "Size": 10,
                "Section": f"mt-{STUDY}"} for suffix in ("sdrf", "idf")]
    entries += [{"path": f"{sample}_{channel}.idat", "Size": 20, "Section": "raw-data"}
                for sample in range(1, 5) for channel in ("Grn", "Red")]
    return {"isPublic": True, "files": len(entries),
            "httpLink": f"https://ftp.ebi.ac.uk/biostudies/fire/E-MTAB-/527/{STUDY}",
            "sectionFileCounts": {f"mt-{STUDY}": 2, "raw-data": 8}}, entries


def pages(entries, total=None):
    def page(offset, limit):
        return {"items": entries[offset:offset+limit], "pagination": {
            "offset": offset, "limit": limit, "total": total or len(entries),
            "filtered": total or len(entries)}}
    return page


def sdrf():
    return [{"Source Name": f"sample{s}", "Characteristics[individual]": str((s+1)//2),
             "Factor Value[time]": "0" if s % 2 else "18", "Factor Value[diet]": "HDG",
             "Array Data File": f"{s}_{channel}.idat"}
            for s in range(1, 5) for channel in ("Grn", "Red")]


def test_live_api_shape_resolves_all_pages_and_pairs_without_legacy_filelist():
    info, entries = info_and_entries()
    resolved = resolve_files(info, pages(entries), page_size=3)
    audit = audit_sdrf_rows(sdrf(), expected_people=2)
    assert validate_raw_mapping(resolved, audit) == {
        "raw_files": 8, "raw_bytes": 160, "metadata_files": 2, "sdrf_channels_complete": True}
    assert all("/Files/" in f["url"] for f in resolved)


@pytest.mark.parametrize("bad", ["../x", "a/../x", "/x", "C:/x", "a\\x", "%2e%2e/x", "https://x", "a//x", "./x", "x?y"])
def test_unsafe_source_paths_rejected(bad):
    with pytest.raises(ValueError):
        safe_file_path(bad)


@pytest.mark.parametrize("failure", ["duplicate", "truncated", "total_drift", "zero_size", "wrong_host"])
def test_listing_never_reports_incomplete_success(failure):
    info, entries = info_and_entries()
    if failure == "duplicate": entries[-1] = entries[0]
    if failure == "truncated": entries.pop()
    if failure == "zero_size": entries[0]["Size"] = 0
    if failure == "wrong_host": info["httpLink"] = "https://example.com/" + STUDY
    with pytest.raises(ValueError):
        resolve_files(info, pages(entries, total=999 if failure == "total_drift" else info["files"]), page_size=3)


@pytest.mark.parametrize("failure", ["duplicate_channel", "arm_change", "missing_channel", "wrong_time", "sample_collision"])
def test_sdrf_cannot_double_count_channels_or_invent_pairing(failure):
    rows = sdrf()
    if failure == "duplicate_channel": rows[-1] = rows[-2].copy()
    if failure == "arm_change": rows[2]["Factor Value[diet]"] = "MED"
    if failure == "missing_channel": rows.pop()
    if failure == "wrong_time": rows[0]["Factor Value[time]"] = "18 months"
    if failure == "sample_collision": rows[2]["Source Name"] = rows[0]["Source Name"]
    with pytest.raises(ValueError): audit_sdrf_rows(rows, expected_people=2)


def test_listing_sdrf_mismatch_is_failure():
    info, entries = info_and_entries()
    resolved = resolve_files(info, pages(entries))
    audit = audit_sdrf_rows(sdrf(), expected_people=2)
    audit["raw_names"][-1] = "unlisted_Red.idat"
    with pytest.raises(ValueError, match="missing/extra"): validate_raw_mapping(resolved, audit)


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data); self.status = status; self.headers = headers or {"Content-Length": str(len(data))}


def test_cached_wrong_size_is_not_success(tmp_path):
    path = tmp_path / "a.idat"; path.write_bytes(b"short")
    assert fetch("https://example.org/a", path, 10, expected_size=10)["status"] == "failed"


def test_same_size_local_corruption_is_not_revalidated(tmp_path):
    path = tmp_path / "a.idat"; path.write_bytes(b"changed")
    result = fetch("https://example.org/a", path, 7, expected_size=7, expected_sha256="0"*64)
    assert result["status"] == "failed" and "SHA256" in result["reason"]


def test_colour_label_disagreement_is_failure():
    rows = sdrf(); rows[0]["Label"] = "Cy5"
    with pytest.raises(ValueError, match="colour"): audit_sdrf_rows(rows, expected_people=2)


def test_source_bound_partial_resumes_exactly_and_hashes_bytes(tmp_path):
    path = tmp_path / "a.idat"
    partial = path.with_suffix(".idat.part"); partial.write_bytes(b"abc")
    partial.with_suffix(".part.json").write_text(json.dumps({
        "url": "https://example.org/a", "expected_size": 6, "source_identity": "A", "etag": '"v1"'}))
    response = Response(b"def", 206, {"Content-Length": "3", "Content-Range": "bytes 3-5/6", "ETag": '"v1"'})
    with patch("urllib.request.urlopen", return_value=response) as request:
        result = fetch("https://example.org/a", path, 6, expected_size=6, source_identity="A")
    assert result["status"] == "downloaded" and path.read_bytes() == b"abcdef"
    assert request.call_args.args[0].headers["Range"] == "bytes=3-"


def test_unbound_partial_is_preserved_but_not_appended(tmp_path):
    path = tmp_path / "a.idat"; path.with_suffix(".idat.part").write_bytes(b"old")
    with patch("urllib.request.urlopen", return_value=Response(b"new")) as request:
        result = fetch("https://example.org/a", path, 3, expected_size=3, source_identity="A")
    assert result["status"] == "downloaded" and "Range" not in request.call_args.args[0].headers
    assert next(tmp_path.glob("*.unbound.*")).read_bytes() == b"old"


def test_truncated_or_wrong_range_cannot_be_success(tmp_path):
    path = tmp_path / "a.idat"
    with patch("urllib.request.urlopen", return_value=Response(b"abc", headers={"Content-Length": "6"})):
        assert fetch("https://example.org/a", path, 6, retries=1, expected_size=6)["status"] == "failed"
    assert not path.exists()


def test_refresh_failure_preserves_original_metadata(tmp_path):
    path = tmp_path / "source.json"; path.write_text('{"original_terms":"retained"}')
    with patch("urllib.request.urlopen", side_effect=OSError("offline")):
        assert fetch("https://example.org/a", path, 100, retries=1, refresh=True)["status"] == "failed"
    assert json.loads(path.read_text())["original_terms"] == "retained"


def test_successful_refresh_archives_previous_original_bytes(tmp_path):
    path = tmp_path / "source.json"; path.write_bytes(b"old original")
    with patch("urllib.request.urlopen", return_value=Response(b"new original")):
        result = fetch("https://example.org/a", path, 100, retries=1, refresh=True)
    assert result["status"] == "downloaded" and path.read_bytes() == b"new original"
    assert next(tmp_path.glob("*.source-*")).read_bytes() == b"old original"


def test_manifest_update_preserves_preexisting_rights_and_unrelated_downloads(tmp_path):
    from scripts import download_bridge_studies as module
    manifest = tmp_path / "download_manifest.json"
    manifest.write_text(json.dumps({"usage": "Original usage condition", "rights": {"export": False},
        "files": [{"url": "https://example.org/unrelated", "status": "downloaded", "license": "restricted"},
                  {"url": "https://example.org/test", "license": "original license", "validation": {"old": True}}]}))
    def acquisition(directory, record, **kwargs):
        record("https://example.org/test", directory / "test", 100)
        return {"status": "metadata_only_raw_not_requested"}
    with patch("sys.argv", ["download", "--root", str(tmp_path), "--studies", STUDY]), \
         patch.object(module, "acquire_direct_plus", acquisition), \
         patch.object(module, "fetch", return_value={"url": "https://example.org/test", "status": "cached_verified"}):
        module.main()
    result = json.loads(manifest.read_text())
    assert result["usage"] == "Original usage condition" and result["rights"] == {"export": False}
    keyed = {r["url"]: r for r in result["files"]}
    assert keyed["https://example.org/test"]["license"] == "original license"
    assert keyed["https://example.org/test"]["validation"] == {"old": True}
    assert keyed["https://example.org/unrelated"]["license"] == "restricted"


@pytest.mark.parametrize("failure_mode", ["failed_record", "raised_record_error", None])
def test_all_raw_files_attempted_after_an_early_failure(tmp_path, failure_mode):
    from urllib.parse import parse_qs, urlsplit

    from scripts import download_bridge_studies as module

    info, entries = info_and_entries()
    attempted = []
    def record(url, path, budget, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.name == "study_metadata.json": payload = {}
        elif path.name.startswith("study_info"): payload = info
        elif path.name.startswith("files_"):
            query = parse_qs(urlsplit(url).query)
            payload = pages(entries)(int(query["offset"][0]), int(query["limit"][0]))
        elif path.suffix == ".idat":
            attempted.append(path.name)
            if path.name == "1_Grn.idat" and failure_mode:
                if failure_mode == "raised_record_error":
                    raise OSError("Simulated invalid remote IDAT")
                return {"url": url, "status": "failed", "reason": "Remote object truncated"}
            path.write_bytes(b"x"*20)
            return {"url": url, "path": str(path), "status": "downloaded", "bytes": 20,
                    "sha256": module.digest(path)}
        else:
            path.write_bytes(b"metadata!!")
            return {"url": url, "status": "downloaded", "bytes": 10}
        path.write_text(json.dumps(payload), encoding="utf-8")
        return {"url": url, "status": "downloaded", "bytes": path.stat().st_size}
    audit = audit_sdrf_rows(sdrf(), expected_people=2)
    with patch.object(module, "direct_plus_audit", return_value=audit):
        if failure_mode:
            with pytest.raises(module.IncompleteDirectPlusAcquisition) as error:
                module.acquire_direct_plus(tmp_path, record, raw=True, raw_budget=1000000, workers=2)
            assert error.value.result["failed_count"] == 1
            assert error.value.result["success_count"] == 7
        else:
            result = module.acquire_direct_plus(tmp_path, record, raw=True, raw_budget=1000000, workers=2)
            assert result["status"] == "raw_complete_local_sha256_verified" and result["complete"] is True
    assert len(attempted) == 8 and "4_Red.idat" in attempted
    attempt = json.loads((tmp_path / "raw_attempt_receipts.json").read_text(encoding="utf-8"))
    assert attempt["attempted_count"] == 8 and attempt["not_attempted_count"] == 0
    assert attempt["expected_raw_files"] == 8 and len(attempt["records"]) == 8
    assert attempt["complete"] is (failure_mode is None)
    if failure_mode:
        assert attempt["failed_names"] == ["1_Grn.idat"]
        assert not (tmp_path / "raw_receipts.json").exists()
        status = json.loads((tmp_path / "acquisition_status.json").read_text(encoding="utf-8"))
        assert status["status"] == "failed_incomplete" and status["success_count"] == 7
        first = next((tmp_path / "raw_attempts").glob("attempt.*.json"))
        original_attempt_bytes = first.read_bytes()
        failure_mode = None
        with patch.object(module, "direct_plus_audit", return_value=audit):
            resumed = module.acquire_direct_plus(tmp_path, record, raw=True, raw_budget=1000000, workers=2)
        assert resumed["complete"] is True and len(attempted) == 16
        assert first.read_bytes() == original_attempt_bytes
        assert len(list((tmp_path / "raw_attempts").glob("attempt.*.json"))) == 2
    else:
        assert len(json.loads((tmp_path / "raw_receipts.json").read_text())) == 8


def test_cli_retains_complete_failed_attempt_accounting(tmp_path):
    from scripts import download_bridge_studies as module

    details = {"expected_raw_files": 1024, "attempted_count": 1024, "success_count": 1023,
               "failed_count": 1, "not_attempted_count": 0, "failed_names": ["151_T18_Grn.idat"],
               "complete": False, "attempt_receipt_sha256": "a"*64}
    with patch("sys.argv", ["download", "--root", str(tmp_path), "--studies", STUDY, "--raw"]), \
         patch.object(module, "acquire_direct_plus", side_effect=module.IncompleteDirectPlusAcquisition(details)), \
         pytest.raises(module.IncompleteDirectPlusAcquisition):
        module.main()
    result = json.loads((tmp_path / "raw" / STUDY / "acquisition_status.json").read_text())
    assert result["status"] == "failed_incomplete" and result["complete"] is False
    for key, value in details.items(): assert result[key] == value
