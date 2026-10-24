"""Read the owned job/test evidence and production health without running models."""

import json
from pathlib import Path
import sys
import sqlite3
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    if str(ROOT) != "/home/keunsoo/projects/nutriomics-literature-v6-development":
        raise ValueError("Only the v6 Linux development checkout may collect this receipt")
    from nutri_literature.extraction_v6 import source_manifest
    from nutri_literature.db import digest
    import psutil
    import requests

    def read(name):
        path = ROOT / "runtime" / name
        return json.loads(path.read_bytes()) if path.exists() else None

    record = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "rules_version": source_manifest()[1],
        "training": read("v6/training-state.json"),
        "development": read("v6/development-state.json"),
        "provider_smoke": read("v6/validation/provider-smoke.json"),
        "encoder_smoke": read("v6/validation/encoder-smoke.json"),
        "ui_smoke": read("v6/validation/ui-smoke.json"),
    }
    manifest = read("v6/models/clinical/manifest.json")
    if manifest:
        record["clinical_base"] = {
            "training_articles": {key: len(values) for key, values in manifest["training_pmids"].items()},
            "manifest_sha256": digest((ROOT / "runtime/v6/models/clinical/manifest.json").read_bytes()),
        }
    try:
        record["production_health"] = requests.get("http://127.0.0.1:8010/healthz", timeout=3).json()
        with sqlite3.connect(
            "file:/home/keunsoo/projects/nutriomics-literature/runtime/data/literature.sqlite3?mode=ro",
            uri=True,
        ) as connection:
            record["production_papers"] = connection.execute("SELECT count(*) FROM papers").fetchone()[0]
    except (requests.RequestException, sqlite3.Error):
        record["production_health_read"] = "unavailable"
    launch = read("v6/training-launch.json")
    record["training_processes"] = []
    if launch:
        try:
            parent = psutil.Process(launch["pid"])
            if "scripts/run_v6_training.py" in parent.cmdline():
                record["training_processes"] = [
                    {
                        "pid": child.pid,
                        "rss_bytes": child.memory_info().rss,
                        "vms_bytes": child.memory_info().vms,
                    }
                    for child in parent.children()
                ]
        except psutil.Error:
            pass
    for filename in ("linux-tests-release.xml", "linux-v6-final.xml"):
        path = ROOT / "runtime/v6/validation" / filename
        if path.exists():
            record[filename + "_sha256"] = digest(path.read_bytes())
    destination = ROOT / "runtime/v6/validation/server-receipt.json"
    destination.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    print(
        json.dumps(
            {
                key: record.get(key)
                for key in (
                    "observed_at",
                    "rules_version",
                    "training",
                    "development",
                    "training_processes",
                    "production_papers",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
