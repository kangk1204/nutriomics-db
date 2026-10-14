"""Build an independent Explorer release; does not update the live pointer."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.explorer_build import build_release, upgrade_omics_release, upgrade_release
from nutrigenomics.explorer_scientific_build import upgrade_scientific_release


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph-db", type=Path)
    parser.add_argument(
        "--from-release",
        type=Path,
        help="Validated parent for a source-status-only derivative upgrade",
    )
    parser.add_argument(
        "--add-omics-references",
        action="store_true",
        help="Index exact existing CpG effect annotations in a fresh derivative",
    )
    parser.add_argument(
        "--add-scientific-matching",
        action="store_true",
        help="Index original intervention/analysis metadata; no effect copies",
    )
    parser.add_argument(
        "--curation",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config/explorer_evidence_curations.json",
    )
    parser.add_argument("--core-db", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument(
        "--scope",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config/explorer_scope.json",
    )
    parser.add_argument("--ctd-dir", type=Path)
    parser.add_argument("--literature-db", type=Path)
    args = parser.parse_args()
    if args.from_release:
        if args.graph_db or args.ctd_dir or args.literature_db:
            parser.error(
                "A derivative --from-release cannot silently replace graph/CTD/literature sources"
            )
        if args.add_scientific_matching:
            if args.add_omics_references:
                parser.error("Derivative steps must use separate immutable releases")
            result = upgrade_scientific_release(
                args.from_release, args.core_db, args.output_dir, args.release_id, args.curation
            )
        else:
            function = upgrade_omics_release if args.add_omics_references else upgrade_release
            result = function(args.from_release, args.core_db, args.output_dir, args.release_id)
    else:
        if args.add_omics_references or args.add_scientific_matching:
            parser.error("Derivative flags require --from-release")
        if not args.graph_db:
            parser.error("--graph-db is required unless --from-release is supplied")
        result = build_release(
            args.graph_db,
            args.core_db,
            args.output_dir,
            args.release_id,
            args.scope,
            ctd_dir=args.ctd_dir,
            literature_db=args.literature_db,
        )
    print(
        json.dumps(
            {
                "release_id": result["release_id"],
                "counts": result["counts"],
                "database_sha256": result["database_sha256"],
                "output_dir": str(args.output_dir),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
