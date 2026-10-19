"""Build a release-bound Korean submission report and human-review package."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nutrigenomics.reporting_review import (
    build_report,
    build_review_package,
    validate_review_decisions,
)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--science", type=Path, required=True, help="Final SQLite or read-only science snapshot JSON")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--internal-contract", type=Path)
    parser.add_argument("--public-contract", type=Path)
    parser.add_argument("--clinical-summary", type=Path)
    parser.add_argument("--validation-receipts", type=Path, help="Dated hash-bound receipts JSON list")
    parser.add_argument("--decisions", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--review-only", action="store_true")
    args = parser.parse_args()
    if args.decisions:
        validate_review_decisions(args.science, load(args.decisions))
    if args.review_only:
        result = build_review_package(args.science, args.output)
    else:
        if not args.internal_contract or not args.public_contract:
            parser.error("Report requires both canonical contracts")
        result = build_report(internal_contract=load(args.internal_contract), public_contract=load(args.public_contract),
            science_source=args.science, output=args.output, workspace=args.workspace,
            validation_receipts=load(args.validation_receipts) if args.validation_receipts else None,
            review_decisions=load(args.decisions) if args.decisions else None,
            clinical_summary=load(args.clinical_summary) if args.clinical_summary else None)
    print(json.dumps({"output": str(args.output.resolve()), "journal_submission_ready": False,
                      "review_records": result.get("records", result.get("review", {}).get("expected_records"))}))


if __name__ == "__main__":
    main()
