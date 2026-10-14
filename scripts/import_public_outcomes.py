"""Import reviewed published aggregate DASH endpoints, not participant data."""

from __future__ import annotations

import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import connect, fetch, insert, validate
from scripts.import_public_expansion import entity

ROOT = Path("research/2026-09-30/expansion")
URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id=9099655&retmode=xml"


def extract(raw):
    document = ET.fromstring(raw)
    articles = document.findall("PubmedArticle")
    if len(articles) != 1 or articles[0].findtext("MedlineCitation/PMID") != "9099655":
        raise ValueError("publication identity mismatch")
    abstract = " ".join("".join(node.itertext()) for node in document.findall(".//AbstractText"))
    lower = abstract.lower()
    if not all(
        anchor in lower
        for anchor in ["459", "random", "5.5", "3.0", "2.8", "1.1", "0.07", "eight weeks"]
    ):
        raise ValueError("reviewed design/endpoint anchors changed")
    corrections = document.findall(".//CommentsCorrections[@RefType='ErratumIn']")
    if corrections:
        raise ValueError("linked erratum requires manual review before extraction")
    return abstract


def main():
    root = ROOT / "outcomes"
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "DASH.manifest.json"
    xml_path = root / "DASH-pubmed.xml"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if hashlib.sha256(xml_path.read_bytes()).hexdigest() != manifest["sha256"]:
            raise ValueError("snapshot checksum mismatch")
    else:
        manifest = fetch(URL, xml_path, 2_000_000)
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    extract(xml_path.read_bytes())
    db = connect(ROOT / "nutrition_evidence.sqlite3")
    release = "PubMed-aggregate:" + manifest["sha256"][:20]
    with db:
        insert(
            db,
            "ng_release",
            {
                "id": release,
                "source": "PubMed-reviewed-aggregate",
                "version": "PMID9099655-snapshot",
                "url": URL,
                "retrieved_at": manifest["retrieved_at"],
                "sha256": manifest["sha256"],
                "local_path": str(xml_path),
                "license": "Numeric facts manually extracted from publisher abstract; no participant data or redistribution license inferred",
                "access_status": "public",
            },
        )
        insert(
            db,
            "ng_study",
            {
                "id": "N13",
                "title": "DASH dietary-pattern randomized feeding trial",
                "population": "Adults with elevated blood pressure; 459 randomized",
                "design": "randomized",
                "n_people": 459,
                "pubmed_id": "9099655",
                "participant_group": "DASH-original-trial",
                "independence_status": "verified",
                "independence_basis": "Named multicenter randomized feeding trial, distinct design and recruitment from the five baseline studies, fish-oil trial and Hall trial",
            },
        )
        insert(
            db,
            "ng_dataset",
            {
                "id": "PMID:9099655",
                "accession": "PMID:9099655",
                "study_id": "N13",
                "release_id": release,
                "modality": "published aggregate blood-pressure outcomes",
                "access_status": "public",
                "ingest_status": "metadata",
                "metadata_json": json.dumps(
                    {
                        "participant_files": "not obtained; BioLINCC request/terms would require approval",
                        "measurement_grain": "published randomized contrast",
                        "trial_participants": 459,
                    }
                ),
            },
        )
        entity(
            db,
            "diet:DASH-combination",
            "diet",
            "DASH combination dietary pattern",
            "local-reviewed",
            "DASH-combination",
            {
                "food_pattern": "fruits, vegetables, low-fat dairy; reduced total/saturated fat",
                "individual_food_effect": False,
            },
        )
        entity(
            db,
            "diet:DASH-fruit-vegetable",
            "diet",
            "DASH fruit-and-vegetable dietary pattern",
            "local-reviewed",
            "DASH-fruit-vegetable",
            {"food_pattern": "fruits and vegetables", "individual_food_effect": False},
        )
        for pattern, identifier, label, effect, exact_p, p_bound in [
            ("combination", "systolic_bp", "systolic blood pressure", -5.5, None, "<0.001"),
            ("combination", "diastolic_bp", "diastolic blood pressure", -3.0, None, "<0.001"),
            ("fruit-vegetable", "systolic_bp", "systolic blood pressure", -2.8, None, "<0.001"),
            ("fruit-vegetable", "diastolic_bp", "diastolic blood pressure", -1.1, 0.07, None),
        ]:
            entity(
                db,
                "outcome:" + identifier,
                "outcome",
                label,
                "local-reviewed",
                identifier,
                {"endpoint_class": "clinical_surrogate", "clinical_disease_endpoint": False},
            )
            insert(
                db,
                "ng_evidence",
                {
                    "id": ("DASH:" if pattern == "combination" else "DASH-fruit-vegetable:")
                    + identifier
                    + ":"
                    + manifest["sha256"][:20],
                    "subject_id": "diet:DASH-" + pattern,
                    "object_id": "outcome:" + identifier,
                    "relation": "randomized_difference",
                    "layer": "health_outcome",
                    "study_id": "N13",
                    "release_id": release,
                    "source_ref": "PMID9099655 abstract RESULTS",
                    "tier": "human_randomized",
                    "inference": "intervention",
                    "organism": "Homo sapiens",
                    "duration": "8 weeks",
                    "comparison": pattern + " diet versus control diet",
                    "effect": effect,
                    "effect_unit": "mmHg",
                    "p_value": exact_p,
                    "value_status": "measured",
                    "uncertainty_type": "not_reported_in_abstract",
                    "context_json": json.dumps(
                        {
                            "p_bound": p_bound,
                            "confidence_interval": "not reported in retrieved abstract",
                            "dose": "whole dietary pattern, not a molecule dose",
                            "contrast_n_people": "not extracted",
                            "study_n_people": 459,
                            "food_or_molecule_benefit_inference": False,
                            "estimand": "difference in blood-pressure reduction between assigned diets",
                        }
                    ),
                },
            )
        errors = validate(db)
        if errors:
            raise ValueError(errors)
    print(
        "Imported 4 randomized aggregate blood-pressure contrasts, including p=0.07; uncertainty unreported, participant data not acquired."
    )
    db.close()


if __name__ == "__main__":
    main()
