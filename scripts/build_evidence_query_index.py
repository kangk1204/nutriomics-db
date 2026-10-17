"""Build immutable, release-bound workbench pagination references."""
import argparse
import json
from pathlib import Path

from nutrigenomics.evidence_paging import build


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    receipt = build(args.source, args.output, manifest['database_sha256'])
    args.output.with_suffix('.json').write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt))
