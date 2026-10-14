import gzip
import io
import urllib.error

from scripts.download_bridge_studies import fetch


def test_corrupt_cached_gzip_is_not_reclassified_as_verified(tmp_path):
    path = tmp_path / "count.txt.gz"
    original = gzip.compress(b"preserved")[:-5]
    path.write_bytes(original)
    result = fetch("https://example.org/count.txt.gz", path, 1000, retries=1)
    assert result["status"] == "failed"
    assert path.read_bytes() == original


def test_cached_file_budget_checked_without_overwriting_source(tmp_path):
    path = tmp_path / "count.txt"
    path.write_bytes(b"large")
    result = fetch("https://example.org/count.txt", path, 3, retries=1)
    assert result["status"] == "failed"
    assert path.read_bytes() == b"large"


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = {"Content-Length": str(len(data)), **(headers or {})}


def test_resume_uses_matching_range(tmp_path, monkeypatch):
    path = tmp_path / "count.txt"
    path.with_suffix(".txt.part").write_bytes(b"ab")

    def open_response(request, timeout):
        assert request.headers["Range"] == "bytes=2-"
        return Response(b"cd", 206, {"Content-Range": "bytes 2-3/4"})

    monkeypatch.setattr("urllib.request.urlopen", open_response)
    result = fetch("https://example.org/count.txt", path, 10, retries=1)
    assert result["status"] == "downloaded"
    assert path.read_bytes() == b"abcd"


def test_server_ignoring_range_restarts_without_duplicate_prefix(tmp_path, monkeypatch):
    path = tmp_path / "count.txt"
    path.with_suffix(".txt.part").write_bytes(b"ab")
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response(b"abcd"))
    assert fetch("https://example.org/count.txt", path, 10, retries=1)["status"] == "downloaded"
    assert path.read_bytes() == b"abcd"


def test_invalid_range_does_not_publish_partial(tmp_path, monkeypatch):
    path = tmp_path / "count.txt"
    path.with_suffix(".txt.part").write_bytes(b"ab")
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response(b"cd", 206, {"Content-Range": "bytes 0-1/4"}))
    assert fetch("https://example.org/count.txt", path, 10, retries=1)["status"] == "failed"
    assert not path.exists()
    assert path.with_suffix(".txt.part").read_bytes() == b"ab"


def test_budget_is_enforced_before_publication(tmp_path, monkeypatch):
    path = tmp_path / "count.txt"
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response(b"abcdef"))
    result = fetch("https://example.org/count.txt", path, 5, retries=1)
    assert result["status"] == "failed"
    assert not path.exists()


def test_corrupt_gzip_never_becomes_verified_file(tmp_path, monkeypatch):
    path = tmp_path / "count.txt.gz"
    data = gzip.compress(b"raw counts")[:-3]
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response(data))
    result = fetch("https://example.org/count.txt.gz", path, 100, retries=1)
    assert result["status"] == "failed"
    assert not path.exists()


def test_unavailable_source_is_recorded_without_retry(tmp_path, monkeypatch):
    calls = []

    def unavailable(request, timeout):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", unavailable)
    result = fetch("https://example.org/count.txt", tmp_path / "count.txt", 100)
    assert result["status"] == "unavailable"
    assert len(calls) == 1
