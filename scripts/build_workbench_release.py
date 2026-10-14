"""Build an immutable release; explicitly never switch the production pointer."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.workbench_build import build_workbench_release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-release", type=Path)
    parser.add_argument("--sidecar", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    args = parser.parse_args()
    result = build_workbench_release(
        args.from_release, args.sidecar, args.output_dir, args.release_id
    )
    print(
        json.dumps(
            {
                "release_id": result["release_id"],
                "counts": result["counts"],
                "database_sha256": result["database_sha256"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
