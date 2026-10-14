"""Synthetic ASGI/file interleavings; no live client or clinical evaluation."""
from __future__ import annotations

import gc
import hashlib
import os
from pathlib import Path

import anyio
import pytest
import test_analysis_download_integrity as integrity_fixtures
from nutrigenomics import analysis_downloads, api

CONTENT = integrity_fixtures.CONTENT
client_for = integrity_fixtures.client_for
artifact = integrity_fixtures.artifact


@pytest.fixture
def snapshots(tmp_path, monkeypatch):
    created = []
    original = analysis_downloads.mkstemp

    def create(*args, **kwargs):
        kwargs["dir"] = tmp_path
        fd, name = original(*args, **kwargs)
        created.append(Path(name))
        return fd, name

    monkeypatch.setattr(analysis_downloads, "mkstemp", create)
    return created


def registered(content):
    return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def dispatch(response, send, *, headers=(), extensions=None, method="GET"):
    async def receive():
        return {"type": "http.disconnect"}

    async def run():
        await response({"type": "http", "method": method, "headers": headers,
                        "extensions": extensions or {}}, receive, send)

    anyio.run(run)


def test_route_rejects_other_study_version_and_traversal_before_copy(artifact, snapshots):
    path, session, _ = artifact
    requests = [("GSEOTHER", "synthetic-v1", path.name),
                ("GSE000001", "other-v1", path.name)]
    requests += [("GSE000001", "synthetic-v1", name) for name in
                 ["../sample_qc.tsv", "..\\sample_qc.tsv", ".", "..", str(path)]]
    for accession, version, filename in requests:
        with pytest.raises(api.HTTPException) as error:
            api.download_analysis_auxiliary_file(accession, version, filename, session)
        assert error.value.status_code == 404
    assert snapshots == []


@pytest.mark.parametrize("headers,status", [([], 200), ([(b"range", b"bytes=0-5")], 206),
                                          ([(b"range", b"bytes=0-2,8-10")], 206)])
def test_head_cleans_snapshot_without_body(artifact, snapshots, headers, status):
    path, _, run = artifact
    response = analysis_downloads.verified_analysis_response(path, path.name, run.output_manifest_json[0])
    events = []

    async def send(event):
        events.append(event)

    dispatch(response, send, method="HEAD", headers=headers)
    assert events[0]["status"] == status
    assert events[-1]["body"] == b""
    assert snapshots and all(not path.exists() for path in snapshots)


@pytest.mark.parametrize("header,status", [(b"nonsense", 400), (b"bytes=999-1000", 416)])
def test_range_error_also_cleans_snapshot(artifact, snapshots, header, status):
    path, _, run = artifact
    response = analysis_downloads.verified_analysis_response(path, path.name, run.output_manifest_json[0])
    events = []

    async def send(event):
        events.append(event)

    dispatch(response, send, headers=[(b"range", header)])
    assert events[0]["status"] == status
    assert snapshots and all(not path.exists() for path in snapshots)


def test_send_disconnect_error_cleans_snapshot(tmp_path, snapshots):
    content = b"S" * (128 * 1024)
    path = tmp_path / "large.tsv"
    path.write_bytes(content)
    response = analysis_downloads.verified_analysis_response(path, path.name, registered(content))

    async def send(event):
        if event["type"] == "http.response.body":
            raise BrokenPipeError("Synthetic peer disconnect")

    with pytest.raises(BrokenPipeError, match="Synthetic peer"):
        dispatch(response, send)
    assert snapshots and all(not path.exists() for path in snapshots)


@pytest.mark.parametrize("headers", [[], [(b"range", b"bytes=0-65535")],
                                     [(b"range", b"bytes=0-65535,70000-80000")]])
def test_cancelled_transfer_closes_file_and_cleans_snapshot(tmp_path, snapshots, headers):
    content = b"C" * (128 * 1024)
    path = tmp_path / "cancel.tsv"
    path.write_bytes(content)
    response = analysis_downloads.verified_analysis_response(path, path.name, registered(content))
    bodies = 0

    async def run():
        async def receive():
            return {"type": "http.disconnect"}

        with anyio.CancelScope() as cancellation:
            async def send(event):
                nonlocal bodies
                if event["type"] == "http.response.body":
                    bodies += 1
                    cancellation.cancel()
                    await anyio.lowlevel.checkpoint()

            await response({"type": "http", "method": "GET", "headers": headers}, receive, send)
        assert cancellation.cancel_called

    anyio.run(run)
    assert bodies == 1
    assert snapshots and all(not path.exists() for path in snapshots)


def test_multipart_ranges_keep_payload_headers_and_cleanup(artifact, snapshots):
    path, _, run = artifact
    response = analysis_downloads.verified_analysis_response(path, path.name, run.output_manifest_json[0])
    events = []

    async def send(event):
        events.append(event)

    dispatch(response, send, headers=[(b"range", b"bytes=0-2,8-10")])
    assert events[0]["status"] == 206
    headers = dict(events[0]["headers"])
    body = b"".join(event.get("body", b"") for event in events)
    assert b"multipart/byteranges" in headers[b"content-type"]
    assert len(body) == int(headers[b"content-length"])
    assert f"Content-Range: bytes 0-2/{len(CONTENT)}".encode() in body
    assert f"Content-Range: bytes 8-10/{len(CONTENT)}".encode() in body
    assert b"\r\n\r\n" + CONTENT[:3] + b"\r\n" in body
    assert b"\r\n\r\n" + CONTENT[8:11] + b"\r\n" in body
    assert snapshots and all(not path.exists() for path in snapshots)


def test_pathsend_extension_does_not_outlive_private_snapshot(artifact, snapshots):
    path, _, run = artifact
    response = analysis_downloads.verified_analysis_response(path, path.name, run.output_manifest_json[0])
    events = []

    async def send(event):
        # A server may consume a delegated path after this await returns.
        events.append(event)

    dispatch(response, send, extensions={"http.response.pathsend": {}})
    assert not any(event["type"] == "http.response.pathsend" for event in events)
    assert b"".join(event.get("body", b"") for event in events) == CONTENT
    assert snapshots and all(not path.exists() for path in snapshots)


def test_undispatched_response_finalizer_cleans_snapshot(artifact, snapshots):
    path, _, run = artifact
    response = analysis_downloads.verified_analysis_response(path, path.name, run.output_manifest_json[0])
    assert all(path.exists() for path in snapshots)
    del response
    gc.collect()
    assert snapshots and all(not path.exists() for path in snapshots)


def test_source_open_failure_returns_503_and_cleans_snapshot(artifact, snapshots, monkeypatch):
    path, _, _ = artifact
    original = Path.open

    def open_file(self, *args, **kwargs):
        if self == path and args == ("rb",):
            raise FileNotFoundError(str(path))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    with client_for(artifact) as client:
        result = client.get("/synthetic-download")
    assert result.status_code == 503
    assert result.json()["detail"] == "Analysis output unavailable"
    assert str(path) not in result.text
    assert snapshots and all(not path.exists() for path in snapshots)


def test_copy_write_failure_returns_503_and_cleans_snapshot(artifact, snapshots, monkeypatch):
    original = os.fdopen

    class FullDisk:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def write(self, data):
            self.file.write(data[:1])
            raise OSError(28, "Synthetic disk full")

    def open_target(fd, mode):
        return FullDisk(original(fd, mode))

    monkeypatch.setattr(analysis_downloads.os, "fdopen", open_target)
    with client_for(artifact) as client:
        result = client.get("/synthetic-download")
    assert result.status_code == 503
    assert result.json()["detail"] == "Analysis output unavailable"
    assert snapshots and all(not path.exists() for path in snapshots)


@pytest.mark.parametrize("same_bytes", [False, True])
def test_source_replaced_between_route_check_and_open(artifact, snapshots, monkeypatch, same_bytes):
    path, _, _ = artifact
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(CONTENT if same_bytes else b"X" * len(CONTENT))
    original = Path.open

    def open_file(self, *args, **kwargs):
        if self == path and args == ("rb",) and replacement.exists():
            replacement.replace(path)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    with client_for(artifact) as client:
        result = client.get("/synthetic-download")
    assert result.status_code == (200 if same_bytes else 409)
    if same_bytes:
        assert result.content == CONTENT
    assert snapshots and all(not path.exists() for path in snapshots)


def test_source_changes_mid_copy_are_rejected(tmp_path, snapshots, monkeypatch):
    chunk = analysis_downloads._CHUNK_BYTES
    content = b"A" * (chunk * 2)
    path = tmp_path / "interleaved.tsv"
    path.write_bytes(content)
    original = Path.open
    reads = []

    class Interleaved:
        def __init__(self, source):
            self.source = source

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.source.close()

        def read(self, amount):
            reads.append(amount)
            data = self.source.read(amount)
            if len(reads) == 1:
                with original(path, "r+b") as writer:
                    writer.seek(chunk)
                    writer.write(b"B" * chunk)
            return data

    def open_file(self, *args, **kwargs):
        source = original(self, *args, **kwargs)
        return Interleaved(source) if self == path and args == ("rb",) else source

    monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(ValueError, match="differs"):
        analysis_downloads.verified_analysis_response(path, path.name, registered(content))
    assert max(reads) <= chunk
    assert snapshots and all(not path.exists() for path in snapshots)


def test_copy_and_response_chunk_bounds_and_snapshot_size(tmp_path, snapshots, monkeypatch):
    chunk = analysis_downloads._CHUNK_BYTES
    content = b"B" * (chunk * 2 + 37)
    path = tmp_path / "bounded.tsv"
    path.write_bytes(content)
    original = Path.open
    reads = []

    class Measured:
        def __init__(self, source):
            self.source = source

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.source.close()

        def read(self, amount):
            reads.append(amount)
            return self.source.read(amount)

    def open_file(self, *args, **kwargs):
        source = original(self, *args, **kwargs)
        return Measured(source) if self == path and args == ("rb",) else source

    monkeypatch.setattr(Path, "open", open_file)
    response = analysis_downloads.verified_analysis_response(path, path.name, registered(content))
    assert snapshots[0].stat().st_size == len(content)
    assert max(reads) <= chunk
    digest = hashlib.sha256()
    sent = []

    async def send(event):
        if event["type"] == "http.response.body":
            body = event["body"]
            sent.append(len(body))
            digest.update(body)

    dispatch(response, send)
    assert max(sent) <= response.chunk_size
    assert sum(sent) == len(content)
    assert digest.hexdigest() == registered(content)["sha256"]
    assert all(not path.exists() for path in snapshots)


def test_oversized_source_reads_only_manifest_plus_one(tmp_path, snapshots, monkeypatch):
    expected = b"S" * 7
    path = tmp_path / "oversized.tsv"
    path.write_bytes(expected + b"extra" * 10000)
    original = Path.open
    reads = []

    class Measured:
        def __init__(self, source):
            self.source = source

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.source.close()

        def read(self, amount):
            data = self.source.read(amount)
            reads.append((amount, len(data)))
            return data

    def open_file(self, *args, **kwargs):
        source = original(self, *args, **kwargs)
        return Measured(source) if self == path and args == ("rb",) else source

    monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(ValueError, match="differs"):
        analysis_downloads.verified_analysis_response(path, path.name, registered(expected))
    assert reads == [(len(expected) + 1, len(expected) + 1)]
    assert snapshots and all(not path.exists() for path in snapshots)
