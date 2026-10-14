"""Recompute connected coverage and precise gaps from the saved sidecar."""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.context_joins import (
    analyte_reference_paths,
    trial_context_health,
    validate_context,
)
from nutrigenomics.expansion import connect, coverage
from nutrigenomics.platform_mapping import validate_platform
from scripts.import_trial_context import ROOT


def main():
    db = connect(ROOT / "nutrition_evidence.sqlite3")
    report = coverage(db)
    report.update(
        context_tables={
            name: db.execute("SELECT count(*) FROM " + name).fetchone()[0]
            for name in [
                "ng_nutrient_definition",
                "ng_arm",
                "ng_arm_component",
                "ng_assay_context",
                "ng_study_link",
                "ng_feature_gene",
            ]
        },
        analyte_mechanism_reference_paths=analyte_reference_paths(db),
        fishgastro_aggregate_context=trial_context_health(db, "GSE15284"),
        resveratrol_qualitative_parent_context=trial_context_health(db, "GSE32357"),
        candidate_fish_oil_health_join=trial_context_health(db, "GSE27385"),
        participant_matched_health_chains=0,
        complete_food_molecule_health_chains=0,
        molecule_attributed_health_effects=0,
        context_validation_errors=validate_context(db),
        platform_validation_errors=validate_platform(db),
        normalized_participants=db.execute(
            "SELECT count(DISTINCT participant_key) FROM ng_assay_context"
        ).fetchone()[0],
        sidecar_sha256=hashlib.sha256(
            (ROOT / "nutrition_evidence.sqlite3").read_bytes()
        ).hexdigest(),
    )
    (ROOT / "reports/connected_coverage.json").write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                "evidence": report["evidence_layers"],
                "context_tables": report["context_tables"],
                "analyte_mechanism_reference_paths": len(
                    report["analyte_mechanism_reference_paths"]
                ),
                "fishgastro_aggregate_context_records": len(report["fishgastro_aggregate_context"]),
                "candidate_fish_oil_health_join": len(report["candidate_fish_oil_health_join"]),
                "validation_errors": report["validation_errors"]
                + report["context_validation_errors"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
