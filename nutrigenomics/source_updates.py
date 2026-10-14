"""Content-addressed incremental downloads and reversible immutable releases.

No scheduler, credentials, remote publication or arbitrary shell commands. A
changed source creates a new object/receipt; it never overwrites an old release.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from .explorer_build import canonical, sha256


@contextmanager
def writer_lock(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".update.lock"
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(
            fd,
            canonical({"pid": os.getpid(), "started_at": datetime.now(UTC).isoformat()}).encode(),
        )
        os.close(fd)
        fd = None
        yield
    finally:
        if fd is not None:
            os.close(fd)
        lock.unlink(missing_ok=True)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".part")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(canonical(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _url(url, hosts):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in hosts
        or parsed.username
        or parsed.password
    ):
        raise ValueError("source URL outside explicit HTTPS host registry")


def fetch_source(source, root, session=None):
    """Conditional fetch, validator-bound Range resume and hash verification."""
    root = Path(root)
    source_id = source["id"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,80}", source_id):
        raise ValueError("invalid source ID")
    hosts = set(source["allowed_hosts"])
    _url(source["url"], hosts)
    max_bytes = int(source.get("max_bytes", 100_000_000))
    if not 0 < max_bytes <= 10_000_000_000:
        raise ValueError("invalid source byte budget")
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < max_bytes + 1_000_000_000:
        raise ValueError("insufficient disk reserve")
    receipts = root / "receipts" / source_id
    receipts.mkdir(parents=True, exist_ok=True)
    index = receipts / "latest.json"
    prior = json.loads(index.read_text(encoding="utf-8")) if index.exists() else None
    if prior and (
        not Path(prior["object_path"]).is_file()
        or sha256(Path(prior["object_path"])) != prior["sha256"]
    ):
        raise ValueError("prior object hash mismatch; preserve and investigate")
    partial = receipts / "download.part"
    state_path = receipts / "partial.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    headers = {"Accept-Encoding": "identity", "User-Agent": "NutriOmicsResearch/2.0 source-update"}
    offset = partial.stat().st_size if partial.exists() else 0
    resume = offset > 0 and state.get("url") == source["url"] and state.get("validator")
    if resume:
        headers.update(Range=f"bytes={offset}-", **{"If-Range": state["validator"]})
    elif prior and prior.get("url") == source["url"]:
        if prior.get("etag"):
            headers["If-None-Match"] = prior["etag"]
        elif prior.get("last_modified"):
            headers["If-Modified-Since"] = prior["last_modified"]
    client = session or requests.Session()
    response, url = None, source["url"]
    try:
        for _ in range(5):
            response = client.get(
                url, headers=headers, stream=True, allow_redirects=False, timeout=(10, 45)
            )
            if not response.is_redirect:
                break
            next_url = urljoin(url, response.headers["Location"])
            _url(next_url, hosts)
            response.close()
            url = next_url
        if response is None or response.is_redirect:
            raise ValueError("redirect budget exceeded")
        with response:
            if response.status_code == 304:
                if not prior:
                    raise ValueError("304 without a verified cached object")
                return {**prior, "update_status": "unchanged"}
            response.raise_for_status()
            etag = response.headers.get("ETag")
            # A weak ETag does not guarantee byte identity for If-Range.
            validator = (
                etag
                if etag and not etag.startswith("W/")
                else response.headers.get("Last-Modified")
            )
            mode = "wb"
            if response.status_code == 206:
                cr = response.headers.get("Content-Range", "")
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", cr)
                if (
                    not resume
                    or not match
                    or int(match[1]) != offset
                    or int(match[2]) < offset
                    or int(match[3]) != int(match[2]) + 1
                    or validator != state["validator"]
                ):
                    raise ValueError("invalid or unverified Range response")
                mode = "ab"
            else:
                offset = 0
            length = int(response.headers.get("Content-Length", "0"))
            if response.status_code == 206 and length and length != int(match[2]) - offset + 1:
                raise ValueError("Range length differs from Content-Length")
            if offset + length > max_bytes:
                raise ValueError("source byte budget exceeded")
            atomic_json(state_path, {"url": source["url"], "validator": validator})
            size = offset
            with partial.open(mode) as handle:
                for chunk in response.iter_content(1024 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("stream byte budget exceeded")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if length and size != offset + length:
                raise ValueError("incomplete response body; retain resumable partial")
            with partial.open("rb") as handle:
                prefix = handle.read(20000).lower()
            if any(
                token in prefix
                for token in (b"captcha", b"checking your browser", b"challengepage")
            ):
                raise ValueError("browser challenge received; no bypass")
            if source.get("format") == "gzip" and not prefix.startswith(b"\x1f\x8b"):
                raise ValueError("expected gzip source")
            if source.get("format") == "xlsx" and not prefix.startswith(b"pk"):
                raise ValueError("expected XLSX source")
            digest = sha256(partial)
            expected = source.get("expected_sha256")
            if expected and expected != digest:
                raise ValueError("source checksum mismatch")
            objects = root / "objects"
            objects.mkdir(exist_ok=True)
            destination = objects / digest
            if destination.exists():
                if sha256(destination) != digest:
                    raise ValueError("content-addressed object corrupt")
                partial.unlink()
            else:
                os.replace(partial, destination)
            result = {
                "source_id": source_id,
                "url": source["url"],
                "final_url": url,
                "sha256": digest,
                "bytes": size,
                "object_path": str(destination.resolve()),
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
                "retrieved_at": datetime.now(UTC).isoformat(),
                "terms_url": source.get("terms_url"),
                "export_allowed": source.get("export_allowed") is True,
                "update_status": "unchanged"
                if prior and prior["sha256"] == digest
                else "new_version",
            }
            atomic_json(receipts / (digest + ".json"), result)
            atomic_json(index, result)
            state_path.unlink(missing_ok=True)
            return result
    finally:
        if session is None:
            client.close()


def validate_release(release):
    release = Path(release)
    manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
    if sha256(release / "explorer.sqlite") != manifest["database_sha256"]:
        raise ValueError("release database hash mismatch")
    with sqlite3.connect(
        (release / "explorer.sqlite").resolve().as_uri() + "?mode=ro", uri=True
    ) as db:
        if (
            db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or db.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise ValueError("release SQLite integrity failure")
    return manifest


def promote_release(release, root):
    """Project-local atomic symlink swap; previous version remains restorable."""
    root = Path(root).resolve()
    release = Path(release).resolve()
    if not release.is_relative_to(root / "releases"):
        raise ValueError("release must be inside the permanent project release directory")
    with writer_lock(root):
        manifest = validate_release(release)
        pointer = root / "current"
        if pointer.exists() and not pointer.is_symlink():
            raise ValueError("refuse replacement of a physical directory")
        previous = str(pointer.resolve()) if pointer.is_symlink() else None
        temporary = root / (".current." + uuid.uuid4().hex)
        try:
            temporary.symlink_to(release, target_is_directory=True)
            os.replace(temporary, pointer)
        finally:
            if temporary.is_symlink():
                temporary.unlink()
        result = {
            "release_id": manifest["release_id"],
            "previous": previous,
            "current": str(release),
            "switched_at": datetime.now(UTC).isoformat(),
        }
        atomic_json(root / "history" / (uuid.uuid4().hex + ".json"), result)
        return result
