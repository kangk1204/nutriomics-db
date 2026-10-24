"""Publish local machine-readable results only after independent verification."""
import csv
import json
from pathlib import Path
import platform
import socket
import sys
import xml.etree.ElementTree as ET
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature.benchmarks import _write_immutable
    from nutri_literature.db import digest, now
    verified_path = ROOT / "runtime/v6/refinement-r5/final-verification.json"
    verified = json.loads(verified_path.read_bytes())
    assert verified["passed"]
    xml = ROOT / "runtime/v6/refinement-r4/linux-upgrade-final-tests.xml"
    suite = ET.parse(xml).getroot().find("testsuite")
    tests = {k: int(suite.get(k, 0)) for k in ("tests", "failures", "errors", "skipped")}
    assert tests["failures"] == tests["errors"] == 0
    reports = {}
    for name, folder in {"automatic_noise": "runtime/v6/refinement-r4/relations-end_to_end",
                         "oracle_noise": "runtime/v6/refinement-r4/relations-gold_entities",
                         "identity_noise": verified["identity_run"] + "/relations-end_to_end",
                         "clinical": "runtime/v6/refinement-r7/clinical-development"}.items():
        path = ROOT / folder / "report.json"
        report = json.loads(path.read_bytes())
        assert verified["reports"][folder]["report_sha256"] == digest(path.read_bytes())
        reports[name] = {"path": str(path.relative_to(ROOT)), "sha256": digest(path.read_bytes()),
                         "input_sha256": report["input_sha256"], "gold_sha256": report["gold_sha256"],
                         "metrics": report["metrics"], "papers": report["papers"]}
        for field in ("paired_vs_identity_gate", "paired_recall_ensemble", "paired_identity_recovery_only", "strict_gene_id", "restored_gene_mentions", "alignment", "paired_vs_r3"):
            if field in report:
                reports[name][field] = report[field]
    identity_manifest_path = ROOT / verified["identity_run"] / "identity-head/manifest.json"
    identity_manifest = json.loads(identity_manifest_path.read_bytes())
    roles_path = ROOT / "runtime/v6/refinement-r5/clinical-roles-final/manifest.json"
    role_manifest = json.loads(roles_path.read_bytes())
    loss_path = ROOT / "runtime/v6/refinement-r5/clinical-loss-diagnostics.json"
    summary = {"at": now(), "execution_complete": True, "scientific_targets_met": False,
        "server": {"ssh": "keunsoo@100.75.73.88", "hostname": socket.gethostname(), "python": platform.python_version()},
        "reports": reports, "verification_sha256": digest(verified_path.read_bytes()), "tests": tests,
        "structured_trial_records": verified["structured_trial_records"],
        "clinical_diagnostics": json.loads(loss_path.read_bytes()),
        "identity_training": {"manifest_sha256": digest(identity_manifest_path.read_bytes()),
            "selection": identity_manifest["selection"], "examples": identity_manifest["examples"],
            "candidate_rows": identity_manifest["candidate_rows"], "precision90_met": identity_manifest["target_precision_met"]},
        "clinical_training": {"manifest_sha256": digest(roles_path.read_bytes()), "roles": role_manifest["roles"]},
        "decision": {"default_engine": "heuristic-v1", "confirmed_evidence_changed": False,
            "comparison_candidate": "fixed union of existing-ID original model and automatic-noise model",
            "id_recovery": "experimental only; target precision not met",
            "clinical_model": "not adopted; no additional strict ICO true positives",
            "sealed_test_opened": False, "nutrition_accuracy": "unmeasured", "condition_accuracy": "unmeasured"},
        "scope": "Same development corpora/scorers. Cached frozen automatic entity predictions reused. Fresh relation and independent-ID inference. Not a fresh full-service run or independent final test.",
        "oof_scope": verified["oof_scope"]}
    output = ROOT / "runtime/v6/validation/automatic-noise-upgrade-20261015.json"
    _write_immutable(output, summary)
    rows = []
    for name in ("automatic_noise", "oracle_noise", "identity_noise"):
        for stage, metric in reports[name]["metrics"].items():
            rows.append({"task": "chemical_gene_type_id", "experiment": name, "stage": stage,
                         "tp_unit": "relation", **{k: metric["relation"][k] for k in ("tp", "fp", "fn", "precision", "recall", "f1")}})
    for stage, metric in reports["clinical"]["metrics"].items():
        rows.append({"task": "strict_automatic_ICO_evidence", "experiment": "clinical", "stage": stage,
                     "tp_unit": "evidence_token", **{k: metric[k] for k in ("tp", "fp", "fn", "precision", "recall", "f1")}})
    csv_path = output.with_suffix(".csv")
    if csv_path.exists():
        raise ValueError("Summary CSV already exists")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"path": str(output), "tests": tests, "scientific_targets_met": False}))


if __name__ == "__main__":
    main()
