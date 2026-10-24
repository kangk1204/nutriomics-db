"""Source-only clinical condition sidecars; never invent ICO benchmark labels."""
from collections import Counter
import json
import os
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature import trial_records_v6 as records
    from nutri_literature.trial_roles_v6 import mesh_terms, matches
    from nutri_literature.refinement_v6 import tokens
    from nutri_literature.resources_v6 import memory_budget
    from nutri_literature.operations_v6 import processing_slot
    from nutri_literature.settings import Settings
    from nutri_literature.benchmarks import _write_immutable
    from nutri_literature.db import digest, dumps, now
    work = ROOT / "runtime/v6/refinement-r8/structured-trial-records"
    spec = json.loads((ROOT / "runtime/v6/training-data/recovery/inference-official-booleans/development-confirmed/spec.json").read_bytes())
    raw = Path(spec["inputs_path"]).read_bytes()
    assert digest(raw) == spec["inputs_sha256"]
    data = json.loads(raw)
    with memory_budget() as resources, processing_slot(Settings(ROOT / "runtime", ROOT / "config/topics.json")):
        trie, mesh_sha = mesh_terms(ROOT / "runtime/v6/refinement-r1/frozen-models")
        identity = {"record_source": records.source_sha(), "mesh_sha256": mesh_sha, "input_sha256": digest(raw),
                    "runner_sha256": digest(Path(__file__).read_bytes()),
                    "role_manifest_sha256": digest((ROOT / "runtime/v6/refinement-r5/clinical-roles-final/manifest.json").read_bytes()),
                    "dependencies": {name: digest((ROOT / "nutri_literature" / name).read_bytes()) for name in (
                        "trial_active_v6.py", "trial_linking_v6.py", "clinical_grounding_v6.py", "trial_roles_v6.py")}}
        totals, files = Counter(), []
        work.mkdir(parents=True, exist_ok=True)
        for doc in data["documents"]:
            path = work / ("paper-" + doc["pmid"] + ".json")
            if path.exists():
                pred = json.loads(path.read_bytes())
                assert pred["identity"] == identity and pred["source_hash"] == doc["source_hash"]
            else:
                role_raw = (ROOT / "runtime/v6/refinement-r5/clinical-development" / path.name).read_bytes()
                role_pred = json.loads(role_raw)
                assert role_pred["source_hash"] == doc["source_hash"]
                sequence = tokens(doc["text"])
                chemical = [{"start": sequence[a]["start"], "end": sequence[b - 1]["end"],
                             "text": doc["text"][sequence[a]["start"]:sequence[b - 1]["end"]]} for a, b in matches(sequence, trie)]
                registry = records.build_registry(doc, role_pred["trace"]["source_role_candidates"], chemical)
                links = records.link_results(doc, registry)
                pred = {"pmid": doc["pmid"], "source_hash": doc["source_hash"], "identity": identity,
                        "role_prediction_sha256": digest(role_raw), "arm_registry": registry, "result_links": links}
                _write_immutable(path, pred)
            totals["papers"] += 1
            totals["arm_occurrences"] += len(pred["arm_registry"])
            totals["arms_with_dose"] += sum(bool(a["doses"]) for a in pred["arm_registry"])
            totals["complete_regimens"] += sum(a["regimen_complete"] for a in pred["arm_registry"])
            totals["result_link_candidates"] += len(pred["result_links"])
            totals["resolved_arm_links"] += sum(bool(a["arm_instance_ids"]) for a in pred["result_links"])
            totals["links_with_followup"] += sum(a["timepoint"] is not None for a in pred["result_links"])
            totals["held_arm_links"] += sum(not a["arm_instance_ids"] for a in pred["result_links"])
            files.append({"path": path.name, "sha256": digest(path.read_bytes())})
        _write_immutable(work / "manifest.json", {"at": now(), "identity": identity, "files": files,
            "counts": dict(totals), "resources": resources, "condition_accuracy": "unmeasured",
            "benchmark_ICO_predictions_changed": False, "all_records_are_review_candidates": True})
        print(dumps(dict(totals)), flush=True)


if __name__ == "__main__":
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = "1"
    if os.name == "posix":
        os.nice(10)
    main()
