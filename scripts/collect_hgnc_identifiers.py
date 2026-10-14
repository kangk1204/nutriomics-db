"""Official approved HGNC cross-references, with no symbol-based identity inference."""

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import connect, insert, migrate
from nutrigenomics.explorer_build import canonical, read_only, sha256
from nutrigenomics.source_updates import atomic_json

LICENSE_URL = "https://www.genenames.org/about/license/"


def collect(snapshot, output, parent):
    output, raw = Path(output), Path(snapshot["object_path"])
    if output.exists() or output.with_suffix(".building").exists():
        raise FileExistsError("immutable identifier sidecar")
    if sha256(raw) != snapshot["sha256"]:
        raise ValueError("HGNC source hash mismatch")
    with read_only(Path(parent) / "explorer.sqlite") as db:
        existing = {r["id"] for r in db.execute("SELECT id FROM node WHERE kind='gene'")}
    counts, by_entrez, by_ensembl = Counter(), defaultdict(set), defaultdict(set)
    rows = []
    with raw.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {"hgnc_id", "symbol", "status", "entrez_id", "ensembl_gene_id"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("HGNC schema changed")
        for number, row in enumerate(reader, 2):
            counts["raw_rows"] += 1
            if row["status"] != "Approved":
                counts["not_approved"] += 1
                continue
            if not (
                re.fullmatch(r"HGNC:\d+", row["hgnc_id"])
                and re.fullmatch(r"\d+", row["entrez_id"])
                and re.fullmatch(r"ENSG\d+", row["ensembl_gene_id"])
            ):
                counts["missing_or_noncanonical_exact_id"] += 1
                continue
            a, b = "NCBIGene:" + row["entrez_id"], "Ensembl:" + row["ensembl_gene_id"]
            by_entrez[a].add((b, row["hgnc_id"]))
            by_ensembl[b].add((a, row["hgnc_id"]))
            rows.append((number, row, a, b))
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_suffix(".building")
    db = connect(staging)
    migrate(db)
    sid = "HGNC-identifiers:" + snapshot["sha256"][:16]
    insert(
        db,
        "ng_release",
        {
            "id": sid,
            "source": "HGNC-official-identifiers",
            "version": snapshot["sha256"],
            "url": snapshot["url"],
            "retrieved_at": snapshot["retrieved_at"],
            "sha256": snapshot["sha256"],
            "local_path": str(raw),
            "license": "CC0-1.0",
            "access_status": "public",
        },
    )
    seen, inserted_entities = set(), set()
    for number, row, a, b in rows:
        if len(by_entrez[a]) != 1 or len(by_ensembl[b]) != 1:
            counts["ambiguous_exact_mapping"] += 1
            continue
        if a not in existing or b not in existing:
            counts["one_or_both_identifiers_absent_in_parent"] += 1
            continue
        if (a, b) in seen:
            counts["duplicate_mapping"] += 1
            continue
        seen.add((a, b))
        for identifier, namespace in [(a, "NCBIGene"), (b, "Ensembl")]:
            if identifier in inserted_entities:
                continue
            insert(
                db,
                "ng_entity",
                {
                    "id": identifier,
                    "kind": "gene",
                    "label": row["symbol"],
                    "namespace": namespace,
                    "identifier": identifier.split(":", 1)[1],
                    "organism": "Homo sapiens",
                    "metadata_json": canonical({"hgnc_id": row["hgnc_id"]}),
                },
            )
            inserted_entities.add(identifier)
        context = {
            "review_status": "source_checked",
            "export_allowed": True,
            "export_policy_proof": {
                "license": "CC0-1.0",
                "url": LICENSE_URL,
                "checked_at": "2026-10-09",
            },
            "source_raw_fields": {k: row[k] for k in sorted(required)},
            "source_tsv_row": number,
            "hgnc_id": row["hgnc_id"],
            "gene_symbol": row["symbol"],
            "mapping_scope": "one-to-one approved exact identifier cross-reference; both native IDs present",
            "limitations": [
                "Identifier equivalence does not establish a dietary effect or pool statistics."
            ],
        }
        insert(
            db,
            "ng_evidence",
            {
                "id": f"HGNC:{row['hgnc_id']}:{snapshot['sha256'][:16]}",
                "subject_id": a,
                "object_id": b,
                "relation": "verified_gene_identifier_cross_reference",
                "layer": "molecular_identity",
                "release_id": sid,
                "source_ref": f"{snapshot['url']}#TSV-row-{number}",
                "tier": "identity",
                "inference": "descriptive",
                "organism": "Homo sapiens",
                "value_status": "qualitative",
                "context_json": canonical(context),
            },
        )
        counts["verified_exact_cross_references"] += 1
    db.commit()
    if (
        db.execute("PRAGMA foreign_key_check").fetchall()
        or db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
    ):
        raise ValueError("HGNC sidecar failed integrity checks")
    db.close()
    os.replace(staging, output)
    receipt = {
        "source_sha256": snapshot["sha256"],
        "sidecar_sha256": sha256(output),
        "collector_code_sha256": sha256(Path(__file__)),
        "parent_database_sha256": sha256(Path(parent) / "explorer.sqlite"),
        "counts": dict(counts),
        "scope": "all upstream rows audited; cross-reference only existing exact parent gene IDs",
    }
    atomic_json(output.with_suffix(".receipt.json"), receipt)
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--parent", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            collect(json.loads(args.receipt.read_text()), args.output, args.parent), indent=2
        )
    )
