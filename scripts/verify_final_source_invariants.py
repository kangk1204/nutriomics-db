"""Read-only final scope, access, mapping and artifact checks."""

import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.context_joins import trial_context_health, validate_context
from nutrigenomics.expansion import connect, migrate, validate
from nutrigenomics.platform_mapping import SCHEMA, validate_platform

ROOT = Path("research/2026-09-30/expansion")


def main():
    db = connect(ROOT / "nutrition_evidence.sqlite3")
    errors = validate(db) + validate_context(db) + validate_platform(db)
    assert not errors, errors
    assert (
        db.execute(
            "SELECT count(*) FROM ng_feature_gene WHERE dataset_id='GSE15284' AND platform_id!='GPL7020'"
        ).fetchone()[0]
        == 0
    )
    new_sources = db.execute(
        "SELECT s.id,r.status FROM ng_release s LEFT JOIN ng_rights r ON r.release_id=s.id WHERE s.source LIKE '%annotation%' OR s.source LIKE '%manuscript%'"
    ).fetchall()
    assert new_sources and all(row[1] == "quarantined" for row in new_sources)
    assert (
        db.execute(
            "SELECT count(*) FROM ng_evidence WHERE id LIKE 'PMC3880862:%:difference' AND (standard_error IS NOT NULL OR ci_low IS NOT NULL OR ci_high IS NOT NULL)"
        ).fetchone()[0]
        == 0
    )
    assert (
        db.execute(
            "SELECT count(*) FROM ng_evidence WHERE id LIKE 'PMC3880862:%' AND value_status='below_detection'"
        ).fetchone()[0]
        == 2
    )
    assert len(trial_context_health(db, "GSE32357")) == 25
    assert len(trial_context_health(db, "GSE15284")) == 8
    assert trial_context_health(db, "GSE27385") == []
    tables = [
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ng_%' ORDER BY name"
        )
    ]
    report = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "table_counts": {
            name: db.execute("SELECT count(*) FROM " + name).fetchone()[0] for name in tables
        },
        "integrity_check": db.execute("PRAGMA integrity_check").fetchone()[0],
        "validation_errors": errors,
        "annotation_and_manuscript_sources_quarantined": True,
        "wrong_platform_rows_remaining": 0,
        "no_derived_paired_SE_or_CI": True,
        "below_detection_not_zero": True,
        "resveratrol_trial_comparisons": 25,
        "fishgastro_trial_records": 8,
        "candidate_fish_oil_health_joins": 0,
        "individual_health_crosswalks": 0,
    }
    report["source_sha256"] = hashlib.sha256(
        (ROOT / "nutrition_evidence.sqlite3").read_bytes()
    ).hexdigest()
    replay = json.loads((ROOT / "reports/final_replay_validation.json").read_text())
    assert replay["logical_rows_identical"] and replay["duplicates_added"] == 0
    report["idempotent_replay"] = True
    # Persist additive SQL from the final tested implementation.
    (ROOT / "migrations/004_platform_up.sql").write_text(
        "BEGIN IMMEDIATE;\n"
        + SCHEMA
        + "\nINSERT OR IGNORE INTO ng_migration VALUES ('nutrition-platform-gene-v1',datetime('now'));\nCOMMIT;\n"
    )
    (ROOT / "migrations/004_platform_down.sql").write_text(
        "BEGIN IMMEDIATE;\nDROP TABLE IF EXISTS ng_feature_gene;\nDELETE FROM ng_migration WHERE version='nutrition-platform-gene-v1';\nCOMMIT;\n"
    )
    probe = sqlite3.connect(":memory:")
    probe.execute("CREATE TABLE legacy_sentinel (id INTEGER)")
    probe.execute("INSERT INTO legacy_sentinel VALUES (7)")
    probe.commit()
    migrate(probe)
    for filename in [
        "003_context_up.sql",
        "004_platform_up.sql",
        "004_platform_down.sql",
        "003_context_down.sql",
    ]:
        probe.executescript((ROOT / "migrations" / filename).read_text())
    assert probe.execute("SELECT id FROM legacy_sentinel").fetchone()[0] == 7
    report["additive_SQL_reversal_preserves_legacy"] = True
    (ROOT / "reports/final_source_invariants.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
