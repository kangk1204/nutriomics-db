"""Build a separate local research release with exact-source Korean dishes."""
import argparse
from pathlib import Path

from nutrigenomics.korean_dishes import build

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['parent', 'raw-directory', 'selection', 'output']:
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--release-id', required=True)
    args = parser.parse_args()
    build(args.parent, args.raw_directory, args.selection, args.output, args.release_id)
