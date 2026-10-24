"""Audit complete development predictions without changing models or thresholds."""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature import benchmarks as scoring, evaluation_v6
    from nutri_literature.db import digest, dumps, now, uid
    from nutri_literature.settings import Settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings(args.home, ROOT / "config/topics.json")
    identity = json.loads((args.predictions / "identity.json").read_bytes())
    index = json.loads((settings.home / "public_benchmarks/v2-measurement-index.json").read_bytes())
    prepared = index["steps"]["prepare:development:bioredirect:abstract"]
    inputs, _, sha = scoring._load_inputs(settings, prepared["inputs_path"])
    if (identity["corpus"] != "bioredirect" or sha != identity["input_sha256"]
            or inputs["split"] != "development"):
        raise ValueError("Only the authenticated complete BioREDirect development cohort is allowed")
    source = {d["pmid"]: d for d in inputs["documents"]}
    predictions, file_hashes = {}, {}
    for path in args.predictions.glob("paper-*.json"):
        pred = json.loads(path.read_bytes())
        if (pred["pmid"] in predictions or pred["pmid"] not in source
                or pred["source_hash"] != digest(source[pred["pmid"]]["text"])
                or pred["artifact_identity"] != identity["artifact"]["artifact_identity"]):
            raise ValueError("Prediction/source/artifact mismatch")
        predictions[pred["pmid"]] = pred
        file_hashes[path.name] = digest(path.read_bytes())
    if set(predictions) != set(identity["pmids"]):
        raise ValueError("Diagnostics require every prediction from the saved cohort")
    gold, _, gold_sha = scoring._load_gold(settings, prepared["gold_path"])
    labels = {d["pmid"]: d for d in gold["documents"]}
    counts, directions, false_positives = Counter(), Counter(), Counter()
    covered = total = 0
    examples = []
    for pmid, prediction in predictions.items():
        # The same source-only native-ID mapping used by the common rescorer.
        mapping = {}
        for entity in prediction["entities"]:
            native = entity.get("payload", {}).get("native_id")
            identifier = entity.get("normalized_id")
            kind = "gene" if entity["kind"] == "gene" else "component"
            if native and identifier:
                mapping[(kind, identifier)] = {str(native)}
            elif identifier and str(identifier).startswith(("MESH:", "NCBIGene:")):
                mapping[(kind, identifier)] = {identifier.split(":", 1)[1]}
        oracle = identity["mode"] == "gold_entities"
        fn, role, fp, errors = evaluation_v6.failures(labels[pmid], prediction, mapping, oracle)
        counts.update(fn)
        directions.update(role)
        false_positives.update(fp)
        examples.extend(errors)
        coverage = evaluation_v6.candidate_coverage(labels[pmid], prediction)
        covered += coverage["tp"]
        total += coverage["gold_relations"]
    value = {"id": uid(), "at": now(), "schema": "saved-development-diagnostics-v6-1",
             "corpus": "bioredirect", "mode": identity["mode"], "papers": len(predictions),
             "prediction_artifact": identity["artifact"], "prediction_files": file_hashes,
             "input_sha256": sha, "gold_sha256": gold_sha,
             "evaluator_sha256": digest(Path(evaluation_v6.__file__).read_bytes()),
             "failure_counts": dict(counts), "direction_failure_counts": dict(directions),
             "false_positive_counts": dict(false_positives),
             "candidate_coverage": {"tp": covered, "fn": total-covered, "total": total,
                                    "recall": covered/total if total else None},
             "error_examples": examples[:50], "labels_used_for_selection": False,
             "sealed_test_opened": False, "nutrition_accuracy": "unmeasured"}
    scoring._write_immutable(args.out, value)
    scoring._receipt(settings, "v6_saved_development_diagnostics", value["id"], value)
    print(dumps({k: value[k] for k in ("mode", "papers", "candidate_coverage", "failure_counts",
                                     "direction_failure_counts", "false_positive_counts")}))


if __name__ == "__main__":
    main()
