"""Prepare audited native PIO and sentence-role training data only."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutri_literature.clinical_data_v8 import prepare_clinical


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-directory", required=True, type=Path)
    parser.add_argument("--allowed-manifest", required=True, type=Path)
    parser.add_argument("--allowed-manifest-sha256")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--protection", type=Path)
    args = parser.parse_args()
    result = prepare_clinical(args.source_directory, args.allowed_manifest, args.output, args.protection,
                              args.allowed_manifest_sha256)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
