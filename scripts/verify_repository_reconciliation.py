"""Compare bridge rebuilds and verify the preserved pre-NMDR checkpoint and unchanged export."""

import hashlib
import json
import sqlite3
from pathlib import Path

ROOT = Path("research/2026-09-30/expansion")
CAP = ROOT / "repository-reconciliation"


def table_hashes(path):
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        tables = [
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ]
        return {
            table: hashlib.sha256(
                json.dumps(
                    sorted(db.execute("SELECT * FROM " + table).fetchall()), ensure_ascii=False
                ).encode()
            ).hexdigest()
            for table in tables
        }


if __name__ == "__main__":
    first = CAP / "quarantined_food_affinity_v2.sqlite3"
    replay = CAP / "quarantined_food_affinity_replay.sqlite3"
    assert table_hashes(first) == table_hashes(replay)
    expected = {
        "shareable_food_composition_v7.sqlite3": "93807ccbad5729ab2d0a991e47a9094309aad6981fedf4872d408a19236d7918",
    }
    hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in expected}
    assert hashes == expected
    baseline = ROOT / 'before-st001257.sqlite3'
    with sqlite3.connect(baseline.resolve().as_uri()+'?mode=ro', uri=True) as previous:
        assert previous.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert previous.execute('SELECT COUNT(*) FROM ng_evidence').fetchone()[0]==453
        assert previous.execute('SELECT COUNT(*) FROM ng_dataset').fetchone()[0]==19
    # SQLite backup may rewrite file headers: compare legacy rows in the delivery verifier,
    # rather than pretending a backup must have the original file's byte hash.
    with sqlite3.connect(first) as db:
        species = dict(db.execute("SELECT species,COUNT(*) FROM affinity GROUP BY species"))
        verified = db.execute("SELECT SUM(primary_record_verified) FROM affinity").fetchone()[0]
        assert verified == 0
        food_zeros = db.execute(
            "SELECT COUNT(*) FROM food_composition WHERE status='reported_zero_detection_status_unknown'"
        ).fetchone()[0]
    result = {
        "pre_st001257_backup_sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
        "logical_rebuild_identical": True,
        "checkpoint_hashes_unchanged": hashes,
        "affinity_records_by_species": species,
        "primary_verified_affinity_records": verified,
        "reported_food_zeros_not_assumed_below_detection": food_zeros,
        "table_hashes": table_hashes(first),
    }
    (CAP / "replay_validation.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
