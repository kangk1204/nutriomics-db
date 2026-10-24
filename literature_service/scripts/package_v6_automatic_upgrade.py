"""Explicit recovery bundle; original frozen v6 environment remains required."""
import io
import json
from pathlib import Path
import sys
import tarfile
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature.db import digest, dumps, now
    verified = json.loads((ROOT / "runtime/v6/refinement-r5/final-verification.json").read_bytes())
    assert verified["passed"]
    identity_run = verified["identity_run"]
    folders = ["runtime/v6/refinement-r4/relation-head", "runtime/v6/refinement-r4/relations-end_to_end",
        "runtime/v6/refinement-r4/relations-gold_entities", "runtime/v6/refinement-r4/clinical-development-b",
        identity_run + "/identity-head", identity_run + "/relation-head", identity_run + "/relations-end_to_end",
        "runtime/v6/refinement-r5/clinical-roles-final", "runtime/v6/refinement-r5/clinical-development",
        "runtime/v6/refinement-r6/clinical-development", "runtime/v6/refinement-r7/clinical-development",
        "runtime/v6/refinement-r8/structured-trial-records",
        "runtime/v6/training-data/recovery/relation-evidence-audited",
        "runtime/v6/training-data/recovery/inference-official-booleans/train-audited",
        "runtime/v6/training-data/recovery/inference-official-booleans/development-confirmed",
        "runtime/public_benchmarks/bioredirect/404a04bf9fb316caa061e9ab"]
    modules = ["automatic_features_v6", "automatic_models_v6", "automatic_identity_v6", "fast_frozen_v6",
        "trial_linking_v6", "trial_roles_v6", "trial_recovery_v6", "trial_active_v6", "trial_composition_v6", "trial_records_v6"]
    scripts = ["prepare_v6_automatic_learning", "prepare_v6_automatic_fast", "train_v6_automatic_relations",
        "evaluate_v6_automatic_relations", "evaluate_v6_trial_linking", "evaluate_v6_trial_recovery",
        "evaluate_v6_trial_active", "evaluate_v6_trial_composition", "train_v6_trial_roles",
        "run_v6_automatic_pipeline", "run_v6_identity_pipeline", "run_v6_identity_memoized",
        "status_v6_automatic_upgrade", "diagnose_v6_trial_coverage", "build_v6_trial_records",
        "verify_v6_automatic_upgrade", "summarize_v6_automatic_upgrade", "package_v6_automatic_upgrade"]
    scripts.append("audit_v6_same_criterion")
    tests = ["automatic_learning_v6", "automatic_identity_v6", "trial_linking_v6", "trial_recovery_v6",
             "trial_active_v6", "trial_composition_v6", "trial_records_v6"]
    selected = {ROOT / "nutri_literature" / (name + ".py") for name in modules}
    selected.update(ROOT / "scripts" / (name + ".py") for name in scripts)
    selected.update(ROOT / "tests" / ("test_" + name + ".py") for name in tests)
    aliases = {
        "automatic-noise-verification-20261015.json": "runtime/v6/refinement-r5/final-verification.json",
        "automatic-noise-r4-auto-20261015.json": "runtime/v6/refinement-r4/relations-end_to_end/report.json",
        "automatic-noise-r4-oracle-20261015.json": "runtime/v6/refinement-r4/relations-gold_entities/report.json",
        "automatic-noise-r5-auto-20261015.json": identity_run + "/relations-end_to_end/report.json",
        "automatic-noise-clinical-20261015.json": "runtime/v6/refinement-r7/clinical-development/report.json",
        "automatic-noise-linux-20261015.xml": "runtime/v6/refinement-r4/linux-upgrade-final-tests.xml",
        "automatic-noise-same-criterion-20261015.json": "runtime/v6/refinement-r5/same-criterion-audit.json",
        "automatic-noise-id-loss-20261015.json": "runtime/v6/refinement-r5/identity-loss-diagnostics.json",
        "automatic-noise-clinical-loss-20261015.json": "runtime/v6/refinement-r5/clinical-loss-diagnostics.json",
        "automatic-noise-trial-records-20261015.json": "runtime/v6/refinement-r8/structured-trial-records/manifest.json",
    }
    for name, source in aliases.items():
        target, raw = ROOT / "runtime/v6/validation" / name, (ROOT / source).read_bytes()
        if target.exists():
            assert target.read_bytes() == raw
        else:
            target.write_bytes(raw)
        selected.add(target)
    for name in ("requirements-v6.lock", "docs/implementation-v6.md",
        "runtime/v6/refinement-r4/automatic-features/manifest.json", identity_run + "/automatic-features-oof/manifest.json",
        "runtime/v6/refinement-r5/final-verification.json", "runtime/v6/refinement-r5/clinical-loss-diagnostics.json",
        "runtime/v6/refinement-r5/identity-loss-diagnostics.json", "runtime/v6/refinement-r5/same-criterion-audit.json",
        "runtime/v6/validation/automatic-noise-upgrade-20261015.json", "runtime/v6/validation/automatic-noise-upgrade-20261015.csv"):
        selected.add(ROOT / name)
    for folder in folders:
        selected.update(p for p in (ROOT / folder).rglob("*") if p.is_file())
    for pattern in ("*.log", "*.xml", "*complete.json"):
        selected.update(p for p in (ROOT / "runtime/v6/refinement-r4").glob(pattern) if not p.name.startswith("package"))
    selected.update((ROOT / identity_run).glob("*complete.json"))
    output = ROOT / "dist/v6-automatic-upgrade-20261015-complete.tar.gz"
    if output.exists():
        raise ValueError("Recovery bundle must be new")
    output.parent.mkdir(parents=True, exist_ok=True)
    receipt = {"schema": "v6-automatic-upgrade-recovery-1", "at": now(), "files": {},
               "requires_existing_frozen_v6_environment": True, "frozen_encoder_or_database_included": False,
               "requires_previous_refinement_bundle": {"name": "v6-refinement-results-20261015.tar.gz",
                   "sha256": "3bd4230cb7bc3d767550c3bd1966e559a589eadf8b46cb4a5acee4a2421e8592"},
               "feature_caches_included": "manifests only; regenerate with audited train corpora",
               "default_engine": "heuristic-v1", "scientific_targets_met": False}
    with tarfile.open(output, "w:gz") as archive:
        for path in sorted(selected):
            if not path.is_file() or not path.resolve().is_relative_to(ROOT.resolve()):
                raise ValueError("Invalid recovery source: " + str(path))
            name, raw = path.relative_to(ROOT).as_posix(), path.read_bytes()
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(raw), 0o600
            archive.addfile(entry, io.BytesIO(raw))
            receipt["files"][name] = {"sha256": digest(raw), "bytes": len(raw)}
        raw = dumps(receipt).encode()
        entry = tarfile.TarInfo("recovery-manifest.json")
        entry.size, entry.mode = len(raw), 0o600
        archive.addfile(entry, io.BytesIO(raw))
    manifest = {"archive": output.name, "sha256": digest(output.read_bytes()), "bytes": output.stat().st_size,
                "files": len(receipt["files"]), "inner_manifest_sha256": digest(raw)}
    output.with_suffix(".manifest.json").write_text(dumps(manifest))
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
