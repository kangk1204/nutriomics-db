"""Recover only explicit public research artifacts, code and new model weights."""

import io
import json
from pathlib import Path
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature.db import digest, dumps, now
    from nutri_literature.extraction_v6 import source_manifest

    folders = [
        "runtime/v6/refinement-r1/joint-relation",
        "runtime/v6/refinement-r1/clinical-roles",
        "runtime/v6/refinement-r1/development",
        "runtime/v6/refinement-r2/boundary",
        "runtime/v6/refinement-r2/development",
        "runtime/v6/refinement-r3/development",
        "runtime/v6/refinement-guards-final/development",
        "runtime/v6/training-data/recovery/inference-official-booleans/train-audited",
        "runtime/v6/training-data/recovery/inference-official-booleans/development-confirmed",
        "runtime/v6/training-data/recovery/relation-evidence-audited",
        "runtime/public_benchmarks/bioredirect/404a04bf9fb316caa061e9ab",
    ]
    files = [
        "requirements-v6.lock",
        "docs/v6-precision-ico-refinement-20261015.md",
        "nutri_literature/refinement_v6.py",
        "nutri_literature/clinical_refinement_v6.py",
        "nutri_literature/clinical_boundary_v6.py",
        "nutri_literature/clinical_grounding_v6.py",
        "nutri_literature/decision_guard_v6.py",
        "scripts/train_v6_refinement.py",
        "scripts/evaluate_v6_refinement.py",
        "scripts/evaluate_v6_clinical_boundary.py",
        "scripts/evaluate_v6_clinical_grounding.py",
        "scripts/evaluate_v6_guards.py",
        "scripts/run_v6_boundary_pipeline.py",
        "scripts/verify_v6_refinement_outputs.py",
        "scripts/package_v6_refinement_results.py",
        "tests/test_refinement_v6.py",
        "tests/test_clinical_boundary_v6.py",
        "tests/test_clinical_grounding_v6.py",
        "tests/test_decision_guard_v6.py",
        "runtime/v6/validation/relation-refinement-training-export.json.gz",
        "runtime/v6/validation/clinical-baseline-for-r1.json",
        "runtime/v6/validation/clinical-evidence-development-predictions-20261015.tar.gz",
        "runtime/v6/validation/bioredirect-public-release-transfer.json",
        "runtime/v6/validation/refinement-r1-relations-auto.json",
        "runtime/v6/validation/refinement-r1-relations-oracle.json",
        "runtime/v6/validation/refinement-r1-clinical.json",
        "runtime/v6/validation/refinement-r2-clinical.json",
        "runtime/v6/validation/refinement-r3-clinical.json",
        "runtime/v6/validation/refinement-guarded-clinical-final.json",
        "runtime/v6/validation/refinement-final-verification.json",
        "runtime/v6/validation/refinement-causal-regression-receipt.json",
        "runtime/v6/validation/refinement-clinical-loss-diagnostics.json",
        "runtime/v6/validation/refinement-id-loss-diagnostics.json",
        "runtime/v6/validation/refinement-grounding-linux.xml",
        "runtime/v6/validation/refinement-guarded-extraction-smoke.json",
        "runtime/v6/validation/refinement-trained-model-manifests.json",
        "runtime/v6/validation/v6-precision-ico-refinement-20261015.json",
        "runtime/v6/validation/refinement-task-state.json",
    ]
    selected = {ROOT / name for name in files}
    for folder in folders:
        selected.update(path for path in (ROOT / folder).rglob("*") if path.is_file())
    out = ROOT / "dist/v6-refinement-results-20261015.tar.gz"
    out.parent.mkdir(exist_ok=True)
    if out.exists():
        raise ValueError("Recovery archive must be new; do not overwrite prior exports")
    receipt = {
        "schema": "v6-refinement-recovery-1",
        "at": now(),
        "files": {},
        "frozen_v6_sources": source_manifest()[1],
        "requires_existing_v6_environment": True,
        "frozen_encoder_weights_included": False,
        "database_or_credentials_included": False,
        "default_engine": "heuristic-v1",
        "sealed_test_opened": False,
    }
    with tarfile.open(out, "w:gz") as archive:
        for path in sorted(selected):
            if not path.is_file() or not path.resolve().is_relative_to(ROOT.resolve()):
                raise ValueError("Unexpected research artifact path: " + str(path))
            name = path.relative_to(ROOT).as_posix()
            raw = path.read_bytes()
            record = tarfile.TarInfo(name)
            record.size, record.mode = len(raw), 0o600
            archive.addfile(record, io.BytesIO(raw))
            receipt["files"][name] = {"sha256": digest(raw), "bytes": len(raw)}
        raw = dumps(receipt).encode()
        record = tarfile.TarInfo("recovery-manifest.json")
        record.size, record.mode = len(raw), 0o600
        archive.addfile(record, io.BytesIO(raw))
    manifest = {
        "archive": out.name,
        "sha256": digest(out.read_bytes()),
        "bytes": out.stat().st_size,
        "files": len(receipt["files"]),
        "inner_manifest_sha256": digest(raw),
        "frozen_v6_sources": receipt["frozen_v6_sources"],
    }
    (out.parent / "v6-refinement-results-20261015.manifest.json").write_text(dumps(manifest))
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
