"""One-pass development collection; holdout execution requires source-only human gold."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nutrigenomics.retrieval_review import (
    evaluate_review,
    load_package,
    read_json,
    validate_human_gold,
)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--partition", choices=["legacy-development", "development", "holdout"], default="legacy-development")
    parser.add_argument("--base-url")
    parser.add_argument("--software-version")
    parser.add_argument("--human-gold", type=Path)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-only", action="store_true", help="Validate human review bindings without HTTP or final-query execution")
    args = parser.parse_args()
    try:
        if args.validate_only:
            if not args.human_gold:
                parser.error("--validate-only requires --human-gold")
            manifest, questions, catalog = load_package(args.package, args.partition)
            validated = validate_human_gold(read_json(args.human_gold), manifest, questions, catalog, args.partition)
            print(json.dumps({"status": "validation_only_no_http", "query_reviews": len(validated),
                              "partition": args.partition, "system_queries_executed": 0}, ensure_ascii=False))
            raise SystemExit(0)
        if not args.base_url or not args.software_version or not args.output:
            parser.error("Execution requires --base-url, --software-version and --output")
        result = evaluate_review(args.package, args.output, args.base_url, partition=args.partition,
                                 human_gold=read_json(args.human_gold) if args.human_gold else None,
                                 authorization=read_json(args.authorization) if args.authorization else None,
                                 software_version=args.software_version)
    except (PermissionError, ValueError, FileExistsError) as error:
        parser.exit(2, str(error) + "\n")
    print(json.dumps({key: result[key] for key in ["partition", "query_count", "available_queries", "actual_unique_match_requests",
                                                   "scientific_accuracy_status", "machine_policy"]}, ensure_ascii=False, indent=2))
