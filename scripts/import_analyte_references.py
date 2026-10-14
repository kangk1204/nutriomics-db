"""Related analyte references and nominal formulation components; no dose conversion."""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.context_joins import migrate_context, put_context
from nutrigenomics.expansion import connect, insert, validate
from scripts.import_public_expansion import entity
from scripts.import_trial_context import CAP, ROOT, source


def main():
    captures = {}
    for manifest in [
        "definitions-manifest.json",
        "trial-context-manifest.json",
        "cis-EPA-manifest.json",
    ]:
        rows = json.loads((CAP / manifest).read_text())
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:
            if "sha256" in row:
                if hashlib.sha256((CAP / row["file"]).read_bytes()).hexdigest() != row["sha256"]:
                    raise ValueError("Reference checksum mismatch")
                captures[row["file"]] = row
    db = connect(ROOT / "nutrition_evidence.sqlite3")
    migrate_context(db)
    with db:
        method_release = source(
            db, captures["USDA-SR28-documentation.pdf"], "USDA-SR28-method-documentation"
        )
        for nutrient, compound, analyte in [
            ("1162", "54670067", "Vitamin C, total ascorbic acid"),
            ("1210", "6305", "Tryptophan"),
            ("1211", "6288", "Threonine"),
            ("1213", "6106", "Leucine"),
        ]:
            definition = db.execute(
                "SELECT * FROM ng_nutrient_definition WHERE fdc_id=?", (nutrient,)
            ).fetchone()
            if not definition or definition["source_name"].casefold() != analyte.casefold():
                raise ValueError("Source analyte definition mismatch: " + nutrient)
            insert(
                db,
                "ng_mapping",
                {
                    "id": "USDA-assay-reference:" + nutrient + ":" + compound,
                    "from_id": "FDC-nutrient:" + nutrient,
                    "to_id": "PubChem:" + compound,
                    "relation": "related",
                    "release_id": method_release,
                    "source_ref": "USDA-SR28:Vitamin C"
                    if nutrient == "1162"
                    else "USDA-SR28:Amino acids",
                    "method": json.dumps(
                        {
                            "mapping_kind": "analyte_reference_only",
                            "quantity_transfer_allowed": False,
                            "exact_chemical_identity": False,
                            "free_molecule_concentration": False,
                            "measured_stereochemistry_not_established": True,
                            "assay_context": "Total vitamin C, reduced/oxidized analytical coverage varies"
                            if nutrient == "1162"
                            else "Hydrolysis-derived amino-acid composition; free amino acids not separately measured",
                        },
                        sort_keys=True,
                    ),
                    "review_status": "verified",
                },
            )
        for label, filename, expected, dose in [
            ("EPA", "PubChem-cis-EPA.json", 446284, 1.7),
            ("DHA", "PubChem-DHA.json", 445580, 1.2),
        ]:
            properties = json.loads((CAP / filename).read_text())["PropertyTable"]["Properties"][0]
            if (
                properties["CID"] != expected
                or "Z" not in properties["IUPACName"]
                or "E," in properties["IUPACName"]
            ):
                raise ValueError("Biological reference stereochemistry mismatch")
            structure = source(db, captures[filename], "PubChem-structure-reference")
            entity(
                db,
                "PubChem:" + str(expected),
                "compound",
                properties["Title"],
                "PubChem",
                str(expected),
                properties,
            )
            for study in ["N06", "N06-clinical-RCT"]:
                arm = db.execute(
                    "SELECT * FROM ng_arm WHERE id=?", (study + ":FISHOIL",)
                ).fetchone()
                put_context(
                    db,
                    "ng_arm_component",
                    {
                        "id": study + ":FISHOIL:" + label,
                        "arm_id": arm["id"],
                        "compound_id": "PubChem:" + str(expected),
                        "source_label": label,
                        "nominal_dose": dose,
                        "dose_unit": "g/day nominal fatty-acid equivalents",
                        "release_id": arm["release_id"],
                        "structure_release_id": structure,
                        "chemical_form_measured": 0,
                        "molecule_effect_attribution": 0,
                        "context_json": json.dumps(
                            {
                                "component_source": "GEO summary or PMID21429719 primary abstract",
                                "reference_structure_is_not_assayed_form": True,
                                "ester_free_acid_form_unknown": True,
                                "nominal_dose_not_analytic_batch_measurement": True,
                                "individual_component_health_effect_not_identified": True,
                            },
                            sort_keys=True,
                        ),
                    },
                )
        if validate(db):
            raise ValueError(validate(db))
    report = {
        "related_analyte_references": 4,
        "nominal_formulation_components": 4,
        "exact_food_compound_mappings": 0,
        "strict_quantity_transfer": False,
        "molecule_effect_attribution": False,
        "rejected_name_only_EPA_CID": 5282847,
        "correct_cis_EPA_reference_CID": 446284,
        "source_rights_quarantined": True,
    }
    (ROOT / "reports/analyte_reference_validation.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
