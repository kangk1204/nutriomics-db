"""Attach verification as a separate local audit database; original bridge remains immutable."""

import json
import sqlite3
from pathlib import Path

ROOT = Path("research/2026-09-30/expansion/repository-reconciliation")
if __name__ == "__main__":
    audits = json.loads((ROOT / "primary-affinity/verification.json").read_text())
    if len(audits) != 24 or not all(row.get("record_match") for row in audits):
        raise ValueError("Incomplete primary-provider verification")
    path = ROOT / "primary-affinity/primary_record_audit_v3.sqlite3"
    if path.exists():
        raise ValueError("Preserve existing audit checkpoint")
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE record_audit(
      record TEXT PRIMARY KEY, provider_record_verified INTEGER CHECK(provider_record_verified=1),
      original_paper_verified INTEGER CHECK(original_paper_verified=0),
      experimental_construct_verified INTEGER CHECK(experimental_construct_verified=0),
      origin TEXT NOT NULL, license TEXT NOT NULL, origin_explicitly_confirmed INTEGER,
      kd_nm REAL NOT NULL, reported_uncertainty TEXT, uncertainty_type TEXT,
      ph REAL, temperature_kelvin REAL, assay_page_status TEXT, release_status TEXT,
      verification_json TEXT NOT NULL);
      CREATE TABLE policy(name TEXT PRIMARY KEY, data_json TEXT NOT NULL);""")
    with db:
        for row in audits:
            db.execute(
                "INSERT INTO record_audit VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["record"],
                    1,
                    0,
                    0,
                    row["origin"],
                    row["license"],
                    int(
                        row["origin_confirmation"]
                        == "official_ChEMBL_identifier_in_reaction_record"
                    ),
                    row["expected_kd_nm"],
                    row["official_reported_uncertainty"],
                    row["uncertainty_type"],
                    row["official_ph"],
                    row["official_temperature_kelvin"],
                    row["assay_page_status"],
                    "quarantined",
                    json.dumps(row, sort_keys=True),
                ),
            )
        db.execute(
            "INSERT INTO policy VALUES(?,?)",
            ("rights", (ROOT / "primary-affinity/rights_review.json").read_text()),
        )
    counts = {
        row[0]: row[1]
        for row in db.execute("SELECT license,COUNT(*) FROM record_audit GROUP BY license")
    }
    if counts != {"CC-BY-3.0": 4, "CC-BY-SA-3.0": 20}:
        raise ValueError("Origin/license coverage changed")
    result = {
        "provider_records_verified": 24,
        "original_papers_replicated": 0,
        "licenses": counts,
        "explicit_origin_records": db.execute(
            "SELECT SUM(origin_explicitly_confirmed) FROM record_audit"
        ).fetchone()[0],
        "recovered_numeric_uncertainty_rows": db.execute(
            "SELECT COUNT(*) FROM record_audit WHERE reported_uncertainty!='n/a'"
        ).fetchone()[0],
        "integrity": db.execute("PRAGMA integrity_check").fetchone()[0],
        "release_status": "quarantined",
    }
    db.close()
    (ROOT / "primary-affinity/audit_validation_v3.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(json.dumps(result))
