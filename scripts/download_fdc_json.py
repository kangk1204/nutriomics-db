from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import zipfile

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from nutrigenomics.settings import DATA_DIR


DOWNLOADS = [
    {
        "name": "FoodData_Central_foundation_food_json_2026-04-30.zip",
        "release": "Foundation Foods 2026-04-30",
        "url": "https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_foundation_food_json_2026-04-30.zip",
    },
    {
        "name": "FoodData_Central_survey_food_json_2024-10-31.zip",
        "release": "FNDDS 2021-2023 (2024-10-31)",
        "url": "https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_survey_food_json_2024-10-31.zip",
    },
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(data_dir: Path = DATA_DIR) -> dict:
    folder = data_dir.expanduser().resolve() / "raw" / "fdc"
    folder.mkdir(parents=True, exist_ok=True)
    manifest_path = folder / "download_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"files": []}
    known = {row.get("name"): row for row in manifest.get("files", [])}
    session = requests.Session()
    retry = Retry(total=4, connect=4, read=4, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = "nutrigenomics-research-db/0.1 (USDA FDC public dataset retrieval)"
    output = []
    try:
        for item in DOWNLOADS:
            path = folder / item["name"]
            prior = known.get(item["name"])
            if path.exists():
                if prior and prior.get("sha256") == sha256_file(path):
                    output.append({"name": item["name"], "status": "already_verified", "sha256": prior["sha256"]})
                    continue
                raise FileExistsError(f"Existing file is not verified by the manifest; preserving it: {path}")

            temp = path.with_name(path.name + ".part")
            response = session.get(item["url"], stream=True, timeout=(15, 120))
            response.raise_for_status()
            digest = hashlib.sha256()
            size = 0
            try:
                with temp.open("xb") as handle:
                    for block in response.iter_content(chunk_size=1024 * 1024):
                        if block:
                            handle.write(block)
                            digest.update(block)
                            size += len(block)
                with zipfile.ZipFile(temp) as archive:
                    damaged = archive.testzip()
                    if damaged:
                        raise ValueError(f"Corrupt entry in {item['name']}: {damaged}")
                os.replace(temp, path)
            except BaseException:
                temp.unlink(missing_ok=True)
                raise
            finally:
                response.close()
            record = {
                **item,
                "status": "downloaded",
                "retrieved_utc": datetime.now(timezone.utc).isoformat(),
                "bytes": size,
                "sha256": digest.hexdigest(),
                "relative_path": path.relative_to(data_dir.resolve()).as_posix(),
                "license_note": "USDA FoodData Central source data; retain dataset identity and release attribution.",
            }
            manifest.setdefault("files", [])
            manifest["files"] = [row for row in manifest["files"] if row.get("name") != item["name"]]
            manifest["files"].append(record)
            temp_manifest = manifest_path.with_suffix(".json.tmp")
            temp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temp_manifest, manifest_path)
            output.append({"name": item["name"], "status": "downloaded", "bytes": size, "sha256": record["sha256"]})
    finally:
        session.close()
    return {"manifest": str(manifest_path), "files": output}


def main() -> None:
    parser = argparse.ArgumentParser(description="Download current USDA FDC Foundation and FNDDS JSON snapshots")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = parser.parse_args()
    print(json.dumps(download(args.data_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

