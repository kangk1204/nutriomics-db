"""Offline, checksum-verified mechanisms, assay contexts and rights assessments."""

import csv
import gzip
import hashlib
import io
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import backup, connect, coverage, insert, validate
from nutrigenomics.release_rights import export_verified, migrate_rights
from scripts.import_public_expansion import entity
from scripts.ingest_public_matrix import parse_matrix

ROOT = Path("research/2026-09-30/expansion")
CAP = ROOT / "continuation"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def records(text):
    result = []
    for block in text.split("^SAMPLE = ")[1:]:
        row = {"accession": block.splitlines()[0].strip(), "fields": {}}
        for line in block.splitlines()[1:]:
            if line.startswith("!Sample_") and " = " in line:
                key, value = line.split(" = ", 1)
                row["fields"].setdefault(key[8:], []).append(value)
        result.append(row)
    return result


def release(db, row, source, version):
    identifier = source + ":" + row["sha256"][:20]
    insert(
        db,
        "ng_release",
        {
            "id": identifier,
            "source": source,
            "version": version,
            "url": row["url"],
            "retrieved_at": row["retrieved_at"],
            "sha256": row["sha256"],
            "local_path": str(CAP / row["file"]),
            "license": "Redistribution verification pending; internal audit only",
            "access_status": "public",
        },
    )
    return identifier


def evidence(db, identifier, subject, target, relation, layer, source, source_ref, context):
    insert(
        db,
        "ng_evidence",
        {
            "id": identifier,
            "subject_id": subject,
            "object_id": target,
            "relation": relation,
            "layer": layer,
            "release_id": source,
            "source_ref": source_ref,
            "tier": "curated_mechanism",
            "inference": "descriptive",
            "organism": "Homo sapiens",
            "value_status": "qualitative",
            "context_json": json.dumps(context, sort_keys=True),
        },
    )


def mapping(db, identifier, subject, target, relation, source, method):
    insert(
        db,
        "ng_mapping",
        {
            "id": identifier,
            "from_id": subject,
            "to_id": target,
            "relation": relation,
            "release_id": source,
            "source_ref": identifier,
            "method": method,
            "review_status": "verified",
        },
    )


def main():
    path = ROOT / "nutrition_evidence.sqlite3"
    before = ROOT / "before-continuation.sqlite3"
    if not before.exists():
        backup(path, before)
    db = connect(path)
    captures = {}
    for name in ["probe-manifest.json", "metadata-manifest.json", "candidate-manifest.json"]:
        for row in json.loads((CAP / name).read_text()):
            if "sha256" in row:
                if digest(CAP / row["file"]) != row["sha256"]:
                    raise ValueError("Capture checksum mismatch: " + row["file"])
                captures[row["file"]] = row
    participant_report = {}
    with db:
        chem_sources = {}
        for cid in ["38290", "57427"]:
            row = captures[f"CHEBI-{cid}.json"]
            term = json.loads((CAP / row["file"]).read_text())["_embedded"]["terms"][0]
            assert term["obo_id"] == f"CHEBI:{cid}"
            source = release(db, row, "ChEBI-OLS", row["retrieved_at"])
            chem_sources[cid] = source
            entity(
                db,
                f"CHEBI:{cid}",
                "compound",
                term["label"],
                "ChEBI",
                f"CHEBI:{cid}",
                term["annotation"],
            )
        for acc in ["Q02809", "Q9P2J5"]:
            row = captures[f"uniprot-{acc}.json"]
            protein = json.loads((CAP / row["file"]).read_text())
            assert protein["primaryAccession"] == acc and protein["organism"]["taxonId"] == 9606
            source = release(db, row, "UniProtKB", str(protein["entryAudit"]["entryVersion"]))
            gene = protein["genes"][0]["geneName"]["value"]
            entity(db, "UniProt:" + acc, "protein", gene, "UniProtKB", acc, protein["entryAudit"])
            for xref in protein["uniProtKBCrossReferences"]:
                if xref["database"] == "GeneID":
                    identifier = "NCBIGene:" + xref["id"]
                    entity(db, identifier, "gene", gene, "NCBIGene", xref["id"])
                    mapping(
                        db,
                        f"{acc}:gene:{xref['id']}",
                        identifier,
                        "UniProt:" + acc,
                        "related",
                        source,
                        "UniProt curated GeneID cross-reference; gene encodes protein, not exact entities",
                    )
                if xref["database"] == "GO":
                    properties = {p["key"]: p["value"] for p in xref["properties"]}
                    term = properties.get("GoTerm", "")
                    refs = xref.get("evidences", [])
                    if (
                        term.startswith("P:")
                        and refs
                        and properties.get("GoEvidenceType", "").split(":")[0]
                        in {"IDA", "IMP", "IEP"}
                    ):
                        identifier = xref["id"]
                        entity(
                            db,
                            identifier,
                            "pathway",
                            term[2:],
                            "GO",
                            identifier,
                            {
                                "subtype": "biological_process",
                                "not_a_curated_pathway_diagram": True,
                            },
                        )
                        evidence(
                            db,
                            f"{acc}:{identifier}",
                            "UniProt:" + acc,
                            identifier,
                            "annotated_to_biological_process",
                            "pathway",
                            source,
                            row["url"] + "#" + identifier,
                            {
                                "evidence": refs,
                                "GO_evidence_type": properties["GoEvidenceType"],
                                "health_inference": False,
                            },
                        )
            if acc == "Q02809":
                cofactor = next(
                    c
                    for c in protein["comments"]
                    if c["commentType"] == "COFACTOR" and c["cofactors"][0]["name"] == "L-ascorbate"
                )["cofactors"][0]
                assert cofactor["cofactorCrossReference"]["id"] == "CHEBI:38290"
                evidence(
                    db,
                    acc + ":ascorbate",
                    "CHEBI:38290",
                    "UniProt:" + acc,
                    "cofactor_of",
                    "molecular_target",
                    source,
                    row["url"] + "#COFACTOR",
                    {
                        "evidence": cofactor["evidences"],
                        "dose_unknown": True,
                        "binding_affinity_unknown": True,
                    },
                )
            else:
                reaction = next(
                    c["reaction"]
                    for c in protein["comments"]
                    if c["commentType"] == "CATALYTIC ACTIVITY"
                    and c["reaction"].get("ecNumber") == "6.1.1.4"
                )
                assert any(r["id"] == "CHEBI:57427" for r in reaction["reactionCrossReferences"])
                evidence(
                    db,
                    acc + ":leucine",
                    "CHEBI:57427",
                    "UniProt:" + acc,
                    "substrate_of",
                    "molecular_target",
                    source,
                    row["url"] + "#CATALYTIC-ACTIVITY",
                    {
                        "evidence": reaction["evidences"],
                        "reaction": reaction["name"],
                        "dose_unknown": True,
                        "health_inference": False,
                    },
                )
        # InChIKey correspondence is representation-specific: acid != ascorbate anion.
        for cid, pubchem, relation in [
            ("38290", "54670067", "related"),
            ("57427", "6106", "exact"),
        ]:
            existing = db.execute(
                "SELECT id FROM ng_entity WHERE namespace='PubChem-CID' AND identifier=?",
                (pubchem,),
            ).fetchone()
            if existing:
                properties = json.loads(
                    db.execute(
                        "SELECT metadata_json FROM ng_entity WHERE id=?", (existing["id"],)
                    ).fetchone()[0]
                )
                term = json.loads((CAP / f"CHEBI-{cid}.json").read_text())["_embedded"]["terms"][0]
                chebi_key = term["annotation"]["inchi_key_string"][0]
                pubchem_key = properties["InChIKey"]
                if relation == "exact":
                    assert pubchem_key == chebi_key
                else:
                    assert (
                        pubchem_key != chebi_key
                        and pubchem_key.split("-")[:2] == chebi_key.split("-")[:2]
                    )
                mapping(
                    db,
                    "CHEBI:" + cid + ":PubChem:" + pubchem,
                    existing["id"],
                    "CHEBI:" + cid,
                    relation,
                    chem_sources[cid],
                    "Reviewed standard InChIKey: same leucine key; ascorbate differs in protonation/charge and is only related",
                )
        # FISHGASTRO paired human matrix.
        row = captures["GSE15284_series_matrix.txt.gz"]
        raw = (CAP / row["file"]).read_bytes()
        header = gzip.decompress(raw).decode().split("!series_matrix_table_begin")[0]
        fields = {}
        for record in csv.reader(io.StringIO(header), delimiter="\t"):
            if record and record[0] in [
                "!Sample_geo_accession",
                "!Sample_title",
                "!Sample_data_processing",
            ]:
                fields[record[0]] = record[1:]
        assays = []
        for accession, title, processing in zip(
            fields["!Sample_geo_accession"],
            fields["!Sample_title"],
            fields["!Sample_data_processing"],
            strict=True,
        ):
            match = re.fullmatch(
                r"COLON_(\d+)_(COD|SALMON)_(baseline|6months_after_intervention)", title
            )
            if not match or "GCRMA" not in processing:
                raise ValueError("Unreviewed FISHGASTRO sample")
            assays.append(
                {
                    "accession": accession,
                    "participant_id": match[1],
                    "diet": match[2],
                    "time": match[3],
                    "processing": processing,
                }
            )
        people = {a["participant_id"] for a in assays}
        assert len(people) == 28 and len(assays) == 56
        for person in people:
            paired = [a for a in assays if a["participant_id"] == person]
            assert {a["time"] for a in paired} == {
                "baseline",
                "6months_after_intervention",
            } and len({a["diet"] for a in paired}) == 1
        frame, meta = parse_matrix(raw, "GSE15284", {a["accession"] for a in assays})
        meta.update(
            n_people=28,
            n_people_basis="28 reviewed subject IDs, each with baseline and six-month biopsy",
            processing="GCRMA source estimates; local conversion only",
            value_unit="Normalized expression estimate; source header does not specify log base",
            raw_or_derived="derived",
            assays=assays,
            total_values=meta["n_features"] * 56,
        )
        output = CAP / "GSE15284.parquet"
        if not output.exists():
            frame.to_parquet(output, index=False)
        meta["artifact"] = {
            "path": str(output),
            "sha256": digest(output),
            "input_sha256": row["sha256"],
        }
        source = release(db, row, "GEO-matrix", row["retrieved_at"])
        dataset = db.execute("SELECT * FROM ng_dataset WHERE accession='GSE15284'").fetchone()
        metadata = json.loads(dataset["metadata_json"])
        metadata["measurement_artifact"] = meta
        db.execute(
            "UPDATE ng_dataset SET ingest_status=?,metadata_json=? WHERE id=?",
            ("measurements", json.dumps(metadata, sort_keys=True), dataset["id"]),
        )
        db.execute(
            "UPDATE ng_study SET n_people=?,independence_status=?,independence_basis=? WHERE id=?",
            (
                28,
                "verified",
                "28 unique source subject IDs with paired timepoints; separate FISHGASTRO study. No global participant linkage available.",
                dataset["study_id"],
            ),
        )
        participant_report["GSE15284"] = meta
        # Review candidate RNA-seq contexts, without inventing treatment/time labels.
        for accession in ["GSE323980", "GSE46761"]:
            row = captures[accession + "-samples.txt"]
            samples = records((CAP / row["file"]).read_text())
            source = release(db, row, "GEO-sample-metadata", row["retrieved_at"])
            assays = []
            for sample in samples:
                fields = sample["fields"]
                if accession == "GSE323980":
                    match = re.fullmatch(r"Library name: (D\d+)([AB])", fields["description"][0])
                    if not match:
                        raise ValueError("Unexpected donor label")
                    assays.append(
                        {
                            "accession": sample["accession"],
                            "participant_id": match[1],
                            "visit_code": match[2],
                            "processing": fields["data_processing"],
                            "diet_arm": None,
                            "visit_meaning": None,
                        }
                    )
                else:
                    chars = dict(
                        v.split(": ", 1) for v in fields["characteristics_ch1"] if ": " in v
                    )
                    assays.append(
                        {
                            "accession": sample["accession"],
                            "participant_id": chars["subject"],
                            "time": chars["time"],
                            "diet": chars["diet"],
                            "organism": fields["organism_ch1"][0],
                            "processing": fields["data_processing"],
                        }
                    )
            meta = {
                "n_assays": len(assays),
                "n_people": len({a["participant_id"] for a in assays}),
                "assays": assays,
                "metadata_release_id": source,
                "independence_limit": "Distinct study protocol; cannot exclude overlap across unrelated repositories without linkage",
                "analysis_ready": False,
                "reason": "Treatment/visit codebook missing"
                if accession == "GSE323980"
                else "Microbial gene IDs need audited genome/annotation mapping; repeated participants across diets",
            }
            dataset = db.execute(
                "SELECT * FROM ng_dataset WHERE accession=?", (accession,)
            ).fetchone()
            metadata = json.loads(dataset["metadata_json"])
            metadata["sample_audit"] = meta
            db.execute(
                "UPDATE ng_dataset SET metadata_json=? WHERE id=?",
                (json.dumps(metadata, sort_keys=True), dataset["id"]),
            )
            db.execute(
                "UPDATE ng_study SET n_people=?,independence_status=?,independence_basis=? WHERE id=?",
                (meta["n_people"], "verified", meta["independence_limit"], dataset["study_id"]),
            )
            participant_report[accession] = meta
    migrate_rights(db)
    policy = ROOT / "snapshots/fdc_policy.html"
    assert "CC0" in policy.read_text() or "public domain" in policy.read_text().lower()
    now = datetime.now(UTC).isoformat()
    with db:
        for row in db.execute("SELECT * FROM ng_release").fetchall():
            verified = row["source"] == "USDA-FDC"
            db.execute(
                "INSERT OR IGNORE INTO ng_rights VALUES (?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    "verified" if verified else "quarantined",
                    "CC0-1.0" if verified else None,
                    "https://fdc.nal.usda.gov/api-guide/" if verified else None,
                    digest(policy) if verified else None,
                    "USDA FoodData Central" if verified else row["source"],
                    "Source-specific CC0/public domain statement captured"
                    if verified
                    else "Public access verified; redistribution and additional conditions unverified. Excluded from shareable export.",
                    now,
                ),
            )
        db.execute(
            "UPDATE ng_rights SET policy_url=NULL,policy_sha256=NULL WHERE status='quarantined'"
        )
    report = coverage(db)
    report["participant_audits"] = participant_report
    report["rights_counts"] = dict(
        db.execute("SELECT status,count(*) FROM ng_rights GROUP BY status")
    )
    report["verified_food_to_health_paths"] = 0
    report["mechanism_limit"] = (
        "Two biochemical roles and experimental GO biological-process annotations; no causal health claim. GO processes are not curated pathway diagrams."
    )
    report["user_dependent_blockers"] = [
        "NHANES/Reactome/ChEMBL agreements remain unaccepted; alternative sources can be developed without these."
    ]
    errors = validate(db)
    if errors:
        raise ValueError(errors)
    db.close()
    (ROOT / "reports/continuation_coverage.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    exported = ROOT / "shareable_food_composition_v2.sqlite3"
    if not exported.exists():
        export_verified(path, exported)
    print(json.dumps({k: v for k, v in report.items() if k != "participant_audits"}, indent=2))


if __name__ == "__main__":
    main()
