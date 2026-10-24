"""Build immutable source-classification and analysis-review upgrade inputs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from nutrigenomics.submission_upgrade import (
    analysis_resolution_queue,
    build_review_dependencies,
    build_source_classifications,
    science_inventory,
)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--science", type=Path, required=True)
    p.add_argument("--packets", type=Path, required=True)
    p.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    packets = json.loads(a.packets.read_text(encoding="utf-8"))
    results = {"source-classifications.json": build_source_classifications(a.science, packets, a.workspace),
               "analysis-resolution-queue.json": analysis_resolution_queue(a.science, packets),
               "review-dependencies.json": build_review_dependencies(a.science, packets, a.workspace),
               "science-inventory.json": science_inventory(a.science)}
    a.output.mkdir(parents=True, exist_ok=False)
    for name, value in results.items():
        (a.output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(a.output), "records": results["science-inventory.json"]["records"],
                      "classified_records": len(results["source-classifications.json"]["records"]),
                      "analyses": results["analysis-resolution-queue.json"]["counts"], "human_approvals_created": 0}))


if __name__ == "__main__":
    main()
