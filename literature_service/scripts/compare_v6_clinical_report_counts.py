"""Pair authenticated token counts only after proving the scoring branches equal."""

import ast
import inspect
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature import benchmarks, evaluation_v5, evaluation_v6
    from nutri_literature.db import digest, dumps, now, uid
    from nutri_literature.settings import Settings
    from scripts.compare_v5_v6 import paired_rows

    def branch(function):
        tree = ast.parse(inspect.getsource(function))
        return next(
            ast.dump(node, include_attributes=False)
            for node in tree.body[0].body
            if isinstance(node, ast.If)
            and "ebmnlp" in ast.dump(node.test)
            and "evidenceoutcomes" in ast.dump(node.test)
        )

    left_ast, right_ast = (
        branch(function) for function in (evaluation_v5._metrics_for, evaluation_v6._metrics_for)
    )
    if left_ast != right_ast:
        raise ValueError("Clinical token scoring changed; raw predictions must be rescored")
    settings = Settings(ROOT / "runtime", ROOT / "config/topics.json")
    old_settings = Settings(Path("/home/keunsoo/projects/nutriomics-literature/runtime"), settings.config)
    old, new = evaluation_v5.reports(old_settings)["items"], evaluation_v6.reports(settings)["items"]
    latest = max(new, key=lambda r: r["at"])
    family = evaluation_v6.experiment_family(latest["artifact"])
    results = []
    for corpus in ("evidenceoutcomes", "ebmnlp"):

        def match(record):
            return (
                record["corpus"] == corpus
                and record["source_level"] == "abstract"
                and record["provider"] == "union"
                and record["ablation"] == "context"
                and record["mode"] == "end_to_end"
                and record["papers"] == 200
            )

        candidate = max(
            (r for r in new if match(r) and evaluation_v6.experiment_family(r["artifact"]) == family),
            key=lambda r: r["at"],
        )
        baseline = max((r for r in old if match(r)), key=lambda r: r["at"])
        if (
            baseline["input_sha256"] != candidate["input_sha256"]
            or baseline["gold_sha256"] != candidate["gold_sha256"]
        ):
            raise ValueError("Source or annotation cohort differs")
        results.append(
            {
                "corpus": corpus,
                "papers": 200,
                "v5_id": baseline["id"],
                "v6_id": candidate["id"],
                "input_sha256": candidate["input_sha256"],
                "gold_sha256": candidate["gold_sha256"],
                "v5_artifact_identity": baseline["artifact"]["artifact_identity"],
                "v6_artifact_identity": candidate["artifact"]["artifact_identity"],
                "metrics": paired_rows(baseline, candidate),
            }
        )
    report = {
        "id": uid(),
        "at": now(),
        "comparisons": results,
        "method": "authenticated saved per-paper token counts; clinical scoring AST and shared span scorer identical",
        "prediction_rescore_performed_by_this_script": False,
        "clinical_scoring_ast_sha256": digest(left_ast),
        "shared_span_scorer_source_sha256": digest(inspect.getsource(benchmarks._span_sets)),
        "bootstrap_iterations": 2000,
        "sealed_test_opened": False,
        "nutrition_accuracy": "unmeasured",
    }
    path = settings.home / "v6/validation" / ("paired-clinical-counts-" + report["id"] + ".json")
    benchmarks._write_immutable(path, report)
    benchmarks._receipt(settings, "v6_paired_clinical_report_counts", report["id"], report)
    print(dumps({"path": str(path), "comparisons": results}))


if __name__ == "__main__":
    main()
