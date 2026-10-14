"""Fetch only the fixed allowlist; refresh requires a new snapshot directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import fetch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=Path("config/public_expansion_plan.json"))
    parser.add_argument(
        "--output", type=Path, default=Path("research/2026-09-30/expansion/snapshots")
    )
    args = parser.parse_args()
    plan_bytes = args.plan.read_bytes()
    plan = json.loads(plan_bytes)
    if len(plan["geo_series"]) > 12 or len(plan["fdc_ids"]) > 5 or len(plan["pubchem_cids"]) > 10:
        raise ValueError("bounded plan cardinality exceeded")
    manifest_path = args.output / "manifest.json"
    if manifest_path.exists():
        raise ValueError("snapshot already exists; replay offline or choose a new output directory")
    requests = []
    for acc in plan["geo_series"]:
        if not acc.startswith("GSE") or not acc[3:].isdigit():
            raise ValueError("invalid GEO accession")
        requests.append(
            (
                acc + ".soft",
                f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={acc}&targ=self&form=text&view=full",
                "GEO",
                "public",
                "NCBI molecular data policy; submitter rights may remain",
            )
        )
    for identifier in plan["fdc_ids"]:
        requests.append(
            (
                f"fdc-{int(identifier)}.json",
                f"https://api.nal.usda.gov/fdc/v1/food/{int(identifier)}?api_key=DEMO_KEY",
                "USDA-FDC",
                "public",
                "CC0-1.0",
            )
        )
    for identifier in plan["pubchem_cids"]:
        requests.append(
            (
                f"pubchem-{int(identifier)}.json",
                f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{int(identifier)}/property/Title,MolecularFormula,MolecularWeight,InChIKey/JSON",
                "PubChem",
                "public",
                "NCBI policy; computed identity properties only, no licensed third-party annotations",
            )
        )
    for key, url in plan["metadata_urls"].items():
        suffix = ".json" if key == "wastyk_repository" else ".html"
        requests.append(
            (
                key + suffix,
                url,
                key,
                "metadata_only",
                "Source-specific; metadata inspection only, no grant of publication reuse",
            )
        )
    manifest = {
        "plan_version": plan["version"],
        "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "max_total_bytes": plan["max_total_bytes"],
        "files": [],
        "failures": [],
        "blocked_sources": plan["blocked_sources"],
    }
    used = 0
    args.output.mkdir(parents=True, exist_ok=True)
    for filename, url, source, access, license_text in requests:
        if used >= plan["max_total_bytes"]:
            manifest["failures"].append({"file": filename, "reason": "total budget exhausted"})
            continue
        try:
            row = fetch(url, args.output / filename, min(5_000_000, plan["max_total_bytes"] - used))
            used += row["bytes"]
            row.update(
                file=filename,
                source=source,
                access_status=access,
                license=license_text,
                version=plan["version"],
            )
            manifest["files"].append(row)
            print(filename, row["bytes"], flush=True)
        except (OSError, ValueError) as error:
            manifest["failures"].append(
                {"file": filename, "reason": f"{type(error).__name__}: {error}"}
            )
            print(filename, "FAILED", type(error).__name__, flush=True)
        # At most one public repository request per second, no retry loop.
        time.sleep(1)
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("total_bytes", used, "failures", len(manifest["failures"]))


if __name__ == "__main__":
    main()
