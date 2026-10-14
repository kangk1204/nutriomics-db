"""Compare frozen human gene-set membership with an archived official release."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def audit(official: Path, frozen: Path, release: str) -> dict:
    with official.open(encoding="utf-8") as handle:
        reference = {(row[1], row[0]) for row in csv.reader(handle, delimiter="\t")
                     if len(row) >= 6 and row[5] == "Homo sapiens"}
    with frozen.open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        columns = set(reader.fieldnames or [])
        pathway = "pathway_id" if "pathway_id" in columns else "DB_ID"
        gene = "entrez_id" if "entrez_id" in columns else "gene_id"
        if not {pathway, gene} <= columns:
            raise ValueError("Frozen membership must identify pathway and Entrez gene columns")
        observed = {(row[pathway], row[gene]) for row in reader}
    if not reference or not observed:
        raise ValueError("Empty membership cannot establish a biological release")
    return {"release": release,
            "source_url": f"https://download.reactome.org/{release}/NCBI2Reactome_All_Levels.txt",
            "official_file_sha256": digest(official), "frozen_file_sha256": digest(frozen),
            "official_human_memberships": len(reference), "package_memberships": len(observed),
            "intersection": len(reference & observed), "package_only": len(observed - reference),
            "official_only": len(reference - observed), "matched_exactly": observed == reference,
            "parse_columns": [pathway, gene], "method": "exact human pathway/Entrez membership set comparison"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official", type=Path, required=True)
    parser.add_argument("--frozen", type=Path, required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.official, args.frozen, args.release)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    if not result["matched_exactly"]:
        raise SystemExit(1)
