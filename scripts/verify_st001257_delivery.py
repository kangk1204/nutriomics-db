"""Validate dataset joins, legacy negatives and reversible optional measurement schema."""

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import rollback
from nutrigenomics.metabolomics import migrate_metabolomics

ROOT = Path("research/2026-09-30/expansion")
if __name__ == "__main__":
    path = ROOT / "nutrition_evidence.sqlite3"
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as source:
        assert source.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not source.execute("PRAGMA foreign_key_check").fetchall()
        n_samples = source.execute(
            "SELECT COUNT(*) FROM ng_molecular_sample WHERE dataset_id='ST001257'"
        ).fetchone()[0]
        n_people = source.execute(
            "SELECT COUNT(DISTINCT participant_key) FROM ng_molecular_sample WHERE dataset_id='ST001257' AND sample_type='urine'"
        ).fetchone()[0]
        missing = source.execute(
            "SELECT SUM(missing_count) FROM ng_molecular_sample WHERE dataset_id='ST001257'"
        ).fetchone()[0]
        observed = source.execute("SELECT COUNT(*) FROM ng_molecular_measurement").fetchone()[0]
        assert (n_samples, n_people, missing, observed) == (90, 19, 904465, 147905)
        paths = source.execute(
            "SELECT COUNT(*) FROM ng_food_urine_feature_path WHERE dataset_id='ST001257'"
        ).fetchone()[0]
        assert paths == 352635
        assert (
            source.execute(
                "SELECT COUNT(*) FROM ng_mapping WHERE from_id LIKE 'NMDR:AN002086:%' OR to_id LIKE 'NMDR:AN002086:%'"
            ).fetchone()[0]
            == 0
        )
        assert (
            source.execute(
                "SELECT COUNT(*) FROM ng_evidence WHERE study_id='N15' AND layer='health_outcome'"
            ).fetchone()[0]
            == 0
        )
        assert (
            source.execute(
                "SELECT COUNT(*) FROM ng_molecular_sample WHERE dataset_id='ST001257' AND sample_type='food' AND participant_key IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
        assert (
            source.execute("SELECT COUNT(*) FROM ng_evidence WHERE layer='composition'").fetchone()[
                0
            ]
            == 332
        )
        assert (
            source.execute(
                "SELECT COUNT(*) FROM ng_evidence WHERE q_value<0.05 AND layer='health_outcome'"
            ).fetchone()[0]
            == 0
        )
        before = source.execute(
            "SELECT metadata_json FROM ng_dataset WHERE accession='GSE27385'"
        ).fetchone()[0]
        previous = sqlite3.connect(
            (ROOT / "before-st001257.sqlite3").resolve().as_uri() + "?mode=ro", uri=True
        )
        assert (
            before
            == previous.execute(
                "SELECT metadata_json FROM ng_dataset WHERE accession='GSE27385'"
            ).fetchone()[0]
        )
        for table in ["ng_evidence", "ng_mapping", "ng_study", "ng_dataset"]:
            old = previous.execute("SELECT * FROM " + table + " ORDER BY id").fetchall()
            placeholders = ",".join("?" for _ in old)
            current = source.execute(
                "SELECT * FROM " + table + " WHERE id IN (" + placeholders + ") ORDER BY id",
                [r[0] for r in old],
            ).fetchall()
            assert old == current, "Legacy row changed: " + table
        previous.close()
        # In-memory clone: exercise the SQL down/up path without touching the real database.
        clone = sqlite3.connect(":memory:")
        source.backup(clone)
        clone.execute("PRAGMA foreign_keys=ON")
        clone.execute("CREATE TABLE legacy_guard(id INTEGER PRIMARY KEY, value TEXT)")
        clone.execute("INSERT INTO legacy_guard VALUES(1,'preserved')")
        clone.commit()
        clone.executescript((ROOT / "migrations/006_metabolomics_down.sql").read_text())
        migrate_metabolomics(clone)
        assert clone.execute("SELECT COUNT(*) FROM ng_molecular_measurement").fetchone()[0] == 0
        rollback(clone)
        assert clone.execute("SELECT value FROM legacy_guard").fetchone()[0] == "preserved"
        clone.close()
        table_counts = {
            r[0]: source.execute("SELECT COUNT(*) FROM " + r[0]).fetchone()[0]
            for r in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ng_%'"
            )
        }
    export = ROOT / "shareable_food_composition_v7.sqlite3"
    export_hash = hashlib.sha256(export.read_bytes()).hexdigest()
    assert export_hash == "93807ccbad5729ab2d0a991e47a9094309aad6981fedf4872d408a19236d7918"
    report = {
        "primary_dataset": "ST001257",
        "samples": n_samples,
        "people": n_people,
        "observed": observed,
        "explicit_missing": missing,
        "shared_anonymous_feature_paths": paths,
        "named_molecule_paths": 0,
        "health_effects_added": 0,
        "legacy_rows_preserved": True,
        "down_up_and_full_rollback_preserve_legacy": True,
        "table_counts": table_counts,
        "source_database_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "shareable_v7_unchanged_sha256": export_hash,
    }
    (ROOT / "reports/st001257_delivery_validation.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report, indent=2))
