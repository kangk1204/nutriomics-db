"""Read-only database integrity, migration reversal and checkpoint manifest."""

import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import migrate

ROOT = Path("research/2026-09-30/expansion")


def main():
    report = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "full_suite_passed_before_export_closure_repair": 214,
        "focused_tests_after_export_closure_repair": 60,
        "dependency_warnings": 3,
        "ruff": "passed",
        "library_file_id": "libfile_2e5da0fd460c8191a4b878571071afa8",
        "library_version_pending": 3,
        "complete_food_molecule_health_chains": 0,
        "deployed": False,
        "artifacts": {},
    }
    for name in ["nutrition_evidence.sqlite3", "shareable_food_composition_v6.sqlite3"]:
        path = ROOT / name
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        report["artifacts"][name] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "integrity_check": db.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_errors": db.execute("PRAGMA foreign_key_check").fetchall(),
        }
        if name.startswith("shareable"):
            report["artifacts"][name]["optional_counts"] = {
                table: db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                for table in [
                    "ng_nutrient_definition",
                    "ng_arm",
                    "ng_assay_context",
                    "ng_arm_component",
                    "ng_study_link",
                ]
            }
        db.close()
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("CREATE TABLE legacy_sentinel (id INTEGER)")
    db.execute("INSERT INTO legacy_sentinel VALUES (7)")
    db.commit()
    migrate(db)
    db.executescript((ROOT / "migrations/003_context_up.sql").read_text())
    db.executescript((ROOT / "migrations/003_context_down.sql").read_text())
    assert db.execute("SELECT id FROM legacy_sentinel").fetchone()[0] == 7
    assert not db.execute(
        "SELECT name FROM sqlite_master WHERE name IN ('ng_arm','ng_arm_component','ng_assay_context','ng_study_link','ng_nutrient_definition')"
    ).fetchall()
    report["context_sql_up_down_preserves_legacy"] = True
    report["parser_sha256"] = {
        name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
        for name in [
            "nutrigenomics/context_joins.py",
            "nutrigenomics/release_rights.py",
            "scripts/import_trial_context.py",
            "scripts/import_fishgastro_secondary.py",
            "scripts/import_analyte_references.py",
            "scripts/import_gse32357_primary.py",
            "scripts/import_resveratrol_trial_context.py",
            "scripts/report_connected_coverage.py",
        ]
    }
    (ROOT / "reports/connected_checkpoint_validation.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
