"""Serve only the bytes registered for an analysis, from a private snapshot."""
from __future__ import annotations

import hashlib
import os
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import token_hex
from tempfile import mkstemp

import anyio
from fastapi.responses import FileResponse
from starlette.datastructures import MutableHeaders

from .download_budget import _OwnedSnapshot, reserve_temporary_copy

_CHUNK_BYTES = 1024 * 1024


class _VerifiedFileResponse(FileResponse):
    """Retain FileResponse range support and remove the snapshot after delivery."""

    def __init__(self, snapshot: _OwnedSnapshot, filename: str, sha256: str):
        super().__init__(snapshot.path, filename=filename, headers={"etag": f'"{sha256}"'})
        # Also clean up a prepared response which is never dispatched.
        self._snapshot = snapshot
        self._cleanup = weakref.finalize(self, snapshot.cleanup)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            if self._snapshot.cleanup():
                self._cleanup.detach()

    @asynccontextmanager
    async def _snapshot_reader(self):
        file = await anyio.open_file(self.path, mode="rb")
        try:
            yield file
        finally:
            # A cancelled scope must still close the Windows handle before unlink.
            with anyio.CancelScope(shield=True):
                await file.aclose()

    async def _handle_simple(self, send, send_header_only, send_pathsend):
        await send({"type": "http.response.start", "status": self.status_code,
                    "headers": self.raw_headers})
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        # Temporary paths cannot be delegated to a server for later consumption.
        async with self._snapshot_reader() as file:
            more_body = True
            while more_body:
                chunk = await file.read(self.chunk_size)
                more_body = len(chunk) == self.chunk_size
                await send({"type": "http.response.body", "body": chunk, "more_body": more_body})

    async def _handle_single_range(self, send, start, end, file_size, send_header_only):
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-range"] = f"bytes {start}-{end - 1}/{file_size}"
        headers["content-length"] = str(end - start)
        await send({"type": "http.response.start", "status": 206, "headers": headers.raw})
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        async with self._snapshot_reader() as file:
            await file.seek(start)
            more_body = True
            while more_body:
                chunk = await file.read(min(self.chunk_size, end - start))
                start += len(chunk)
                more_body = len(chunk) == self.chunk_size and start < end
                await send({"type": "http.response.body", "body": chunk, "more_body": more_body})

    async def _handle_multiple_ranges(self, send, ranges, file_size, send_header_only):
        boundary = token_hex(13)
        content_length, header_generator = self.generate_multipart(
            ranges, boundary, file_size, self.headers["content-type"],
        )
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-type"] = f"multipart/byteranges; boundary={boundary}"
        headers["content-length"] = str(content_length)
        await send({"type": "http.response.start", "status": 206, "headers": headers.raw})
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        async with self._snapshot_reader() as file:
            for start, end in ranges:
                await send({"type": "http.response.body", "body": header_generator(start, end),
                            "more_body": True})
                await file.seek(start)
                while start < end:
                    chunk = await file.read(min(self.chunk_size, end - start))
                    start += len(chunk)
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
                await send({"type": "http.response.body", "body": b"\r\n", "more_body": True})
            await send({"type": "http.response.body", "body": f"--{boundary}--".encode("latin-1"),
                        "more_body": False})


def verified_analysis_response(path: Path, filename: str, manifest: dict) -> FileResponse:
    """Reject absent/stale provenance before any HTTP body; bound copy memory.

    A separate temporary snapshot avoids re-opening a changed analysis output
    when the ASGI response is dispatched later. It is not a new analysis result.
    """
    expected_hash = manifest.get("sha256")
    expected_bytes = manifest.get("bytes")
    if (
        not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in expected_hash)
        or not isinstance(expected_bytes, int)
        or isinstance(expected_bytes, bool)
        or expected_bytes < 0
    ):
        raise ValueError("Analysis output provenance is missing or invalid")
    expected_hash = expected_hash.lower()
    reservation = reserve_temporary_copy(expected_bytes)
    fd = None
    snapshot = None
    owned = None
    try:
        fd, name = mkstemp(prefix="nutrigenomics-verified-output-", suffix=".snapshot")
        snapshot = Path(name)
        owned = _OwnedSnapshot(snapshot, reservation, os.fstat(fd))
        size = 0
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as target:
            fd = None  # The context now owns and closes this handle.
            with path.open("rb") as source:
                # Stop at the first extra byte rather than copying a growing file.
                while block := source.read(min(_CHUNK_BYTES, expected_bytes - size + 1)):
                    size += len(block)
                    if size > expected_bytes:
                        raise ValueError("Analysis output differs from its registered manifest")
                    digest.update(block)
                    target.write(block)
        if size != expected_bytes or digest.hexdigest() != expected_hash:
            raise ValueError("Analysis output differs from its registered manifest")
        return _VerifiedFileResponse(owned, filename, expected_hash)
    except BaseException:
        try:
            if fd is not None:
                os.close(fd)
        finally:
            if owned is not None:
                owned.cleanup()
            elif snapshot is None:
                reservation.release()
            # If metadata/ownership capture itself failed after creation, retain
            # credit and do not delete an unverified path. No automatic janitor.
        raise


def analysis_auxiliary_response(accession, analysis_version, filename, session, data_dir):
    """Shared path/manifest checks for the existing auxiliary-file route."""
    from fastapi import HTTPException
    from sqlalchemy import select

    from .models import AnalysisRun

    if Path(filename).name != filename or filename in {".", ".."}:
        raise HTTPException(status_code=404, detail="Analysis output not found")
    run = session.scalar(
        select(AnalysisRun).where(
            AnalysisRun.series_accession == accession.upper(),
            AnalysisRun.analysis_version == analysis_version,
        )
    )
    auxiliary_files = (run.metadata_json or {}).get("auxiliary_files", []) if run else []
    if isinstance(auxiliary_files, str):
        auxiliary_files = [auxiliary_files]
    if not isinstance(auxiliary_files, list):
        auxiliary_files = []
    if filename not in auxiliary_files:
        raise HTTPException(status_code=404, detail="Analysis output not found")
    manifest_record = next(
        (
            item
            for item in (run.output_manifest_json or [])
            if isinstance(item, dict)
            and Path(str(item.get("path", ""))).name == filename
        ),
        None,
    )
    if manifest_record is None:
        raise HTTPException(status_code=404, detail="Analysis output not found")
    output_path = Path(str(manifest_record.get("path", ""))).resolve()
    output_root = (Path(data_dir).resolve().parent / "outputs").resolve()
    if output_root not in output_path.parents or not output_path.is_file():
        raise HTTPException(status_code=404, detail="Analysis output not found")
    try:
        return verified_analysis_response(output_path, filename, manifest_record)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=503, detail="Analysis output unavailable") from error

