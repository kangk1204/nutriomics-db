"""Trial linkage extension, unchanged strict 92-paper clinical metric."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature import benchmarks as scoring, clinical_boundary_v6 as boundary_code
    from nutri_literature import trial_linking_v6 as trial
    from nutri_literature.refinement_v6 import load_artifact, source_identity
    from nutri_literature.clinical_evaluation_v6 import evidence_counts
    from nutri_literature.db import digest, dumps, uid, now
    from nutri_literature.operations_v6 import processing_slot
    from nutri_literature.resources_v6 import memory_budget
    from nutri_literature.settings import Settings
    parser = argparse.ArgumentParser()
    for key in ("roles", "boundary", "spec", "inherited", "baseline", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    args = parser.parse_args()
    settings = Settings(ROOT / "runtime", ROOT / "config/topics.json")
    with memory_budget() as resources, processing_slot(settings):
        roles, role_manifest = load_artifact(args.roles)
        boundary, manifest = boundary_code.load(args.boundary)
        assert manifest["upstream_role_manifest_sha256"] == digest((args.roles / "manifest.json").read_bytes())
        spec = json.loads(args.spec.read_bytes())
        assert spec["official_split"] == "development"
        raw = Path(spec["inputs_path"]).read_bytes()
        assert digest(raw) == spec["inputs_sha256"]
        inputs = json.loads(raw)
        assert not set(manifest["training_pmids"]) & {d["pmid"] for d in inputs["documents"]}
        assert all(not set(d) & {"comparisons", "evidence", "labels", "entities"} for d in inputs["documents"])
        identity = {"engine": "trial-linking-v6-r4", "trial_source": trial.source_sha(), "role_source": source_identity(),
                    "boundary_source": boundary_code.implementation_sha(), "input_sha256": digest(raw),
                    "runner_sha256": digest(Path(__file__).read_bytes()),
                    "role_manifest_sha256": digest((args.roles / "manifest.json").read_bytes()),
                    "boundary_manifest_sha256": digest((args.boundary / "manifest.json").read_bytes())}
        args.output.mkdir(parents=True, exist_ok=True)
        preds = {}
        for i, doc in enumerate(inputs["documents"]):
            assert trial.source_sha() == identity["trial_source"]
            path = args.output / ("paper-" + doc["pmid"] + ".json")
            if path.exists():
                pred = json.loads(path.read_bytes())
                assert pred["identity"] == identity and pred["source_hash"] == doc["source_hash"]
            else:
                inherited_raw = (args.inherited / path.name).read_bytes()
                inherited = json.loads(inherited_raw)
                assert inherited["source_hash"] == doc["source_hash"]
                values, trace = trial.comparisons(doc, roles, role_manifest, boundary, inherited["automatic_comparisons"])
                pred = {"pmid": doc["pmid"], "identity": identity, "source_hash": doc["source_hash"],
                        "automatic_comparisons": values, "trace": trace, "inherited_prediction_sha256": digest(inherited_raw)}
                scoring._write_immutable(path, pred)
            preds[doc["pmid"]] = pred
            print(dumps({"stage": "trial_linking", "papers": i + 1, "total": len(inputs["documents"]),
                         "comparisons": len(pred["automatic_comparisons"])}), flush=True)
        gold_raw = Path(spec["gold_path"]).read_bytes()
        assert digest(gold_raw) == spec["gold_sha256"]
        gold = json.loads(gold_raw)
        baseline = json.loads(args.baseline.read_bytes())
        assert baseline["input_sha256"] == digest(raw) and baseline["gold_sha256"] == digest(gold_raw)
        old = {r["pmid"]: r for r in baseline["per_paper"]}
        rows, alignment = [], Counter()
        for doc in gold["documents"]:
            pred = preds[doc["pmid"]]
            counts = evidence_counts(doc, pred["automatic_comparisons"], [])
            row = {"pmid": doc["pmid"], "automatic_ico_evidence_tokens": counts["automatic_ico_evidence_tokens"], "ico_alignment": counts["ico_alignment"]}
            rows.append(row)
            alignment.update(row["ico_alignment"])
        new = {r["pmid"]: r for r in rows}
        key = "automatic_ico_evidence_tokens"
        assert set(old) == set(new)
        assert all(old[p][key]["tp"] + old[p][key]["fn"] == new[p][key]["tp"] + new[p][key]["fn"] for p in old)
        report = {"id": uid(), "at": now(), "identity": identity, "papers": len(rows), "schema": "v6-r4-trial-linking-development-1",
                  "input_sha256": digest(raw), "gold_sha256": digest(gold_raw),
                  "baseline_report_sha256": digest(args.baseline.read_bytes()),
                  "metrics": {"previous_r3": scoring._aggregate(list(old.values()), key), "trial_linking": scoring._aggregate(rows, key)},
                  "per_paper": rows, "alignment": dict(alignment), "resources": resources,
                  "paired_vs_r3": scoring.paired_bootstrap(sorted(old), old, new, key, 2000, 20261010),
                  "nutrition_accuracy": "unmeasured", "sealed_test_opened": False, "default_engine": "heuristic-v1",
                  "condition_accuracy": "unmeasured; source-alignment and behavior regression only"}
        scoring._write_immutable(args.output / "report.json", report)
        scoring._receipt(settings, "v6_r4_trial_linking_development", report["id"], report)
        print(dumps({"report": str(args.output / "report.json"), "metrics": report["metrics"], "alignment": dict(alignment)}), flush=True)


if __name__ == "__main__":
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = "1"
    if os.name == "posix":
        os.nice(10)
    main()
