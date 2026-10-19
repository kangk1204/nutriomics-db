"""Create bounded disposable tmpfs replicas from an explicit immutable plan."""
import argparse
import json
from pathlib import Path

from nutrigenomics.source_replicas import copy_plan

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--ram-root', type=Path, required=True)
    parser.add_argument('--receipts', type=Path, required=True)
    args = parser.parse_args()
    result = copy_plan(json.loads(args.plan.read_text(encoding='utf-8')), args.ram_root, args.receipts)
    print(json.dumps(result, indent=2))
