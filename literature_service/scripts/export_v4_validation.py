"""Export receipt-verified current development results and real operation checks."""
import argparse
import json
from pathlib import Path

from nutri_literature import benchmarks
from nutri_literature.db import digest, dumps, now
from nutri_literature.operations_v4 import operation_status
from nutri_literature.registry_v4 import default_engine, engine_rules_version
from nutri_literature.settings import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings.from_env()
    directory = settings.home / "experiments/v4"
    rules = engine_rules_version("heuristic-v4")
    reports = []
    historical = []
    for path in sorted(directory.glob("*/report.json")):
        value = json.loads(path.read_bytes())
        if not benchmarks.verify_receipt(settings, "public_v4_development_experiment", value):
            raise ValueError(f"Invalid development receipt: {path}")
        value["report_sha256"] = digest(path.read_bytes())
        selected = reports if value["identity"]["artifact"]["rules_version"] == rules else historical
        selected.append(value)
    summary = {
        "generated_at": now(), "engine": "heuristic-v4", "rules_version": rules,
        "default_engine": default_engine(settings), "status": "candidate_beta_goals_not_met",
        "current_reports": reports,
        "historical_reports": [{k: value.get(k) for k in
                                ("id", "at", "corpus", "mode", "provider", "profile", "papers", "metrics",
                                 "output_path", "relation_evaluation_scope", "report_sha256")} for value in historical],
        "recorded_final_attempts": [str(p) for p in directory.rglob("attempt.json")],
        "nutrition_accuracy": "not_measured", "review_time_saved": "not_measured",
        "operation": operation_status(settings),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "development-summary.json").write_text(dumps(summary), encoding="utf-8")
    drills = sorted((json.loads(p.read_bytes()) for p in directory.glob("drills-*.json")), key=lambda r: r["at"])
    (args.output / "operations-drill-history.json").write_text(dumps(drills), encoding="utf-8")
    if drills:
        (args.output / "operations-drills.json").write_text(dumps(drills[-1]), encoding="utf-8")
    examples = []
    for report in reports:
        by_stage = {}
        for failure in report["failures"]:
            stage = failure["first_failure"]
            if stage is not None:
                by_stage.setdefault(stage, failure)
        for stage, failure in by_stage.items():
            paper_path = Path(report["output_path"]).with_name("paper-" + failure["pmid"] + ".json")
            paper = json.loads(paper_path.read_bytes())
            unresolved = [m for m in paper["entities"] if m["kind"] == "gene" and not m.get("normalized_id")]
            examples.append({"report_id": report["id"], "corpus": report["corpus"], "mode": report["mode"],
                             "provider": report["provider"], "source_hash": paper["source_hash"],
                             "artifact_identity": paper["artifact_identity"], "failure": failure,
                             "unresolved_gene_examples": unresolved[:5], "events": paper["events"][:3]})
    (args.output / "failure-examples.json").write_text(dumps(examples), encoding="utf-8")
    print(dumps({"output": str(args.output), "current_reports": len(reports), "historical_reports": len(historical),
                 "failure_examples": len(examples)}), flush=True)


if __name__ == "__main__":
    main()
