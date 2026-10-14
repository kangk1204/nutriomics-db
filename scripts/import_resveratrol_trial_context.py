"""Qualitative parent-trial outcomes; keeps 11 clinical and 10 omics people distinct."""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.context_joins import put_context, trial_context_health
from nutrigenomics.expansion import connect, insert, validate
from scripts.import_trial_context import ROOT, abstract, aggregate, arm, outcome, primary_articles


def main():
    cap = ROOT / "cohort-primary/GSE32357"
    row = json.loads((cap / "primary-pubmed-manifest.json").read_text())
    if hashlib.sha256((cap / row["file"]).read_bytes()).hexdigest() != row["sha256"]:
        raise ValueError("Primary publication checksum mismatch")
    article = primary_articles(cap / row["file"])["22055504"]
    content = abstract(article)
    if "11 healthy, obese men" not in content:
        raise ValueError("Parent trial denominator differs from source")
    db = connect(ROOT / "nutrition_evidence.sqlite3")
    with db:
        release = "PubMed-primary-resveratrol:" + row["sha256"][:20]
        insert(
            db,
            "ng_release",
            {
                "id": release,
                "source": "PubMed primary / Timmers et al.",
                "version": row["retrieved_at"],
                "url": row["url"],
                "retrieved_at": row["retrieved_at"],
                "sha256": row["sha256"],
                "local_path": str(cap / row["file"]),
                "license": "Original publication rights; redistribution unverified",
                "access_status": "public",
            },
        )
        put_context(
            db,
            "ng_rights",
            {
                "release_id": release,
                "status": "quarantined",
                "attribution": "Timmers et al. / PubMed",
                "basis": "Public abstract; original publication conditions unverified; no abstract text redistributed",
                "reviewed_at": row["retrieved_at"],
            },
        )
        insert(
            db,
            "ng_study",
            {
                "id": "N14-parent-RCT",
                "title": article.findtext("./MedlineCitation/Article/ArticleTitle"),
                "population": "11 healthy obese men in the clinical trial",
                "design": "randomized",
                "participant_group": "Timmers2011-GSE32357",
                "independence_status": "overlap",
                "independence_basis": "Same primary PMID cited by GEO; clinical parent, not a second independent cohort",
                "n_people": 11,
                "pubmed_id": "22055504",
                "metadata_json": json.dumps(
                    {"omics_n": 10, "clinical_n": 11, "individual_crosswalk_available": False},
                    sort_keys=True,
                ),
            },
        )
        exposure = "TrialExposure:GSE32357:RESVERATROL"
        arm(
            db,
            "N14-parent-RCT",
            "RESVERATROL",
            exposure,
            150,
            "mg/day trans-resveratrol",
            "30 days",
            release,
        )
        for endpoint, source_label in [
            ("glucose", "circulating glucose"),
            ("triglycerides", "triglycerides"),
            ("ALT", "alanine-aminotransferase"),
            ("systolic-blood-pressure", "Systolic blood pressure"),
            ("HOMA-index", "HOMA index"),
        ]:
            if source_label not in content:
                raise ValueError("Primary qualitative endpoint not found")
            identifier = "ClinicalOutcome:Timmers2011:" + endpoint
            outcome(db, identifier, source_label, "clinical_or_metabolic_risk_marker")
            aggregate(
                db,
                "PMID22055504:" + endpoint,
                exposure,
                identifier,
                "N14-parent-RCT",
                release,
                "22055504",
                {
                    "endpoint_class": "clinical_or_metabolic_risk_marker",
                    "reported_direction": "improved" if endpoint == "HOMA-index" else "decreased",
                    "effect_estimate_not_reported": True,
                    "uncertainty_not_reported": True,
                    "p_value_not_reported": True,
                    "analysis_unit": "qualitative parent-trial summary",
                    "clinical_n": 11,
                    "omics_n": 10,
                    "participant_health_crosswalk_unavailable": True,
                    "isolated_food_effect": False,
                    "covariate_adjustment": "not specified in abstract",
                    "multiplicity_adjustment": "not reported",
                },
                dose=150,
                dose_unit="mg/day trans-resveratrol",
                duration="30 days",
                comparison="placebo crossover arm",
            )
        put_context(
            db,
            "ng_study_link",
            {
                "id": "N14:parent-publication-context",
                "from_study": "N14",
                "to_study": "N14-parent-RCT",
                "relation": "same_primary_publication_trial_context_only",
                "review_status": "verified",
                "participant_join_allowed": 0,
                "release_id": release,
                "basis": "GEO directly cites PMID22055504 with same treatment/design; clinical n=11 vs omics n=10; no public individual crosswalk",
            },
        )
        if validate(db):
            raise ValueError(validate(db))
    report = {
        "clinical_n": 11,
        "omics_n": 10,
        "qualitative_health_records": 5,
        "quantitative_health_estimates": 0,
        "participant_matched_health": False,
        "trial_level_context": trial_context_health(db, "GSE32357"),
    }
    (ROOT / "reports/resveratrol_trial_context.json").write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                "clinical_n": 11,
                "omics_n": 10,
                "qualitative_records": 5,
                "individual_crosswalk": False,
            }
        )
    )


if __name__ == "__main__":
    main()
