"""Repeatable update CLI; downloads and promotion are separate explicit stages."""

import argparse
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.source_updates import (
    fetch_source,
    promote_release,
    validate_release,
    writer_lock,
)
from nutrigenomics.workbench_build import build_workbench_release


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["plan", "fetch", "build", "validate", "promote", "rollback"])
    p.add_argument(
        "--registry",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config/update_sources.json",
    )
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--source", action="append")
    p.add_argument("--from-release", type=Path)
    p.add_argument("--sidecar", type=Path, action="append", default=[])
    p.add_argument("--release", type=Path)
    p.add_argument("--release-id")
    args = p.parse_args()
    if args.command in {"plan", "fetch"}:
        registry = json.loads(args.registry.read_text(encoding="utf-8"))
        selected = [s for s in registry["sources"] if not args.source or s["id"] in args.source]
        if args.source and set(args.source) - {s["id"] for s in selected}:
            p.error("unknown source ID")
        if args.command == "plan":
            result = {
                "sources": selected,
                "automatic_publication": False,
                "validation_required_before_promotion": True,
            }
        else:
            result = []
            with writer_lock(args.root):
                for source in selected:
                    if not source.get("enabled"):
                        result.append({"source_id": source["id"], "status": "manual_or_disabled"})
                        continue
                    try:
                        result.append(fetch_source(source, args.root))
                    except (requests.RequestException, OSError, ValueError) as error:
                        result.append(
                            {"source_id": source["id"], "status": "failed", "reason": str(error)}
                        )
                from nutrigenomics.source_updates import atomic_json

                atomic_json(args.root / "last-run.json", result)
            if any(r.get("status") == "failed" for r in result):
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 1
    elif args.command == "build":
        if not args.release_id or not args.sidecar:
            p.error("build requires --release-id and at least one --sidecar")
        with writer_lock(args.root):
            result = build_workbench_release(
                args.from_release,
                args.sidecar,
                args.root / "releases" / args.release_id,
                args.release_id,
            )
    else:
        if not args.release:
            p.error("--release is required")
        result = (
            validate_release(args.release)
            if args.command == "validate"
            else promote_release(args.release, args.root)
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
