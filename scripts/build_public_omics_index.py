"""Build a new immutable public ordered-reference derivative (one source stream)."""
import argparse
import json
from pathlib import Path

from nutrigenomics.public_omics_index import build

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-sha256', required=True)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output, args.source_sha256, resume=args.resume)), flush=True)
