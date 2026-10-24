"""Official-API expansion CLI. Audit/reconcile are strictly read-only."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutri_literature.collection_v8 import DEFAULT_CONFIG, expand, inspect
from nutri_literature.settings import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("audit", "collect", "resume", "reconcile"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--max-batches", type=int, default=1)
    parser.add_argument("--continuous", action="store_true", help="Run all currently eligible metadata batches once")
    parser.add_argument("--skip-failed", action="store_true", help="Do not retry retained failed PMID rows")
    parser.add_argument("--min-available-gib", type=float, default=24)
    parser.add_argument("--min-free-disk-gib", type=float, default=50)
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.command in {"audit", "reconcile"}:
        result = inspect(settings, args.config)
    else:
        try:
            os.nice(10)
        except (AttributeError, PermissionError):
            pass
        result = expand(settings, args.config, max_batches=None if args.continuous else args.max_batches,
                        batch_size=args.batch_size, retry_failed=not args.skip_failed,
                        min_available_gib=args.min_available_gib, min_free_disk_gib=args.min_free_disk_gib)
    # Full missing lists remain available in returned audit data, not console logs.
    result.pop("missing_pmids", None)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if args.command == "reconcile" and not result.get("metadata_complete"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
