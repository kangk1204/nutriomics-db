"""Secondary outcomes of the existing FISHGASTRO trial, not a new cohort."""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.context_joins import trial_context_health
from nutrigenomics.expansion import connect, validate
from scripts.import_trial_context import (
    CAP,
    ROOT,
    abstract,
    aggregate,
    outcome,
    primary_articles,
    source,
)


def main():
    row = json.loads((CAP / "secondary-manifest.json").read_text())[0]
    if hashlib.sha256((CAP / row["file"]).read_bytes()).hexdigest() != row["sha256"]:
        raise ValueError("Secondary source checksum mismatch")
    article = primary_articles(CAP / row["file"])["20032491"]
    content = abstract(article)
    for snippet in [
        "salmon (-0.5 mg/L; 95% CI -0.9, -0.2)",
        "cod (-0.4 mg/L; 95% CI -0.7, 0.0)",
        "n = 161",
        "n = 52",
        "None of the inflammation markers",
    ]:
        if snippet not in content:
            raise ValueError("Secondary source mismatch: " + snippet)
    registry = json.loads((CAP / "FISHGASTRO-NCT00145015.json").read_text())
    refs = registry["protocolSection"]["referencesModule"]["references"]
    if not any(ref["pmid"] == "20032491" for ref in refs):
        raise ValueError("Secondary publication is not linked by the trial registry")
    db = connect(ROOT / "nutrition_evidence.sqlite3")
    with db:
        release = source(db, row, "PubMed-primary-FISHGASTRO-secondary")
        outcome(
            db,
            "ClinicalOutcome:CRP:FISHGASTRO",
            "Serum C-reactive protein",
            "circulating_biomarker",
        )
        outcome(
            db,
            "TrialOutcome:FISHGASTRO:local-inflammation",
            "Exploratory local inflammation marker panel",
            "tissue_surrogate",
        )
        for label, estimate, low, high in [("SALMON", -0.5, -0.9, -0.2), ("COD", -0.4, -0.7, 0.0)]:
            aggregate(
                db,
                "PMID20032491:" + label + ":CRP",
                "TrialFood:NCT00145015:" + label,
                "ClinicalOutcome:CRP:FISHGASTRO",
                "N07-parent-RCT",
                release,
                "20032491",
                {
                    "endpoint_class": "circulating_biomarker",
                    "analysis_unit": "trial-level aggregate contrast",
                    "source_reported_crp_n": 161,
                    "source_reported_subgroup_n": 52,
                    "per_arm_denominator_not_explicit": True,
                    "adjustment": "not specified in abstract",
                    "multiplicity_adjustment_not_reported": True,
                    "cod_ci_upper_is_reported_rounded_zero": (label == "COD"),
                    "disease_incidence": False,
                    "molecule_effect_attribution": False,
                },
                estimate,
                "mg/L",
                (low, high),
                dose=300,
                dose_unit="g/week",
                duration="6 months",
                comparison="dietary advice only",
            )
            aggregate(
                db,
                "PMID20032491:" + label + ":local-panel-null",
                "TrialFood:NCT00145015:" + label,
                "TrialOutcome:FISHGASTRO:local-inflammation",
                "N07-parent-RCT",
                release,
                "20032491",
                {
                    "endpoint_class": "tissue_surrogate",
                    "analysis_unit": "trial-level qualitative exploratory panel",
                    "source_reported_subgroup_n": 52,
                    "null_result": True,
                    "marker_specific_effects_not_reported": True,
                    "endpoint_panel_not_independent_tests": True,
                    "source_statement": "Source reports no local inflammation marker differences",
                    "molecule_effect_attribution": False,
                },
                dose=300,
                dose_unit="g/week",
                duration="6 months",
                comparison="dietary advice only",
            )
        if validate(db):
            raise ValueError(validate(db))
    report = {
        "secondary_publication_not_independent_cohort": True,
        "new_health_records": 4,
        "trial_level_context_joins": trial_context_health(db, "GSE15284"),
        "individual_outcome_join": False,
    }
    (ROOT / "reports/fishgastro_secondary_validation.json").write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                "new_health_records": 4,
                "trial_level_records": len(report["trial_level_context_joins"]),
                "individual_outcome_join": False,
            }
        )
    )


if __name__ == "__main__":
    main()
