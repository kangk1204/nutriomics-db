"""Rescore authenticated development predictions identically, then pair papers."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def paired_rows(baseline, candidate, iterations=2000):
    from nutri_literature import benchmarks as scoring

    left = {str(row["pmid"]): row for row in baseline["per_paper"]}
    right = {str(row["pmid"]): row for row in candidate["per_paper"]}
    if not left or set(left) != set(right):
        raise ValueError("Comparison requires the complete same paper cohort")
    if len(left) != len(baseline["per_paper"]) or len(right) != len(candidate["per_paper"]):
        raise ValueError("Duplicate paper in paired comparison")
    endpoints = set(baseline["metrics"]) & set(candidate["metrics"])
    output = {}
    for endpoint in sorted(endpoints - {"gene_candidates"}):
        for pmid in left:
            a, b = left[pmid][endpoint], right[pmid][endpoint]
            if a["tp"] + a["fn"] != b["tp"] + b["fn"]:
                raise ValueError("Gold denominator differs: " + endpoint + ":" + pmid)
            if left[pmid].get("group_id", pmid) != right[pmid].get("group_id", pmid):
                raise ValueError("Paired bootstrap grouping differs")
        a, b = (scoring._aggregate(rows, endpoint) for rows in (list(left.values()), list(right.values())))
        def grouped(rows):
            groups = {}
            for pmid, row in rows.items():
                groups.setdefault(str(row.get("group_id", pmid)), []).append(row)
            return {key: {endpoint: scoring._aggregate(values, endpoint)} for key, values in groups.items()}

        grouped_left, grouped_right = grouped(left), grouped(right)
        output[endpoint] = {
            "v5": a, "v6": b,
            "delta_percentage_points": {
                metric: 100 * (b[metric] - a[metric])
                if a[metric] is not None and b[metric] is not None else None
                for metric in ("precision", "recall", "f1")
            },
            "paired_bootstrap": scoring.paired_bootstrap(
                sorted(grouped_left), grouped_left, grouped_right, endpoint, iterations, 20261010
            ),
        }
    return output


def compare(baseline_home, candidate_home, output):
    from nutri_literature import benchmarks as scoring, evaluation_v5, evaluation_v6
    from nutri_literature.db import digest, dumps, now
    from nutri_literature.settings import Settings

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    settings = Settings(Path(candidate_home), ROOT / "config/topics.json")
    baseline_settings = Settings(Path(baseline_home), settings.config)
    old = evaluation_v5.reports(baseline_settings)["items"]
    new = evaluation_v6.reports(settings)["items"]
    old = [r for r in old if r["ablation"] == "context"]
    new = [r for r in new if r["ablation"] == "context"]
    latest = max(new, key=lambda r: r["at"], default=None)
    if latest:
        family = evaluation_v6.experiment_family(latest["artifact"])
        new = [r for r in new if evaluation_v6.experiment_family(r["artifact"]) == family]
    required = {
        ("bioredirect", "union", "end_to_end", "abstract"),
        ("bioredirect", "union", "gold_entities", "abstract"),
        ("bioscope", "union", "end_to_end", "abstract"),
        ("bioscope", "union", "end_to_end", "fulltext"),
        ("ebmnlp", "union", "end_to_end", "abstract"),
        ("evidenceoutcomes", "union", "end_to_end", "abstract"),
    }
    keys = sorted(required | {(r["corpus"], r["provider"], r["mode"], r["source_level"]) for r in new})
    comparisons, missing = [], []
    for key in keys:
        def matches(report):
            return tuple(report[k] for k in ("corpus", "provider", "mode", "source_level")) == key

        left = max((r for r in old if matches(r)), key=lambda r: r["at"], default=None)
        right = max((r for r in new if matches(r)), key=lambda r: r["at"], default=None)
        if right is None:
            missing.append({"configuration": key, "reason": "authenticated_v6_report_missing"})
            continue
        if left is None:
            missing.append({"configuration": key, "reason": "authenticated_v5_report_missing"})
            continue
        if left["input_sha256"] != right["input_sha256"] or left["gold_sha256"] != right["gold_sha256"]:
            missing.append({"configuration": key, "reason": "source_or_gold_hash_differs"})
            continue
        index = json.loads((settings.home / "public_benchmarks/v2-measurement-index.json").read_bytes())
        prepared = index["steps"]["prepare:development:" + key[0] + ":" + key[3]]
        gold = Path(prepared["gold_path"])
        inputs = json.loads((gold.parent / "inputs.json").read_bytes())
        if inputs.get("split") != "development":
            raise ValueError("Comparison may only rescore development annotations")
        if digest(gold.read_bytes()) != right["gold_sha256"]:
            raise ValueError("Development gold no longer matches the saved report")
        rescored = []
        for label, report, home in (("v5", left, baseline_settings.home), ("v6", right, settings.home)):
            candidates = list((home / ("experiments/" + label)).glob("*/report.json"))
            location = next(p.parent for p in candidates if json.loads(p.read_bytes())["id"] == report["id"])
            stem = digest(report["id"] + digest(Path(evaluation_v6.__file__).read_bytes()))[:24]
            destination = output / (label + "-rescore-" + stem + ".json")
            if destination.exists():
                destination = output / (label + "-rescore-" + stem + "-" + digest(now())[:8] + ".json")
            rescored.append(evaluation_v6.rescore(location, gold, destination))
        comparisons.append({
            "configuration": key,
            "baseline_id": left["id"], "candidate_id": right["id"],
            "input_sha256": right["input_sha256"], "gold_sha256": right["gold_sha256"],
            "v5_artifact": left["artifact"], "v6_artifact": right["artifact"],
            "papers": rescored[1]["papers"], "metrics": paired_rows(*rescored),
        })
    value = {
        "at": now(), "comparison_completed": bool(comparisons) and not missing,
        "same_evaluator_sha256": digest(Path(evaluation_v6.__file__).read_bytes()),
        "comparisons": comparisons, "missing": missing,
        "status": "measured_development" if comparisons else "awaiting_saved_v6_predictions",
        "bootstrap_iterations": 2000, "sealed_test_opened": False,
        "nutrition_accuracy": "unmeasured", "default_engine": "heuristic-v1",
    }
    destination = output / ("v5-v6-comparison-" + digest(dumps(value))[:24] + ".json")
    scoring._write_immutable(destination, value)
    print(dumps({"path": str(destination), "comparisons": len(comparisons), "status": value["status"]}))
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-home", type=Path, required=True)
    parser.add_argument("--candidate-home", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    compare(args.baseline_home, args.candidate_home, args.out)
