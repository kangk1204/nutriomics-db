"""Materialize an exact completed release at a new permanent path, without activation.

Every declared file and the original manifest are copied and fully hash-read
back before an atomic directory rename. The operator receipt is outside both
source and destination, so it cannot change the frozen release manifest.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path

from nutrigenomics.composed_release import _copy_verified, _nonempty_wal
from nutrigenomics.submission_package import SHA, fingerprint, safe_path, sha256


def promote(source, output, expected_manifest_sha256, receipt):
    source, output, receipt = map(Path, (source, output, receipt))
    if source.is_symlink():
        raise ValueError("Release source root may not be a symlink")
    source, output, receipt = source.resolve(), output.resolve(), receipt.resolve()
    staging = output.with_name("." + output.name + ".promoting")
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Use a separate new permanent release directory")
    if receipt.is_relative_to(source) or receipt.is_relative_to(output) or receipt.is_relative_to(staging):
        raise ValueError("Operator receipt must be outside immutable source/destination")
    if output.exists() or staging.exists() or receipt.exists():
        raise FileExistsError("Immutable target, staging or receipt already exists")
    started = time.monotonic()
    manifest_path = safe_path(source, "manifest.json")
    if not SHA.fullmatch(expected_manifest_sha256) or sha256(manifest_path) != expected_manifest_sha256:
        raise ValueError("Pinned completed manifest SHA mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest.get("release_id"), str) or not manifest["release_id"]:
        raise ValueError("Completed release identity is required")
    declared, before = {}, {}
    for entry in manifest["files"]:
        name = entry["path"]
        path = safe_path(source, name)
        if (name == "manifest.json" or name in declared or not path.is_file()
                or type(entry["bytes"]) is not int or entry["bytes"] < 0
                or not SHA.fullmatch(entry["sha256"]) or _nonempty_wal(path)):
            raise ValueError("Invalid, duplicate, missing or unclosed declared release file")
        if path.stat().st_size != entry["bytes"]:
            raise ValueError("Source release is partial or has changed size")
        declared[name] = entry
        before[name] = fingerprint(path)
    if not declared:
        raise ValueError("Completed release has no declared files")
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError("Source release contains a symlink")
        if path.is_file() and path.relative_to(source).as_posix() not in {*declared, "manifest.json"}:
            raise ValueError("Unexpected unmanifested source file")
    if "nutriomics.sqlite3" in declared and declared["nutriomics.sqlite3"]["sha256"] != manifest.get("database_sha256"):
        raise ValueError("Native database SHA differs from file manifest")
    before["manifest.json"] = fingerprint(manifest_path)
    total = sum(r["bytes"] for r in declared.values()) + manifest_path.stat().st_size
    output.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output.parent).free < total + 1024**3:
        raise ValueError("Insufficient permanent disk space")
    staging.mkdir()
    result = {"schema_version": "verified-release-materialization-v1", "release_id": manifest["release_id"],
              "manifest_sha256": expected_manifest_sha256, "passed": False, "activated": False,
              "copied_files": [], "source_fingerprints_before": before,
              "method": "Full source SHA while copying, full target readback SHA, stable source identities, atomic rename",
              "semantic_mutations": False, "publication_readiness_inferred": False}
    receipt.parent.mkdir(parents=True, exist_ok=True)
    try:
        for name, entry in sorted(declared.items()):
            _copy_verified(safe_path(source, name), safe_path(staging, name), entry)
            result["copied_files"].append(dict(entry))
        _copy_verified(manifest_path, staging / "manifest.json", {"bytes": manifest_path.stat().st_size, "sha256": expected_manifest_sha256})
        after = {name: fingerprint(safe_path(source, name)) for name in before}
        result["source_fingerprints_after"] = after
        if before != after or sha256(manifest_path) != expected_manifest_sha256:
            raise ValueError("Completed source changed during materialization")
        if any(_nonempty_wal(safe_path(source, name)) for name in before):
            raise ValueError("Source WAL became nonempty during materialization")
        if output.exists():
            raise FileExistsError("Target appeared before atomic publication")
        os.replace(staging, output)
        result.update(passed=True, files=len(declared) + 1, copied_bytes=total,
                      source_unchanged=True, target_manifest_unchanged=True)
    except BaseException as exc:
        result["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        result["elapsed_seconds"] = time.monotonic() - started
        receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "output", "receipt"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    result = promote(**vars(parser.parse_args()))
    print(json.dumps({"passed": result["passed"], "release_id": result["release_id"], "copied_bytes": result["copied_bytes"]}))


if __name__ == "__main__":
    main()
