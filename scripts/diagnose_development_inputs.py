"""Diagnose development food discovery and explicitly selected research exposures."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.retrieval_input_diagnostics import diagnose_development_inputs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected-release-id", required=True)
    parser.add_argument("--software-version", required=True)
    parser.add_argument("--exposure-id", action="append", default=[])
    args = parser.parse_args()
    result = diagnose_development_inputs(args.review, args.output, args.base_url,
        expected_release_id=args.expected_release_id, software_version=args.software_version,
        selected_exposure_ids=tuple(args.exposure_id))
    print(json.dumps({"request_count": result["request_count"], "scope": result["scope"],
                      "diagnostic_file": str(Path(args.output) / "input-diagnostic.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
