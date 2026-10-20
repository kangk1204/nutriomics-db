"""Freeze source-only review questions without opening the original sealed file."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nutrigenomics.retrieval_review import prepare_review

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", type=Path, default=ROOT / "outputs/publication/2026-10-09/benchmark-review")
    parser.add_argument("--curator", type=Path, default=ROOT / "config/human_exposures.v1.json")
    parser.add_argument("--science", type=Path, default=ROOT / "outputs/publication/2026-10-19/evidence/final-public-science.json")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/publication/2026-10-20/retrieval-review/benchmark-review-v2.2")
    args = parser.parse_args()
    result = prepare_review(args.benchmark, args.curator, args.science, args.output)
    print(json.dumps({"version": result["version"], "question_sets": result["question_sets"],
                      "original_sealed_file_opened": False, "expert_approved_queries": 0}, ensure_ascii=False, indent=2))
