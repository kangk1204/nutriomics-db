"""Deliver only explicit source paths and fixed v6 input assets; never credentials/DB."""

import json
from pathlib import Path
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature.db import digest, dumps
    from nutri_literature.extraction_v6 import source_manifest

    out = ROOT / "dist"
    out.mkdir(exist_ok=True)
    files = []
    allowed = {".py", ".json", ".md", ".toml", ".lock", ".sh", ".conf", ".css", ".txt"}
    for folder in ("nutri_literature", "tests", "ui", "scripts", "config", "docs"):
        files.extend(
            p
            for p in (ROOT / folder).rglob("*")
            if p.is_file() and p.suffix in allowed and "__pycache__" not in p.parts
        )
    files.extend(
        p
        for p in (
            ROOT / "pyproject.toml",
            ROOT / "README.md",
            ROOT / "requirements-v5.lock",
            ROOT / "requirements-v6.lock",
        )
        if p.exists()
    )
    with tarfile.open(out / "source-v6-current.tar.gz", "w:gz") as archive:
        for path in sorted(set(files)):
            archive.add(path, arcname=path.relative_to(ROOT).as_posix(), recursive=False)
    preparation = json.loads((ROOT / "runtime/v6/data-preparation.json").read_bytes())
    data = ROOT / preparation["audited_directory"]
    asset_paths = [
        data / "audit.json",
        data / "relation-train.json",
        ROOT / "runtime/v6/data-preparation.json",
        ROOT / "runtime/v6/models/mesh-snapshot.json",
        ROOT / "runtime/v6/models/mesh" / preparation["dictionary"]["filename"],
    ]
    with tarfile.open(out / "inputs-v6-current.tar.gz", "w:gz") as archive:
        for path in asset_paths:
            archive.add(path, arcname=path.relative_to(ROOT).as_posix(), recursive=False)
    hashes, rules = source_manifest()
    manifest = {
        "rules_version": rules,
        "rule_source_hashes": hashes,
        "data_preparation": preparation,
        "archives": {
            p.name: {"sha256": digest(p.read_bytes()), "bytes": p.stat().st_size}
            for p in (out / "source-v6-current.tar.gz", out / "inputs-v6-current.tar.gz")
        },
        "default_engine": "heuristic-v1",
        "package_scope": "source-and-initial-input-assets",
        "archive_contains_trained_model": False,
        "sealed_final_opened": False,
        "seven_day_operation": False,
    }
    results = ROOT / "runtime/v6/validation/v6-development-results-20261015.json"
    if results.exists():
        manifest["separate_runtime_results"] = {
            "path": results.relative_to(ROOT).as_posix(),
            "sha256": digest(results.read_bytes()),
            "included_in_archives": False,
        }
    (out / "delivery-v6.json").write_text(dumps(manifest), encoding="utf-8")
    print(dumps({"rules_version": rules, "archives": manifest["archives"]}))


if __name__ == "__main__":
    main()
