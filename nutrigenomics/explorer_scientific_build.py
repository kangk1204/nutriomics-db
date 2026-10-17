"""Immutable metadata-only scientific derivative of an already validated Explorer."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .explorer_build import canonical, decode, read_only, sha256, verify_closed_sqlite

TABLES = {
    "ng_exposure": "exposure",
    "ng_arm": "arm",
    "ng_contrast": "contrast",
    "ng_assertion": "assertion",
    "ng_bridge_source": "bridge_source",
    "research_study": "study",
    "analysis_run": "analysis_run",
}


def upgrade_scientific_release(base_dir, core_db, output_dir, release_id, curation_file):
    base_dir, core_db, output_dir, curation_file = map(
        Path, (base_dir, core_db, output_dir, curation_file)
    )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", release_id):
        raise ValueError("invalid release ID")
    if output_dir.exists():
        raise FileExistsError("immutable release already exists")
    manifest = decode((base_dir / "manifest.json").read_text(encoding="utf-8"))
    receipt = decode((base_dir / "verification.json").read_text(encoding="utf-8"))
    stat = core_db.stat()
    expected = receipt["source_stats_after_build"]["core_db"]
    if (
        core_db.resolve() != Path(manifest["source_paths"]["core_db"]).resolve()
        or stat.st_size != expected["bytes"]
        or stat.st_mtime_ns != expected["mtime_ns"]
    ):
        raise ValueError("core fingerprint changed; original hash cannot be reused")
    if sha256(base_dir / "explorer.sqlite") != manifest["database_sha256"]:
        raise ValueError("parent SQLite hash mismatch")
    curation_body = curation_file.read_bytes()
    curation_digest = hashlib.sha256(curation_body).hexdigest()
    curation = decode(curation_body.decode("utf-8"))
    if curation.get("expert_review_status") != "pending" or curation.get("schema_version") != 1:
        raise ValueError("unsupported curation state/schema")
    staging = output_dir.with_name("." + output_dir.name + ".building")
    staging.mkdir(parents=True, exist_ok=False)
    target = sqlite3.connect(staging / "explorer.sqlite")
    target.execute("PRAGMA cache_size=-262144")
    body = Path(__file__).read_bytes()
    counts = {}
    try:
        parent = read_only(base_dir / "explorer.sqlite")
        try:
            parent.backup(target)
        finally:
            parent.close()
        target.executescript(
            "CREATE TABLE science_record(id TEXT NOT NULL,kind TEXT NOT NULL,metadata_json TEXT NOT NULL,PRIMARY KEY(kind,id)); CREATE INDEX science_kind_idx ON science_record(kind);"
        )
        core = read_only(core_db)
        try:
            originals = {}
            for table, kind in TABLES.items():
                rows = core.execute("SELECT * FROM " + table).fetchall()
                counts[kind] = len(rows)
                for row in rows:
                    value = dict(row)
                    for key in list(value):
                        if key.endswith("_json"):
                            value[key.removesuffix("_json")] = decode(value.pop(key), {})
                        elif key in {
                            "exposure_ids",
                            "arm_ids",
                            "component_keys",
                            "food_terms",
                            "limitations",
                        }:
                            value[key] = decode(value[key], [])
                    target.execute(
                        "INSERT INTO science_record VALUES (?,?,?)",
                        (str(row["id"]), kind, canonical(value)),
                    )
                    originals[(kind, str(row["id"]))] = value
            for record in curation.get("exposures", []):
                original = originals.get(("exposure", record["core_exposure_id"]))
                if not original or original["study_id"] != record["study_id"]:
                    raise ValueError("curated exposure key missing or study mismatch")
            for study in curation.get("studies", []):
                if ("study", study["study_id"]) not in originals:
                    raise ValueError("curated study key missing")
                for selector in study.get("contrast_selectors", []):
                    contrast = originals.get(("contrast", selector["id"]))
                    if (
                        not contrast
                        or contrast["study_id"] != study["study_id"]
                        or any(
                            contrast[k] != selector[k]
                            for k in ("series_accession", "contrast", "analysis_version")
                        )
                    ):
                        raise ValueError("curated contrast selector changed")
            target.execute(
                "INSERT INTO science_record VALUES (?,?,?)",
                (curation["version"], "curation", canonical(curation)),
            )
            display_records = {r["core_exposure_id"]: r for r in curation.get("exposures", [])}
            for (kind, identifier), original in originals.items():
                if kind != "exposure":
                    continue
                row = target.execute(
                    "SELECT metadata_json FROM node WHERE id=?", ("Exposure:" + identifier,)
                ).fetchone()
                if not row:
                    continue
                label = display_records.get(identifier, {}).get("display_label")
                if not label and "\ufffd" in original.get("label", ""):
                    label = original.get("formulation")
                if label:
                    metadata = decode(row[0], {})
                    metadata.update(
                        source_original_label=original.get("label"),
                        display_label_curator_created=True,
                        display_label_curation_version=curation["version"],
                    )
                    target.execute(
                        "UPDATE node SET label=?,metadata_json=? WHERE id=?",
                        (label, canonical(metadata), "Exposure:" + identifier),
                    )
        finally:
            core.close()
        if (core_db.stat().st_size, core_db.stat().st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise ValueError("core changed during read-only metadata indexing")
        if sha256(curation_file) != curation_digest:
            raise ValueError("curation changed during indexing")
        manifest["derived_from"] = {
            "release_id": manifest["release_id"],
            "database_sha256": manifest["database_sha256"],
            "manifest_sha256": sha256(base_dir / "manifest.json"),
        }
        manifest.update(
            release_id=release_id,
            created_at=datetime.now(UTC).isoformat(),
            builder_sha256=hashlib.sha256(body).hexdigest(),
            builder_archive="scientific_builder_source.py",
            build_method="Validated SQLite backup + original metadata and source-checked context; no effect-row copies",
            scientific_matching={
                "rule_version": curation["version"],
                "curation_sha256": curation_digest,
                "counts": counts,
                "expert_review_status": "pending",
                "separate_axes": ["mapping_identity", "study_evidence", "applicability"],
                "effect_value_rows_copied": 0,
            },
        )
        manifest.pop("database_sha256", None)
        for key, value in manifest.items():
            target.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, canonical(value)))
        target.commit()
        integrity = [row[0] for row in target.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise ValueError("release integrity failure: " + canonical(integrity[:20]))
        target.close()
        verify_closed_sqlite(staging / "explorer.sqlite")
        manifest["database_sha256"] = sha256(staging / "explorer.sqlite")
        (staging / "manifest.json").write_text(canonical(manifest) + "\n", encoding="utf-8")
        (staging / "scientific_builder_source.py").write_bytes(body)
        (staging / "evidence_curations.json").write_bytes(curation_body)
        coverage = decode((base_dir / "coverage.json").read_text(encoding="utf-8"))
        coverage.update(release_id=release_id, scientific_matching=manifest["scientific_matching"])
        (staging / "coverage.json").write_text(canonical(coverage) + "\n", encoding="utf-8")
        receipt.update(
            release_id=release_id,
            verified_at=manifest["created_at"],
            scientific_matching=manifest["scientific_matching"],
            parent_database_hash_verified=True,
            builder_archive_sha256=manifest["builder_sha256"],
            builder_archive_matches_manifest=True,
        )
        (staging / "verification.json").write_text(canonical(receipt) + "\n", encoding="utf-8")
        os.replace(staging, output_dir)
        return manifest
    except BaseException as error:
        target.close()
        if (
            staging.parent.resolve() == output_dir.parent.resolve()
            and staging.name == "." + output_dir.name + ".building"
        ):
            (staging / "FAILED.json").write_text(canonical({"error": str(error),
                "release_id": release_id, "scope": "Unactivated failed derivative retained for diagnosis"}), encoding="utf-8")
            failed = staging.with_name("." + output_dir.name + ".failed-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f"))
            staging.rename(failed)
            error.add_note("Unactivated diagnostic files: " + str(failed))
        raise
