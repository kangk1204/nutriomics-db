"""Inventory a release; write only when explicitly targeting derived staging."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from nutrigenomics.release_contract import build_release_contract, install_release_contract


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--release-id")
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--parent-manifest", type=Path)
    parser.add_argument("--quantitative-database", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--install-in-staging", action="store_true",
                        help="Write metadata into an explicitly new derived staging DB, before hashing")
    args = parser.parse_args()
    load = lambda p: json.loads(p.read_text(encoding="utf-8")) if p else None
    mode = "rw" if args.install_in_staging else "ro"
    db = sqlite3.connect(args.database.resolve().as_uri() + "?mode=" + mode, uri=True)
    quantitative = (sqlite3.connect(args.quantitative_database.resolve().as_uri() + "?mode=ro", uri=True)
                    if args.quantitative_database else None)
    try:
        if not args.install_in_staging:
            db.execute("PRAGMA query_only=ON")
        contract = build_release_contract(db, release_id=args.release_id, previous_metadata=load(args.metadata),
                                          manifest=load(args.parent_manifest), quantitative_db=quantitative)
        if args.install_in_staging:
            install_release_contract(db, contract)
            db.commit()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(contract, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"release_id": contract["release_id"], "installed": args.install_in_staging,
                          "counts": contract["counts"]}, ensure_ascii=False))
    finally:
        db.close()
        if quantitative is not None:
            quantitative.close()


if __name__ == "__main__":
    main()
