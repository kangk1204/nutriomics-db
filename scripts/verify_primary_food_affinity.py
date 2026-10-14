"""Bounded official record capture; independent verification does not clear redistribution."""

import hashlib
import json
import re
import shutil
import sqlite3
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path("research/2026-09-30/expansion/repository-reconciliation")
CAP = ROOT / "primary-affinity"
CAP.mkdir(exist_ok=True)


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.data = []

    def handle_data(self, data):
        self.data.append(data)


def capture(url, name, require_record=True):
    path = CAP / name
    if not path.exists():
        if shutil.disk_usage(CAP).free < 1_000_000_000:
            raise ValueError("Storage reserve")
        request = urllib.request.Request(
            url, headers={"User-Agent": "NutriGenomics provenance audit/1.0"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            if urllib.parse.urlparse(response.url).hostname not in {
                "www.bindingdb.org",
                "bindingdb.org",
            }:
                raise ValueError("Unapproved redirect")
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("Per-record response cap")
        path.write_bytes(raw)
    raw = path.read_bytes()
    parser = Text()
    parser.feed(raw.decode("utf-8", errors="replace"))
    text = " ".join(" ".join(parser.data).split())
    if require_record and "Reaction Details" not in text and "InChI Key" not in text:
        raise ValueError("Record not found or challenge page")
    return text, {
        "url": url,
        "file": name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "verified_utc": datetime.now(UTC).isoformat(),
    }


def parse_kd(text):
    match = re.search(r"\bKd\s+([0-9.eE+-]+)\s*(?:±\s*(n/a|[0-9.eE+-]+))?\s*nM", text)
    return (float(match.group(1)), match.group(2)) if match else (None, None)


def imported_assay_label(text):
    technique = text.split("Meas. Tech.", 1)[-1].split("Kd", 1)[0]
    match = re.search(r"ChE(?:MBL|BML)_\d+(?:\s*\((CHEMBL\d+)\))?", technique)
    return match.group(0) if match else None


if __name__ == "__main__":
    with sqlite3.connect(
        (ROOT / "quarantined_food_affinity_v2.sqlite3").resolve().as_uri() + "?mode=ro", uri=True
    ) as db:
        rows = [
            json.loads(row[0])
            for row in db.execute("SELECT source_row_json FROM affinity ORDER BY record")
        ]
    result = []
    monomers = {}
    for row in rows:
        record = row["reactant_set_id"]
        entry = row["assay_id"].split("_")[0]
        url = "https://www.bindingdb.org/rwd/jsp/dbsearch/Summary_ki.jsp?" + urllib.parse.urlencode(
            {
                "energyterm": "kcal/mole",
                "entryid": entry,
                "icunit": "nM",
                "ki_result_id": record,
                "kiunit": "nM",
                "reactant_set_id": record,
            }
        )
        audit = {
            "record": record,
            "expected_kd_nm": float(row["value_nm"]),
            "origin": row["curation_source"],
        }
        try:
            text, source = capture(url, record + ".html")
            audit["source"] = source
            kd_value, kd_uncertainty = parse_kd(text)
            sequence = re.search(r"\bSequence:\s*([A-Z]+)\s*(?:Blast|Inhibitor)", text)
            molecule = row["bindingdb_monomer_id"]
            if molecule not in monomers:
                monomers[molecule] = capture(
                    "https://www.bindingdb.org/rwd/bind/chemsearch/marvin/MolStructure.jsp?monomerid="
                    + molecule,
                    "monomer-" + molecule + ".html",
                )
            molecule_text, molecule_source = monomers[molecule]
            audit["identity_source"] = molecule_source
            audit["official_reported_uncertainty"] = kd_uncertainty
            audit["uncertainty_type"] = "not_defined_in_record"
            ph = re.search(r"\bpH\s+([0-9.]+)\s*±", text)
            temperature = re.search(r"\bTemperature\s+([0-9.]+)\s*±\s*(?:n/a|[0-9.]+)\s+K\b", text)
            audit["official_ph"] = float(ph.group(1)) if ph else None
            audit["official_temperature_kelvin"] = (
                float(temperature.group(1)) if temperature else None
            )
            audit["checks"] = {
                "kd_nm": kd_value == float(row["value_nm"]),
                "ligand_id": bool(re.search(r"Ligand\s+BDBM" + molecule + r"\b", text)),
                "full_inchikey": row["bindingdb_inchikey"] in molecule_text,
                "stored_sequence": sequence is not None
                and sequence.group(1) == row["protein_sequence"],
                "target_name": row["target_name"] in text,
                "species": {
                    "Homo sapiens": "Human",
                    "Mus musculus": "Mouse",
                    "Ovis aries": "Sheep",
                }.get(row["organism"], row["organism"])
                in text,
            }
            audit["chembl_ids"] = sorted(set(re.findall(r"CHEMBL\d+", text)))
            imported_assay = imported_assay_label(text)
            audit["official_imported_assay_id"] = imported_assay
            audit["official_citation"] = (
                text.split("Citation", 1)[-1].split("More Info.", 1)[0].strip()
            )
            audit["review_source_requires_original_experiment_lookup"] = row["pmid"] in {
                "36490325",
                "38295689",
                "39746038",
            }
            audit["experimental_construct_verified_from_paper"] = False
            assay_number = row["assay_id"].split("_")[1]
            assay_text, assay_source = capture(
                "https://www.bindingdb.org/rwd/jsp/dbsearch/assay.jsp?"
                + urllib.parse.urlencode({"assayid": assay_number, "entryid": entry}),
                "assay-" + row["assay_id"] + ".html",
                False,
            )
            audit["assay_source"] = assay_source
            audit["official_assay_description"] = (
                assay_text.split("Assay Method Information", 1)[-1]
                .split("Affinity data for this assay", 1)[0]
                .strip()
            )
            audit["assay_page_status"] = (
                "no_data_available" if "No data available" in assay_text else "readable"
            )
            audit["origin_confirmation"] = (
                "official_ChEMBL_identifier_in_reaction_record"
                if imported_assay
                else "repository_native_BindingDB_origin_consistent_with_record_not_explicitly_labeled"
            )
            audit["license"] = "CC-BY-SA-3.0" if row["curation_source"] == "ChEMBL" else "CC-BY-3.0"
            audit["license_source"] = "https://www.bindingdb.org/rwd/bind/info.jsp"
            audit["record_match"] = all(audit["checks"].values())
            audit["status"] = (
                "official_record_matched_not_original_paper_replicated"
                if audit["record_match"]
                else "official_record_partial_or_discrepant"
            )
        except (OSError, ValueError) as exc:
            audit["status"] = "unresolved"
            audit["error"] = str(exc)
        audit["redistribution"] = "quarantined; origin_license_identified_release_not_authorized"
        result.append(audit)
        (CAP / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
        print(record, audit["status"], audit.get("checks"), flush=True)
