"""Inspect the evidence extension without writing or deriving health claims."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.evidence_query import LAYERS, query

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--database", type=Path, required=True)
parser.add_argument("--subject-id")
parser.add_argument("--layer", choices=sorted(LAYERS))
parser.add_argument("--limit", type=int, default=100)
parser.add_argument("--offset", type=int, default=0)
args = parser.parse_args()
print(
    json.dumps(query(args.database, args.subject_id, args.layer, args.limit, args.offset), indent=2)
)
