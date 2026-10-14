from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from nutrigenomics.settings import DATA_DIR


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_member_path(name: str) -> PurePosixPath:
    if "\\" in name or "\x00" in name:
        raise ValueError(f"Unsafe path syntax in TAR member: {name!r}")
    normalized = name.removesuffix("/")
    raw_parts = normalized.split("/")
    if not normalized or name.startswith("/") or any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError(f"TAR member escapes its extraction directory: {name!r}")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or not path.parts
        or (path.parts[0].endswith(":") or ":" in path.parts[0])
    ):
        raise ValueError(f"TAR member escapes its extraction directory: {name!r}")
    return path


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Download manifest missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {row["name"]: row for row in payload.get("files", []) if row.get("name")}


def extract_accession(
    data_dir: Path, accession: str, max_expanded_gb: float = 100.0
) -> dict[str, Any]:
    if max_expanded_gb <= 0:
        raise ValueError("max_expanded_gb must be positive")
    data_dir = data_dir.expanduser().resolve()
    accession = accession.upper()
    raw_dir = data_dir / "raw" / "geo" / accession
    archive_path = raw_dir / f"{accession}_RAW.tar"
    download_records = _load_manifest(data_dir / "raw" / "geo" / "download_manifest.json")
    source_record = download_records.get(archive_path.name)
    if not archive_path.is_file() or not source_record:
        raise FileNotFoundError(f"Verified raw TAR not found in download manifest: {archive_path}")
    archive_sha = _sha256(archive_path)
    if archive_sha != source_record.get("sha256"):
        raise ValueError(f"Raw TAR checksum mismatch for {archive_path.name}")

    output_root = data_dir / "processed" / "geo" / accession / "raw_files"
    manifest_path = output_root / "extraction_manifest.json"
    if manifest_path.exists():
        old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if old_manifest.get("source_sha256") == archive_sha:
            for item in old_manifest.get("files", []):
                relative = _safe_member_path(item["path"])
                extracted = (output_root / Path(*relative.parts)).resolve()
                if not extracted.is_relative_to(output_root.resolve()):
                    raise ValueError(
                        "Extraction manifest contains a path outside the extraction directory"
                    )
                if not extracted.is_file() or _sha256(extracted) != item["sha256"]:
                    raise ValueError(
                        f"Previously extracted file is missing or changed: {extracted}"
                    )
            return {
                "accession": accession,
                "status": "already_verified",
                "file_count": len(old_manifest.get("files", [])),
                "manifest": str(manifest_path),
            }
        raise FileExistsError(
            f"Existing extraction manifest belongs to another archive: {manifest_path}"
        )
    if output_root.exists():
        raise FileExistsError(
            f"Preserving existing extraction directory without a manifest: {output_root}"
        )

    with tarfile.open(archive_path, mode="r:") as archive:
        members = archive.getmembers()
        validated: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
        expanded_bytes = 0
        destinations: set[str] = set()
        for member in members:
            if member.isdir():
                _safe_member_path(member.name)
                continue
            if not member.isfile():
                raise ValueError(
                    f"Refusing non-regular TAR member {member.name!r} ({member.type!r})"
                )
            relative = _safe_member_path(member.name)
            key = relative.as_posix()
            if key in destinations:
                raise ValueError(f"Duplicate TAR member path: {key}")
            destinations.add(key)
            if member.size < 0:
                raise ValueError(f"Negative TAR member size: {key}")
            expanded_bytes += member.size
            if expanded_bytes > max_expanded_gb * 1024**3:
                raise ValueError(f"Expanded TAR size exceeds {max_expanded_gb:g} GiB")
            validated.append((member, relative))

        output_root.parent.mkdir(parents=True, exist_ok=True)
        staging_root = Path(tempfile.mkdtemp(prefix=".raw_files.staging-", dir=output_root.parent))
        extracted: list[dict[str, Any]] = []
        try:
            for member, relative in validated:
                destination = (staging_root / Path(*relative.parts)).resolve()
                if not destination.is_relative_to(staging_root.resolve()):
                    raise ValueError(
                        f"TAR member resolves outside extraction root: {member.name!r}"
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise FileExistsError(
                        f"Preserving existing untracked extraction file: {destination}"
                    )
                temporary = destination.with_name(destination.name + ".part")
                source = archive.extractfile(member)
                if source is None:
                    raise OSError(f"Could not read regular TAR member: {member.name}")
                digest = hashlib.sha256()
                size = 0
                try:
                    with source, temporary.open("xb") as sink:
                        while True:
                            block = source.read(1024 * 1024)
                            if not block:
                                break
                            sink.write(block)
                            digest.update(block)
                            size += len(block)
                    if size != member.size:
                        raise OSError(
                            f"Extracted size mismatch for {member.name}: {size} != {member.size}"
                        )
                    os.replace(temporary, destination)
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
                extracted.append(
                    {"path": relative.as_posix(), "bytes": size, "sha256": digest.hexdigest()}
                )
            manifest = {
                "schema_version": "1",
                "accession": accession,
                "source_archive": str(archive_path),
                "source_sha256": archive_sha,
                "extracted_utc": datetime.now(UTC).isoformat(),
                "file_count": len(extracted),
                "expanded_bytes": sum(item["bytes"] for item in extracted),
                "files": extracted,
            }
            (staging_root / "extraction_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            os.replace(staging_root, output_root)
        except BaseException:
            shutil.rmtree(staging_root)
            raise
    return {
        "accession": accession,
        "status": "extracted",
        "file_count": len(extracted),
        "manifest": str(manifest_path),
    }


def extract_archives(
    data_dir: Path = DATA_DIR, accessions: list[str] | None = None, max_expanded_gb: float = 100.0
) -> dict[str, Any]:
    manifest = _load_manifest(
        data_dir.expanduser().resolve() / "raw" / "geo" / "download_manifest.json"
    )
    available = sorted(
        record["accession"]
        for record in manifest.values()
        if record.get("kind") in {"raw_array_archive", "raw_methylation_archive"}
    )
    selected = (
        sorted({item.upper() for item in accessions}) if accessions else sorted(set(available))
    )
    results = [extract_accession(data_dir, accession, max_expanded_gb) for accession in selected]
    return {"results": results, "count": len(results)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Safely extract checksum-verified GEO raw TAR archives"
    )
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--accession", action="append", dest="accessions")
    parser.add_argument("--max-expanded-gb", type=float, default=100.0)
    args = parser.parse_args()
    print(
        json.dumps(
            extract_archives(args.data_dir, args.accessions, args.max_expanded_gb),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
