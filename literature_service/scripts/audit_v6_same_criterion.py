"""Compare previous and new development cohorts, per-paper baselines and scorers."""
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


if __name__ == "__main__":
    from nutri_literature.db import digest, now
    from nutri_literature.benchmarks import _write_immutable
    from nutri_literature.extraction_v6 import source_manifest
    old_path = ROOT / "runtime/v6/refinement-r1/development/end_to_end/report.json"
    old = json.loads(old_path.read_bytes())
    records = []
    for relative in ("runtime/v6/refinement-r4/relations-end_to_end/report.json",
                     "runtime/v6/refinement-r5/relations-end_to_end/report.json"):
        path = ROOT / relative
        new = json.loads(path.read_bytes())
        assert old["input_sha256"] == new["input_sha256"]
        assert old["gold_sha256"] == new["gold_sha256"]
        assert old["per_paper"]["baseline"] == new["per_paper"]["baseline"]
        assert old["identity"]["frozen_v6_sources"] == new["identity"]["frozen_source"] == source_manifest()[1]
        records.append({"path": relative, "sha256": digest(path.read_bytes()),
                        "same_inputs_gold_and_each_paper_baseline": True})
    for field, name in (("relation_evaluator", "evaluation_v6.py"), ("clinical_evaluator", "clinical_evaluation_v6.py")):
        assert old["identity"][field] == digest((ROOT / "nutri_literature" / name).read_bytes())
    clinical_old_path = ROOT / "runtime/v6/refinement-r3/development/report.json"
    clinical_new_path = ROOT / "runtime/v6/refinement-r7/clinical-development/report.json"
    a, b = json.loads(clinical_old_path.read_bytes()), json.loads(clinical_new_path.read_bytes())
    assert a["input_sha256"] == b["input_sha256"] and a["gold_sha256"] == b["gold_sha256"]
    assert b["baseline_report_sha256"] == digest(clinical_old_path.read_bytes())
    assert a["metrics"]["refinement"] == b["metrics"]["previous_r3"]
    result = {"at": now(), "passed": True, "previous_relation_report_sha256": digest(old_path.read_bytes()),
              "relation": records, "unchanged_evaluator_sources": True, "clinical_same_cohort_and_baseline": True,
              "previous_clinical_report_sha256": digest(clinical_old_path.read_bytes()),
              "current_clinical_report_sha256": digest(clinical_new_path.read_bytes()),
              "new_gold_or_metric_used": False}
    _write_immutable(ROOT / "runtime/v6/refinement-r5/same-criterion-audit.json", result)
    print(json.dumps(result))
