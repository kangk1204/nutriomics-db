"""Exercise publication gates with real hashed files and an isolated SQLite DB."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from nutrigenomics.db import create_schema, get_session_factory
from nutrigenomics.models import AnalysisRun, GeoSeries, ResearchStudy, StudyEffect
from scripts.import_analysis_results import import_analysis_results

ACCESSION = "GSE56960"
VERSION = "strict-release-test-v1"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_results(path: Path, effect: str = "0.4") -> None:
    headers = (
        "feature_type",
        "feature_id",
        "feature_label",
        "contrast",
        "tissue",
        "timepoint",
        "intervention",
        "dose",
        "duration",
        "effect_estimate",
        "standard_error",
        "confidence_low",
        "confidence_high",
        "p_value",
        "q_value",
        "effect_unit",
        "status",
        "n_people",
        "n_samples",
        "q_value_contrast",
        "q_value_study_family",
    )
    values = (
        "gene",
        "1234",
        "GENE1",
        "dose_NormalWeight_1000_vs500_6h_change",
        "whole blood",
        "6h change difference",
        "mixed meal energy comparison",
        "1000 versus 500 kcal",
        "acute",
        effect,
        "0.1",
        "0.1",
        "0.7",
        "0.002",
        "0.02",
        "log2 fold change",
        "measured",
        "7",
        "168",
        "0.01",
        "0.02",
    )
    # A deterministic gzip makes identical publication retries byte-identical.
    payload = "\t".join(headers) + "\n" + "\t".join(values) + "\n"
    path.write_bytes(gzip.compress(payload.encode(), mtime=0))


def _freeze(bundle: Path) -> None:
    """Create the same completion/approval separation used by the release queue."""
    metadata = _read_json(bundle / "analysis_metadata.json")
    script = bundle / "source_snapshot" / "analysis.R"
    files = []
    for path in sorted(bundle.rglob("*")):
        if path.is_file() and path.name not in {"DONE.json", "RELEASE_APPROVAL.json"}:
            files.append(
                {
                    "path": path.relative_to(bundle).as_posix(),
                    "sha256": _sha(path),
                    "bytes": path.stat().st_size,
                }
            )
    done = {
        "status": "completed",
        "accession": ACCESSION,
        "analysis_version": metadata["analysis_version"],
        "run_utc": metadata["run_utc"],
        "files": files,
        "executed_source_snapshots": [
            {"path": script.relative_to(bundle).as_posix(), "sha256": _sha(script)}
        ],
    }
    _write_json(bundle / "DONE.json", done)
    run_id = hashlib.sha256(
        (
            ACCESSION
            + ":"
            + metadata["analysis_version"]
            + ":"
            + metadata["run_utc"]
            + ":"
            + metadata["script_sha256"]
        ).encode()
    ).hexdigest()
    producer = bundle / "producer_status.json"
    _write_json(
        bundle / "RELEASE_APPROVAL.json",
        {
            "run_id": run_id,
            "completion_sha256": _sha(bundle / "DONE.json"),
            "producer_exit_code": 0,
            "producer_job": "gse56960-dose",
            "producer_status_path": str(producer),
            "producer_status_sha256": _sha(producer),
            "scientific_qc_status": "passed_with_declared_limitations",
            "limitations": ["Synthetic seven-person result; no biological claim"],
        },
    )


@pytest.fixture
def release_bundle(tmp_path: Path) -> tuple[Path, str]:
    bundle = tmp_path / "outputs" / VERSION
    bundle.mkdir(parents=True)
    snapshot = bundle / "source_snapshot"
    snapshot.mkdir()
    script = snapshot / "analysis.R"
    script.write_text("# Frozen executed source\nset.seed(20261001)\n", encoding="utf-8")
    inputs = bundle / "input_manifest.json"
    _write_json(inputs, {"input_hash": "a" * 64, "person_count": 7, "sample_count": 168})
    lock = bundle / "environment.lock"
    lock.write_text("R=4.5.1\nlimma=3.64.3\n", encoding="utf-8")
    _write_results(bundle / "gene_effects.tsv.gz")
    (bundle / "sample_qc.tsv").write_text("sample\tstatus\nGSM1\tpass\n", encoding="utf-8")
    _write_json(
        bundle / "producer_status.json",
        {
            "jobs": [
                {
                    "name": "gse56960-dose",
                    "status": "completed",
                    "returncode": 0,
                    "started_utc": "2026-10-01T00:00:00Z",
                    "finished_utc": "2026-10-01T00:01:00Z",
                    "script": str(script),
                    "script_sha256": _sha(script),
                    "outputs": [bundle.name],
                }
            ],
        },
    )
    _write_json(
        bundle / "analysis_metadata.json",
        {
            "accession": ACCESSION,
            "analysis_version": VERSION,
            "independent_people": 7,
            "sample_count": 168,
            "run_utc": "2026-10-01T00:01:00Z",
            "script_path": str(script),
            "script_sha256": _sha(script),
            "environment_lock_path": str(lock),
            "environment_lock_sha256": _sha(lock),
            "input_manifest": [
                {"path": str(inputs), "sha256": _sha(inputs), "bytes": inputs.stat().st_size}
            ],
            "result_files": ["gene_effects.tsv.gz"],
            "auxiliary_files": ["sample_qc.tsv"],
            "effect_scope": "Group-level mixed-meal response, not an individual prediction",
        },
    )
    _freeze(bundle)
    database_url = f"sqlite:///{(tmp_path / 'release.sqlite3').as_posix()}"
    create_schema(database_url)
    with get_session_factory(database_url)() as session:
        session.add(ResearchStudy(id="S99", title="Release-gate test", description="Synthetic"))
        session.flush()
        session.add(GeoSeries(accession=ACCESSION, study_id="S99", title="Synthetic series"))
        session.commit()
    return bundle, database_url


def _db_snapshot(database_url: str) -> str:
    with get_session_factory(database_url)() as session:
        state = {}
        for model in (GeoSeries, AnalysisRun, StudyEffect):
            state[model.__tablename__] = [
                {column.name: getattr(record, column.name) for column in model.__table__.columns}
                for record in session.scalars(select(model))
            ]
    return json.dumps(state, sort_keys=True, default=str)


def _import(bundle: Path, database_url: str) -> dict[str, int]:
    return import_analysis_results(bundle, database_url, ACCESSION, require_completion=True)


def test_valid_frozen_release_imports_and_identical_retry_is_safe(release_bundle) -> None:
    bundle, database_url = release_bundle
    assert _import(bundle, database_url) == {
        "inserted": 1,
        "updated": 0,
        "result_rows": 1,
        "gene_effects": 1,
    }
    before = _db_snapshot(database_url)
    assert _import(bundle, database_url)["updated"] == 1
    assert _db_snapshot(database_url) == before
    with get_session_factory(database_url)() as session:
        run = session.scalar(select(AnalysisRun))
        effect = session.scalar(select(StudyEffect))
        assert run is not None and effect is not None
        assert run.source_script_sha256 == _sha(bundle / "source_snapshot" / "analysis.R")
        assert (
            run.metadata_json["producer_source_verification"] == "receipt_sha256_and_frozen_source"
        )
        assert run.metadata_json["release_approval"]["scientific_qc_status"] == (
            "passed_with_declared_limitations"
        )
        assert effect.effect_estimate == 0.4
        assert effect.n_people == 7 and effect.n_samples == 168
        assert effect.metadata_json["q_value_study_family"] == "0.02"


def test_signed_scalar_result_and_auxiliary_names_import_without_rewriting_bytes(release_bundle):
    bundle, database_url = release_bundle
    metadata_path = bundle / "analysis_metadata.json"
    metadata = _read_json(metadata_path)
    # jsonlite auto_unbox uses these scalars for DIRECT PLUS's single CpG result.
    metadata["result_files"] = "gene_effects.tsv.gz"
    metadata["auxiliary_files"] = "sample_qc.tsv"
    _write_json(metadata_path, metadata)
    _freeze(bundle)
    signed_bytes = {
        name: (bundle / name).read_bytes()
        for name in ("analysis_metadata.json", "DONE.json", "RELEASE_APPROVAL.json")
    }
    assert _import(bundle, database_url)["inserted"] == 1
    before = _db_snapshot(database_url)
    assert _import(bundle, database_url)["updated"] == 1
    assert _db_snapshot(database_url) == before
    assert all((bundle / name).read_bytes() == original for name, original in signed_bytes.items())
    with get_session_factory(database_url)() as session:
        run = session.scalar(select(AnalysisRun))
        assert run is not None
        assert run.metadata_json["result_files"] == "gene_effects.tsv.gz"
        assert run.metadata_json["auxiliary_files"] == "sample_qc.tsv"
        assert {Path(record["path"]).name for record in run.output_manifest_json} == {
            "gene_effects.tsv.gz",
            "sample_qc.tsv",
            "analysis_metadata.json",
        }


@pytest.mark.parametrize("scalar_field", ["result_files", "auxiliary_files"])
def test_scalar_path_traversal_rejected_under_strict_gate_without_db_changes(
    release_bundle,
    scalar_field,
) -> None:
    bundle, database_url = release_bundle
    before = _db_snapshot(database_url)
    metadata_path = bundle / "analysis_metadata.json"
    metadata = _read_json(metadata_path)
    metadata[scalar_field] = "../outside.tsv.gz"
    _write_json(metadata_path, metadata)
    _freeze(bundle)
    signed_bytes = metadata_path.read_bytes()
    with pytest.raises(ValueError, match="basenames inside the output directory"):
        _import(bundle, database_url)
    assert _db_snapshot(database_url) == before
    assert metadata_path.read_bytes() == signed_bytes


@pytest.mark.parametrize("tampered", ["result", "executed_source", "producer_status"])
def test_tampered_release_rejected_before_existing_db_is_updated(release_bundle, tampered) -> None:
    bundle, database_url = release_bundle
    _import(bundle, database_url)
    before = _db_snapshot(database_url)
    if tampered == "result":
        _write_results(bundle / "gene_effects.tsv.gz", effect="9.9")
    elif tampered == "executed_source":
        (bundle / "source_snapshot" / "analysis.R").write_text("# replacement\n", encoding="utf-8")
    else:
        producer = bundle / "producer_status.json"
        changed = _read_json(producer)
        changed["jobs"][0].update({"status": "failed", "returncode": 1})
        _write_json(producer, changed)
    with pytest.raises(ValueError, match="changed"):
        _import(bundle, database_url)
    assert _db_snapshot(database_url) == before


@pytest.mark.parametrize(
    "receipt_failure",
    [
        "missing_job",
        "duplicate_job",
        "wrong_job",
        "pending",
        "failed_exit",
        "wrong_source",
        "wrong_output",
    ],
)
def test_valid_hashes_and_success_attestation_cannot_override_actual_producer_receipt(
    release_bundle,
    receipt_failure,
) -> None:
    bundle, database_url = release_bundle
    _import(bundle, database_url)
    before = _db_snapshot(database_url)
    producer = bundle / "producer_status.json"
    receipt = _read_json(producer)
    job = receipt["jobs"][0]
    if receipt_failure == "missing_job":
        receipt["jobs"] = []
    elif receipt_failure == "duplicate_job":
        receipt["jobs"].append(dict(job))
    elif receipt_failure == "wrong_job":
        job["name"] = "other-study"
    elif receipt_failure == "pending":
        job["status"] = "running"
    elif receipt_failure == "failed_exit":
        job["returncode"] = 1
    elif receipt_failure == "wrong_source":
        job["script_sha256"] = "f" * 64
    else:
        job["outputs"] = ["unrelated-release"]
    _write_json(producer, receipt)
    # All bytes are frozen again and approval still declares success. The actual
    # job receipt must independently establish this source/output's successful exit.
    _freeze(bundle)
    with pytest.raises(ValueError, match="Producer receipt does not prove"):
        _import(bundle, database_url)
    assert _db_snapshot(database_url) == before


def _write_legacy_receipt(bundle: Path) -> dict:
    producer = bundle / "producer_status.json"
    receipt = _read_json(producer)
    job = receipt["jobs"][0]
    del job["script_sha256"]
    del job["outputs"]
    job["script"] = "analysis.R"
    job["arguments"] = [
        "--root",
        "/home/research/test-project/expansion/2026-10-01",
        "--output-dir",
        "/home/research/test-project/expansion/2026-10-01/outputs/" + bundle.name,
    ]
    _write_json(producer, receipt)
    return receipt


def test_legacy_receipt_imports_with_narrower_provenance_without_rewriting_receipt(
    release_bundle,
) -> None:
    bundle, database_url = release_bundle
    receipt = _write_legacy_receipt(bundle)
    _freeze(bundle)
    original_producer = (bundle / "producer_status.json").read_bytes()
    original_approval = (bundle / "RELEASE_APPROVAL.json").read_bytes()
    assert _import(bundle, database_url)["inserted"] == 1
    assert (bundle / "producer_status.json").read_bytes() == original_producer
    assert (bundle / "RELEASE_APPROVAL.json").read_bytes() == original_approval
    assert "script_sha256" not in receipt["jobs"][0]
    assert "outputs" not in receipt["jobs"][0]
    with get_session_factory(database_url)() as session:
        run = session.scalar(select(AnalysisRun))
        assert run is not None
        assert run.metadata_json["producer_source_verification"] == (
            "receipt_basename_and_frozen_source_sha256"
        )
        assert run.source_script_sha256 == _sha(bundle / "source_snapshot" / "analysis.R")
        assert run.metadata_json["release_approval"]["producer_status_sha256"] == (
            hashlib.sha256(original_producer).hexdigest()
        )


@pytest.mark.parametrize(
    "legacy_failure",
    ["wrong_script", "wrong_output_argument", "missing_output_argument", "contradicting_sha"],
)
def test_legacy_fallback_rejects_wrong_source_output_and_explicit_hash_contradiction(
    release_bundle,
    legacy_failure,
) -> None:
    bundle, database_url = release_bundle
    before = _db_snapshot(database_url)
    receipt = _write_legacy_receipt(bundle)
    job = receipt["jobs"][0]
    if legacy_failure == "wrong_script":
        job["script"] = "different_analysis.R"
    elif legacy_failure == "wrong_output_argument":
        job["arguments"][-1] = "/home/research/test-project/outputs/different-release"
    elif legacy_failure == "missing_output_argument":
        job["arguments"] = job["arguments"][:2]
    else:
        # A valid basename/output cannot override an explicitly disagreeing SHA.
        job["script_sha256"] = "f" * 64
    _write_json(bundle / "producer_status.json", receipt)
    _freeze(bundle)
    with pytest.raises(ValueError, match="Producer receipt does not prove"):
        _import(bundle, database_url)
    assert _db_snapshot(database_url) == before


def test_changed_published_results_rejected_even_with_new_valid_attestation(release_bundle) -> None:
    bundle, database_url = release_bundle
    _import(bundle, database_url)
    before = _db_snapshot(database_url)
    _write_results(bundle / "gene_effects.tsv.gz", effect="9.9")
    # Passing current-file integrity must not bypass immutability of a published version.
    _freeze(bundle)
    with pytest.raises(ValueError, match="version content changed"):
        _import(bundle, database_url)
    assert _db_snapshot(database_url) == before


@pytest.mark.parametrize("required_file", ["gene_effects.tsv.gz", "analysis_metadata.json"])
def test_completion_manifest_must_cover_every_result_and_metadata(release_bundle, required_file):
    bundle, database_url = release_bundle
    before = _db_snapshot(database_url)
    done = _read_json(bundle / "DONE.json")
    done["files"] = [record for record in done["files"] if record["path"] != required_file]
    _write_json(bundle / "DONE.json", done)
    approval = _read_json(bundle / "RELEASE_APPROVAL.json")
    approval["completion_sha256"] = _sha(bundle / "DONE.json")
    _write_json(bundle / "RELEASE_APPROVAL.json", approval)
    with pytest.raises(ValueError, match="QC is not approved"):
        _import(bundle, database_url)
    assert _db_snapshot(database_url) == before


@pytest.mark.parametrize("source_failure", ["missing_snapshot", "wrong_executed_hash"])
def test_executed_source_identity_required_even_when_file_manifest_is_valid(
    release_bundle,
    source_failure,
) -> None:
    bundle, database_url = release_bundle
    before = _db_snapshot(database_url)
    done = _read_json(bundle / "DONE.json")
    if source_failure == "missing_snapshot":
        done["executed_source_snapshots"] = []
    else:
        done["executed_source_snapshots"][0]["sha256"] = "b" * 64
    _write_json(bundle / "DONE.json", done)
    approval = _read_json(bundle / "RELEASE_APPROVAL.json")
    approval["completion_sha256"] = _sha(bundle / "DONE.json")
    _write_json(bundle / "RELEASE_APPROVAL.json", approval)
    with pytest.raises(ValueError, match="Executed source bytes"):
        _import(bundle, database_url)
    assert _db_snapshot(database_url) == before


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("producer_exit_code", 1),
        ("scientific_qc_status", "pending"),
        ("run_id", "wrong-run"),
        ("completion_sha256", "c" * 64),
    ],
)
def test_unapproved_or_wrong_run_release_cannot_create_analysis(
    release_bundle,
    field,
    invalid_value,
) -> None:
    bundle, database_url = release_bundle
    before = _db_snapshot(database_url)
    approval = _read_json(bundle / "RELEASE_APPROVAL.json")
    approval[field] = invalid_value
    _write_json(bundle / "RELEASE_APPROVAL.json", approval)
    with pytest.raises(ValueError, match="QC is not approved"):
        _import(bundle, database_url)
    assert _db_snapshot(database_url) == before


def test_missing_completion_cannot_create_analysis(release_bundle) -> None:
    bundle, database_url = release_bundle
    before = _db_snapshot(database_url)
    (bundle / "DONE.json").unlink()
    with pytest.raises(ValueError, match="requires a DONE"):
        _import(bundle, database_url)
    assert _db_snapshot(database_url) == before


def _supplemental_audit(bundle):
    producer = bundle / "producer_status.json"
    receipt = _read_json(producer)
    receipt["jobs"][0].pop("outputs", None)
    _write_json(producer, receipt)
    _freeze(bundle)
    qc = bundle.parent / "independent_qc.json"
    _write_json(qc, {"status": "passed"})
    evidence = [bundle / "source_snapshot/analysis.R", producer, qc]
    audit = {
        "schema_version": 1, "audit_type": "producer_output_attribution",
        "verification_status": "passed_with_declared_limitations",
        "accession": ACCESSION, "analysis_version": VERSION,
        "original_output_directory": str(bundle),
        "completion_sha256": _sha(bundle / "DONE.json"),
        "metadata_sha256": _sha(bundle / "analysis_metadata.json"),
        "producer_status_path": str(producer), "producer_status_sha256": _sha(producer),
        "producer_job_name": "gse56960-dose", "script_sha256": _sha(evidence[0]),
        "producer_exit_code": 0, "argv_status": "reconstructed_not_recorded",
        "verification_scope": "Frozen R default and independent QC; argv reconstructed",
        "historical_limitations": ["Explicit destination missing in historical receipt"],
        "checks": {"output_destination_verified": True, "data_attribution_verified": True,
                   "independent_qc_verified": True},
        "evidence_files": [{"path": str(f), "sha256": _sha(f), "role": "test_evidence",
                            "capture_timing": "post-run audit"} for f in evidence],
        "independent_qc_evidence": str(qc), "expert_manual_review": "pending",
    }
    path = bundle.parent / "OUTPUT_AUDIT.json"
    _write_json(path, audit)
    return path


def test_supplemental_audit_imports_without_changing_historical_receipts(release_bundle):
    bundle, database_url = release_bundle
    audit = _supplemental_audit(bundle)
    names = ("producer_status.json", "DONE.json", "RELEASE_APPROVAL.json", "analysis_metadata.json")
    originals = {n: (bundle / n).read_bytes() for n in names}
    assert import_analysis_results(bundle, database_url, ACCESSION, True, audit, _sha(audit))["inserted"] == 1
    assert all((bundle / n).read_bytes() == b for n, b in originals.items())
    with get_session_factory(database_url)() as session:
        run = session.scalar(select(AnalysisRun))
        saved = run.metadata_json["producer_output_audit"]
        assert saved["manifest"]["sha256"] == _sha(audit)
        assert saved["audit"]["argv_status"] == "reconstructed_not_recorded"
        assert saved["audit"]["expert_manual_review"] == "pending"


@pytest.mark.parametrize("failure", ["wrong_hash", "wrong_study", "wrong_completion", "truthy_check",
                                     "changed_evidence", "missing_qc_hash", "failed_producer",
                                     "source_mismatch", "contradicting_output", "no_audit"])
def test_supplemental_audit_cannot_bypass_source_status_or_identity(release_bundle, failure):
    bundle, database_url = release_bundle
    path = _supplemental_audit(bundle)
    audit = _read_json(path)
    before = _db_snapshot(database_url)
    if failure == "wrong_study":
        audit["accession"] = "OTHER"
    elif failure == "wrong_completion":
        audit["completion_sha256"] = "f" * 64
    elif failure == "truthy_check":
        audit["checks"]["data_attribution_verified"] = "true"
    elif failure == "changed_evidence":
        Path(audit["independent_qc_evidence"]).write_text("changed")
    elif failure == "missing_qc_hash":
        audit["independent_qc_evidence"] = str(path)
    elif failure in {"failed_producer", "source_mismatch", "contradicting_output"}:
        producer = bundle / "producer_status.json"
        receipt = _read_json(producer)
        job = receipt["jobs"][0]
        if failure == "failed_producer":
            job["returncode"] = 1
        elif failure == "source_mismatch":
            job["script_sha256"] = "f" * 64
        else:
            job["outputs"] = ["unrelated-release"]
        _write_json(producer, receipt)
        _freeze(bundle)
        audit["producer_status_sha256"] = _sha(producer)
        audit["completion_sha256"] = _sha(bundle / "DONE.json")
        for row in audit["evidence_files"]:
            row["sha256"] = _sha(Path(row["path"]))
    _write_json(path, audit)
    with pytest.raises(ValueError):
        if failure == "no_audit":
            _import(bundle, database_url)
        else:
            import_analysis_results(bundle, database_url, ACCESSION, True, path,
                                    "f" * 64 if failure == "wrong_hash" else _sha(path))
    assert _db_snapshot(database_url) == before
