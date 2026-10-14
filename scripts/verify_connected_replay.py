"""Logical snapshot equality across all offline ingestors; no source downloads."""

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path("research/2026-09-30/expansion")


def snapshot():
    db = sqlite3.connect(
        (ROOT / "nutrition_evidence.sqlite3").resolve().as_uri() + "?mode=ro", uri=True
    )
    tables = [
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ng_%' ORDER BY name"
        )
    ]
    result = {}
    for table in tables:
        digest = hashlib.sha256()
        columns = [row[1] for row in db.execute("PRAGMA table_info(" + table + ")")]
        rows = db.execute("SELECT * FROM " + table + " ORDER BY " + ",".join(columns))
        count = 0
        for row in rows:
            digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode())
            count += 1
        result[table] = {"rows": count, "logical_sha256": digest.hexdigest()}
    db.close()
    return result


def main():
    before = snapshot()
    scripts = [
        "import_context_definitions.py",
        "import_trial_context.py",
        "import_fishgastro_secondary.py",
        "import_analyte_references.py",
        "import_gse32357_primary.py",
        "import_resveratrol_trial_context.py",
        "import_official_platform_mapping.py",
        "import_resveratrol_quantitative.py",
        "import_st001257_primary.py",
    ]
    for script in scripts:
        result = subprocess.run(
            [sys.executable, "scripts/" + script], capture_output=True, text=True, check=False
        )
        if result.returncode:
            raise ValueError(script + ": " + result.stderr[-2000:])
    after = snapshot()
    if before != after:
        raise ValueError(
            "Reingestion changed logical rows: "
            + ",".join(name for name in after if before.get(name) != after[name])
        )
    report = {
        "all_offline_ingestors_replayed": scripts,
        "logical_rows_identical": True,
        "duplicates_added": 0,
        "tables": after,
    }
    (ROOT / "reports/final_replay_validation.json").write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                "logical_rows_identical": True,
                "duplicates_added": 0,
                "tables": {key: row["rows"] for key, row in after.items()},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
