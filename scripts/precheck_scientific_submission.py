"""Create source-backed AI review proposals without changing frozen releases."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from nutrigenomics.scientific_precheck import (
    build_precheck,
    read_json,
    rebind_candidate_precheck,
    write_precheck,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--authoritative-science", type=Path,
                        help="Optional final-candidate JSON/JSONL science snapshot; only known patches may differ")
    args = parser.parse_args()
    inputs = {name: {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
              for name, path in (("review", args.review), ("facts", args.facts), ("catalog", args.catalog))}
    result = build_precheck(read_json(args.review), read_json(args.facts),
                            read_json(args.catalog), args.evidence_root)
    if args.authoritative_science:
        path = args.authoritative_science
        source = ([json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                  if path.suffix == ".jsonl" else read_json(path))
        result = rebind_candidate_precheck(result, source)
        inputs["authoritative_science"] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    write_precheck(result, args.output)
    manifest = {"schema_version": "nutriomics-scientific-precheck-receipt-v1", "inputs": inputs,
                "summary": result["summary"], "scientific_approval": False,
                "database_writes": False, "R_runs": False, "sealed_answers_read": False,
                "files": [{"path": p.name, "bytes": p.stat().st_size,
                           "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                          for p in sorted(args.output.iterdir()) if p.is_file() and p.name != "manifest.json"]}
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
