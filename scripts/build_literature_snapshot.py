"""Build a new public or internal literature projection without changing the source DB."""
import argparse
import json
from pathlib import Path

from nutrigenomics.literature_snapshot import build_snapshot


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--release-id", required=True)
    p.add_argument("--internal", action="store_true")
    p.add_argument("--baseline-reference", type=Path)
    args = p.parse_args()
    print(json.dumps(build_snapshot(args.source, args.output, release_id=args.release_id,
                                   public=not args.internal, baseline_reference=args.baseline_reference), ensure_ascii=False))


if __name__ == "__main__":
    main()
