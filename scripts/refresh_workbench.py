"""Acquire enabled source snapshots and build an immutable derivative release.

Adapters are fixed Python functions, never executable strings from a registry.
Sources without an adapter stay archived or explicitly manual. Promotion remains
separate, so an upstream schema change cannot silently replace the live release.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.explorer_build import sha256
from nutrigenomics.source_updates import atomic_json, fetch_source, writer_lock
from nutrigenomics.workbench_build import build_workbench_release
from scripts.collect_hgnc_identifiers import collect as collect_hgnc
from scripts.collect_screen_complete import collect


def refresh(registry, root, parent, release_id, source_ids=None, sidecars=()):
    root = Path(root)
    entries = json.loads(Path(registry).read_text(encoding="utf-8"))["sources"]
    if source_ids and set(source_ids) - {s["id"] for s in entries}:
        raise ValueError("unknown requested source")
    selected = [s for s in entries if not source_ids or s["id"] in source_ids]
    report = {"release_id": release_id, "sources": [], "promoted": False}
    with writer_lock(root):
        inputs = list(sidecars)
        for source in selected:
            if not source.get("enabled"):
                report["sources"].append({"id": source["id"], "status": "manual_or_disabled"})
                continue
            try:
                receipt = fetch_source(source, root / "cache")
                result = {
                    "id": source["id"],
                    "sha256": receipt["sha256"],
                    "status": receipt["update_status"],
                }
                if source["adapter"] == "screen_links":
                    mode = {"screen-crispr": "crispr", "screen-graphreg": "graphreg"}[source["id"]]
                    output = root / "sidecars" / f"{source['id']}-{receipt['sha256'][:16]}.sqlite3"
                    audit = output.with_suffix(".receipt.json")
                    if output.exists():
                        previous = json.loads(audit.read_text())
                        if (
                            sha256(output) != previous["sidecar_sha256"]
                            or receipt["sha256"] != previous["source_sha256"]
                        ):
                            raise ValueError("cached adapter output hash mismatch")
                    else:
                        collect(receipt, output, mode, resume=True)
                    inputs.append(output)
                    result["adapter_status"] = "parsed_and_verified"
                elif source["adapter"] == "hgnc_identifiers":
                    parent_hash = sha256(Path(parent) / "explorer.sqlite")
                    output = (
                        root
                        / "sidecars"
                        / f"hgnc-{receipt['sha256'][:16]}-{parent_hash[:12]}.sqlite3"
                    )
                    if output.exists():
                        audit = json.loads(output.with_suffix(".receipt.json").read_text())
                        if (
                            sha256(output) != audit["sidecar_sha256"]
                            or receipt["sha256"] != audit["source_sha256"]
                            or parent_hash != audit["parent_database_sha256"]
                        ):
                            raise ValueError("cached HGNC adapter hash mismatch")
                    else:
                        collect_hgnc(receipt, output, parent)
                    inputs.append(output)
                    result["adapter_status"] = "exact_identifier_cross_references_verified"
                else:
                    result["adapter_status"] = "archived_only_not_integrated"
                report["sources"].append(result)
            except Exception as error:
                report["sources"].append(
                    {"id": source["id"], "status": "failed", "reason": str(error)}
                )
                atomic_json(root / "refresh-last-run.json", report)
                raise
        if not inputs:
            raise ValueError("no parsed sources; refuse empty derivative")
        manifest = build_workbench_release(
            parent, inputs, root / "releases" / release_id, release_id
        )
        report.update(
            status="built_not_promoted",
            database_sha256=manifest["database_sha256"],
            counts=manifest["counts"],
        )
        atomic_json(root / "refresh-last-run.json", report)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--registry",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config/update_sources.json",
    )
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--from-release", type=Path, required=True)
    p.add_argument("--release-id", required=True)
    p.add_argument("--source", action="append")
    p.add_argument("--sidecar", type=Path, action="append", default=[])
    a = p.parse_args()
    print(
        json.dumps(
            refresh(a.registry, a.root, a.from_release, a.release_id, a.source, a.sidecar), indent=2
        )
    )


if __name__ == "__main__":
    main()
