"""Recover numerical evidence from complete immutable source-first predictions."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature import benchmarks as scoring, evaluation_v5, evaluation_v6
    from nutri_literature.db import digest, dumps, now, uid
    from nutri_literature.settings import Settings
    from scripts.compare_v5_v6 import paired_rows

    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--baseline-home", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings(args.home, ROOT / "config/topics.json")
    identity = json.loads((args.predictions / "identity.json").read_bytes())
    index = json.loads((settings.home / "public_benchmarks/v2-measurement-index.json").read_bytes())
    key = "prepare:development:" + identity["corpus"] + ":" + identity["source_level"]
    prepared = index["steps"][key]
    inputs, _, sha = scoring._load_inputs(settings, prepared["inputs_path"])
    if inputs["split"] != "development" or sha != identity["input_sha256"]:
        raise ValueError("Saved predictions do not belong to the known development input")
    source = {d["pmid"]: d for d in inputs["documents"]}
    files = list(args.predictions.glob("paper-*.json"))
    restored, hashes = {}, {}
    for path in files:
        pred = json.loads(path.read_bytes())
        if (pred["pmid"] in restored or pred["pmid"] not in source
                or pred["source_hash"] != digest(source[pred["pmid"]]["text"])
                or pred["artifact_identity"] != identity["artifact"]["artifact_identity"]):
            raise ValueError("Saved prediction/source/artifact identity differs")
        restored[pred["pmid"]] = pred
        hashes[path.name] = digest(path.read_bytes())
    if set(restored) != set(identity["pmids"]):
        raise ValueError("Source-first prediction cohort is incomplete")
    args.out.mkdir(parents=True, exist_ok=True)
    receipt = {"id": uid(), "at": now(), "artifact": identity["artifact"],
               "prediction_files": hashes, "input_sha256": sha, "papers": len(restored),
               "reason": "Complete predictions survived a postprocessing-only import failure"}
    scoring._receipt(settings, "v6_saved_prediction_recovery", receipt["id"], receipt)
    scoring._write_immutable(args.out / (receipt["id"] + "-receipt.json"), receipt)
    candidate = evaluation_v6.rescore(args.predictions, prepared["gold_path"],
                                      args.out / (receipt["id"] + "-v6-rescore.json"))
    old_settings = Settings(args.baseline_home, settings.config)
    baselines = [r for r in evaluation_v5.reports(old_settings)["items"]
                 if all(r[k] == identity[k] for k in ("corpus", "mode", "provider", "source_level"))
                 and r["ablation"] == "context" and r["input_sha256"] == sha]
    baseline = max(baselines, key=lambda r: r["at"])
    old = evaluation_v6.rescore(baseline["output_path"], prepared["gold_path"],
                                args.out / (receipt["id"] + "-v5-rescore.json"))
    result = {"id": receipt["id"], "at": now(), "engine": "heuristic-v6",
              "corpus": identity["corpus"], "mode": identity["mode"], "papers": len(restored),
              "prediction_artifact": identity["artifact"], "recovery_receipt": receipt,
              "v5_id": baseline["id"], "v5_prediction_artifact": baseline["artifact"],
              "metrics": paired_rows(old, candidate),
              "nutrition_accuracy": "unmeasured", "sealed_test_opened": False,
              "standard_development_report_complete": False}
    path = args.out / (receipt["id"] + "-comparison.json")
    scoring._write_immutable(path, result)
    scoring._receipt(settings, "v6_recovered_development_comparison", result["id"], result)
    print(dumps({"path": str(path), "papers": result["papers"], "mode": result["mode"],
                 "metrics": result["metrics"]}))


if __name__ == "__main__":
    main()
