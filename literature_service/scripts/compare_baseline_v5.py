"""Paired development comparisons for endpoints with unchanged label semantics."""

import argparse
import json
from pathlib import Path

from nutri_literature import benchmarks as scoring
from nutri_literature.db import digest, dumps, now
from nutri_literature.settings import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings.from_env()
    old = json.loads(args.baseline.read_bytes())["current_reports"]
    new = json.loads(args.candidate.read_bytes())["experiments"]
    mapping = {
        "gene_detection": "gene",
        "gene_strict_id": "gene_strict_ids",
        "scopes:negation": "scopes_negation",
        "scopes:speculation": "scopes_speculation",
        "annotated_spans:participants": "spans_participants",
        "annotated_spans:interventions": "spans_interventions",
        "annotated_spans:outcomes": "spans_outcomes",
    }
    comparisons = []
    for previous in old:
        baseline = json.loads(Path(previous["output_path"]).read_bytes())
        if not scoring.verify_receipt(settings, "public_v4_development_experiment", baseline):
            raise ValueError("Unauthenticated v4 baseline")
        for current in new:
            if (
                current["corpus"] != baseline["corpus"]
                or current["mode"] != "end_to_end"
                or baseline["mode"] != "end_to_end"
                or current["ablation"] != "context"
                or current["source_level"] != "abstract"
            ):
                continue
            candidate = json.loads((Path(current["output_path"]) / "report.json").read_bytes())
            if not scoring.verify_receipt(settings, "public_v5_development_experiment", candidate):
                raise ValueError("Unauthenticated v5 experiment")
            if candidate["gold_sha256"] != baseline["gold_sha256"]:
                raise ValueError("Evaluation annotations differ")
            before = {p["pmid"]: p for p in baseline["per_paper"]}
            after = {p["pmid"]: p for p in candidate["per_paper"]}
            if set(before) != set(after):
                continue
            endpoints = {}
            for original, endpoint in mapping.items():
                if original not in baseline["metrics"] or endpoint not in candidate["metrics"]:
                    continue
                # A task absent from a paper is unannotated, not an empty gold set.
                # Preserve that applicability so paired_bootstrap can verify it.
                aligned = {
                    pmid: {**row, endpoint: row[original]} if original in row else dict(row)
                    for pmid, row in before.items()
                }
                # V5 explicitly serializes zero-count P/I/O rows that V4 omits.
                # Only those empty rows may be removed to match applicability;
                # never discard predictions or silently change aggregate scores.
                applicable_after = {pmid: dict(row) for pmid, row in after.items()}
                omitted_empty = []
                for pmid, row in applicable_after.items():
                    if original not in before[pmid] and endpoint in row:
                        if any(row[endpoint][key] for key in ("tp", "fp", "fn")):
                            raise ValueError("Nonempty counts outside baseline annotation coverage")
                        del row[endpoint]
                        omitted_empty.append(pmid)
                endpoints[endpoint] = {
                    "v4": baseline["metrics"][original],
                    "v5": candidate["metrics"][endpoint],
                    "unannotated_empty_rows_omitted": sorted(omitted_empty),
                    "paired_difference_95": scoring.paired_bootstrap(
                        sorted(before), aligned, applicable_after, endpoint, 2000, 20261009
                    ),
                }
            comparisons.append(
                {
                    "v4_report": baseline["id"],
                    "v5_report": candidate["id"],
                    "corpus": candidate["corpus"],
                    "v4_provider": baseline["provider"],
                    "v5_provider": candidate["provider"],
                    "papers": len(before),
                    "same_provider": baseline["provider"] == candidate["provider"],
                    "endpoints": endpoints,
                    "provider_change_is_not_an_isolated_ablation": baseline["provider"]
                    != candidate["provider"],
                }
            )
    record = {
        "at": now(),
        "schema": "v5-paired-v4-development-comparison",
        "development_exposure": True,
        "seed": 20261009,
        "bootstrap_resamples": 2000,
        "baseline_sha256": digest(args.baseline.read_bytes()),
        "candidate_sha256": digest(args.candidate.read_bytes()),
        "comparisons": comparisons,
        "relation_comparison_excluded": "v4 effect-to-benchmark projection and v5 native relation types have different label semantics",
    }
    scoring._write_immutable(args.out, record)
    print(dumps({"output": str(args.out), "comparisons": len(comparisons)}), flush=True)


if __name__ == "__main__":
    main()
