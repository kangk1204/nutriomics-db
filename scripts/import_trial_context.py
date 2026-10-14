"""Checksum-pinned aggregate outcomes and source-named assay contexts, offline."""

import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.context_joins import (
    migrate_context,
    put_context,
    trial_context_health,
    validate_context,
)
from nutrigenomics.expansion import backup, connect, coverage, insert, validate
from scripts.import_public_expansion import entity

ROOT = Path("research/2026-09-30/expansion")
CAP = ROOT / "context-joins"
FISH_EFFECTS = [
    ("SALMON", "Apoptosis", -0.10, -0.36, 0.16),
    ("COD", "Apoptosis", -0.06, -0.32, 0.20),
    ("SALMON", "Mitosis", -0.87, -2.41, 0.68),
    ("COD", "Mitosis", -1.04, -2.62, 0.53),
]
CLINICAL_EFFECTS = [
    ("FISHOIL", "Triglycerides", -0.61, 0.81, "mmol/L", None, "<0.001", "-0.61 ± 0.81"),
    ("FIBRATE", "Triglycerides", -0.34, 0.85, "mmol/L", 0.048, "=0.048", "-0.34 ± 0.85"),
    ("FISHOIL", "HDL", 0.13, 0.21, "mmol/L", 0.013, "=0.013", "0.13 ± 0.21"),
    ("FIBRATE", "HDL", 0.10, 0.18, "mmol/L", 0.076, "=0.076", "0.10 ± 0.18"),
    ("FISHOIL", "LDL", 0.34, 0.59, "mmol/L", 0.013, "=0.013", "0.34 ± 0.59"),
    ("FIBRATE", "sE-selectin", -4.1, 7.5, "ng/mL", 0.032, "=0.032", "-4.1 ± 7.5"),
    ("FIBRATE", "MCP1", 28, 55, "ng/mL", 0.034, "=0.034", "28 ± 55"),
]


def captured_sources():
    result = {}
    for name in ["discovery-manifest.json", "trial-context-manifest.json"]:
        for row in json.loads((CAP / name).read_text()):
            if "sha256" not in row:
                continue
            if hashlib.sha256((CAP / row["file"]).read_bytes()).hexdigest() != row["sha256"]:
                raise ValueError("Capture checksum mismatch: " + row["file"])
            result[row["file"]] = row
    return result


def source(db, row, name):
    identifier = name + ":" + row["sha256"][:20]
    insert(
        db,
        "ng_release",
        {
            "id": identifier,
            "source": name,
            "version": row["retrieved_at"],
            "url": row["url"],
            "retrieved_at": row["retrieved_at"],
            "sha256": row["sha256"],
            "local_path": str(CAP / row["file"]),
            "license": "Original source rights; redistribution unverified",
            "access_status": "public",
        },
    )
    put_context(
        db,
        "ng_rights",
        {
            "release_id": identifier,
            "status": "quarantined",
            "attribution": name,
            "basis": "Public read access only; original publication/database redistribution conditions unverified",
            "reviewed_at": row["retrieved_at"],
        },
    )
    return identifier


def primary_articles(path):
    return {
        a.findtext("./MedlineCitation/PMID"): a for a in ET.parse(path).findall("PubmedArticle")
    }


def abstract(article):
    return " ".join(
        "".join(n.itertext())
        for n in article.findall("./MedlineCitation/Article/Abstract/AbstractText")
    )


def arm(db, study, label, exposure, dose, unit, duration, release, context=None):
    put_context(
        db,
        "ng_arm",
        {
            "id": study + ":" + label,
            "study_id": study,
            "exposure_id": exposure,
            "source_label": label,
            "dose": dose,
            "dose_unit": unit,
            "duration": duration,
            "release_id": release,
            "context_json": json.dumps(context or {}, sort_keys=True),
        },
    )


def outcome(db, identifier, label, endpoint_class):
    entity(
        db,
        identifier,
        "outcome",
        label,
        "TrialOutcome",
        identifier,
        {"endpoint_class": endpoint_class, "disease_incidence": False},
    )


def aggregate(
    db,
    identifier,
    exposure,
    endpoint,
    study,
    release,
    pmid,
    context,
    estimate=None,
    unit=None,
    ci=None,
    p=None,
    dose=None,
    dose_unit=None,
    duration=None,
    comparison=None,
):
    row = {
        "id": identifier,
        "subject_id": exposure,
        "object_id": endpoint,
        "relation": "reported_trial_contrast",
        "layer": "health_outcome",
        "study_id": study,
        "release_id": release,
        "source_ref": "PMID:" + pmid + "#Abstract",
        "tier": "human_randomized",
        "inference": "intervention",
        "organism": "Homo sapiens",
        "dose": dose,
        "dose_unit": dose_unit,
        "duration": duration,
        "comparison": comparison,
        "context_json": json.dumps(context, sort_keys=True),
        "value_status": "qualitative" if estimate is None else "measured",
    }
    if estimate is not None:
        row.update(effect=estimate, effect_unit=unit)
    if ci:
        row.update(
            ci_low=ci[0], ci_high=ci[1], ci_level=0.95, uncertainty_type="confidence_interval"
        )
    if p is not None:
        row["p_value"] = p
    insert(db, "ng_evidence", row)


def normalize_assays(db, dataset):
    metadata = json.loads(dataset["metadata_json"])
    study = dataset["study_id"]
    if dataset["accession"] == "GSE15284":
        artifact = metadata["measurement_artifact"]
        rows, release = artifact["assays"], artifact["artifact"]["release_id"]
        label_field, tissue = "diet", "colonic mucosa"
    else:
        artifact = metadata["source_processing_audit"]
        rows, release = artifact["assays"], "GEO-matrix:" + artifact["input_sha256"][:20]
        label_field, tissue = "treatment", "peripheral blood mononuclear cells"
    source_row = db.execute("SELECT * FROM ng_release WHERE id=?", (release,)).fetchone()
    if (
        not source_row
        or hashlib.sha256(Path(source_row["local_path"]).read_bytes()).hexdigest()
        != source_row["sha256"]
    ):
        raise ValueError("Assay source matrix checksum mismatch")
    for row in rows:
        put_context(
            db,
            "ng_assay_context",
            {
                "accession": row["accession"],
                "dataset_id": dataset["id"],
                "participant_key": study + ":" + row["participant_id"],
                "arm_id": study + ":" + row[label_field],
                "period_code": row.get("period"),
                "time_label": row.get("time"),
                "tissue": tissue,
                "organism": "Homo sapiens",
                "processing": row["processing"],
                "unit_status": "source-normalized expression estimates; physical units/log base not explicit",
                "release_id": release,
                "source_ref": dataset["accession"] + "#Sample:" + row["accession"],
            },
        )


def main():
    captures = captured_sources()
    fish = primary_articles(CAP / "FISHGASTRO-pubmed.xml")["19553301"]
    clinical = primary_articles(CAP / "fish-oil-fenofibrate-pubmed.xml")["21429719"]
    registry = json.loads((CAP / "FISHGASTRO-NCT00145015.json").read_text())
    if registry["protocolSection"]["identificationModule"]["nctId"] != "NCT00145015":
        raise ValueError("Registry identity mismatch")
    for snippet in [
        "-0.10 (95% CI: -0.36, 0.16)",
        "-0.06 (95% CI: -0.32, 0.20)",
        "-0.87; 95% CI: -2.41, 0.68",
        "-1.04; 95% CI: -2.62, 0.53",
        "n = 242",
        "NCT00145015",
    ]:
        if snippet not in abstract(fish):
            raise ValueError("Fish source estimate mismatch: " + snippet)
    for row in CLINICAL_EFFECTS:
        if row[-1] not in abstract(clinical):
            raise ValueError("Clinical source estimate mismatch: " + row[-1])
    if "2006-005743-28" not in abstract(clinical):
        raise ValueError("Clinical registry mismatch")
    database = ROOT / "nutrition_evidence.sqlite3"
    if not (ROOT / "before-trial-context.sqlite3").exists():
        backup(database, ROOT / "before-trial-context.sqlite3")
    db = connect(database)
    migrate_context(db)
    with db:
        fr = source(db, captures["FISHGASTRO-pubmed.xml"], "PubMed-primary-FISHGASTRO")
        cr = source(db, captures["fish-oil-fenofibrate-pubmed.xml"], "PubMed-primary-fish-oil")
        rr = source(db, captures["FISHGASTRO-NCT00145015.json"], "ClinicalTrials.gov")
        for study, parent, article, people, pmid, registration, population, basis, metadata in [
            (
                "N07-parent-RCT",
                "N07",
                fish,
                242,
                "19553301",
                "NCT00145015",
                "Patients with colorectal polyps, inactive ulcerative colitis, or no macroscopic disease",
                "Same FISHGASTRO named trial; parent publication is not a new independent cohort",
                {
                    "registry_estimated_enrollment": 270,
                    "publication_actual_enrollment": 242,
                    "source_biopsy_n": 213,
                    "biopsy_n_unit_unclear": True,
                },
            ),
            (
                "N06-clinical-RCT",
                "N06",
                clinical,
                20,
                "21429719",
                "EudraCT:2006-005743-28",
                "Overweight and obese subjects",
                "Possible overlap with 11-person GEO subgroup; protocol matches but participant crosswalk unverified; conservatively not independent",
                {"geo_overlap_status": "candidate", "inflammation_improvement_reported": False},
            ),
        ]:
            group = db.execute(
                "SELECT participant_group FROM ng_study WHERE id=?", (parent,)
            ).fetchone()[0]
            insert(
                db,
                "ng_study",
                {
                    "id": study,
                    "title": article.findtext("./MedlineCitation/Article/ArticleTitle"),
                    "population": population,
                    "design": "randomized",
                    "participant_group": group,
                    "independence_status": "overlap",
                    "independence_basis": basis,
                    "n_people": people,
                    "pubmed_id": pmid,
                    "registration": registration,
                    "metadata_json": json.dumps(
                        dict(metadata, individual_crosswalk_available=False), sort_keys=True
                    ),
                },
            )
        for label in ["COD", "SALMON"]:
            food = "TrialFood:NCT00145015:" + label
            entity(
                db,
                food,
                "food",
                label.lower() + " intervention food",
                "TrialFood",
                "NCT00145015:" + label,
                {
                    "preparation_unspecified": True,
                    "batch_composition_unknown": True,
                    "generic_fdc_equivalence": False,
                },
            )
            arm(db, "N07", label, food, 300, "g/week", "6 months", rr)
            arm(db, "N07-parent-RCT", label, food, 300, "g/week", "6 months", fr)
        for endpoint, title in [
            ("Apoptosis", "Apoptotic cells per colonic crypt"),
            ("Mitosis", "Mitotic cells per colonic crypt"),
        ]:
            outcome(db, "TrialOutcome:NCT00145015:" + endpoint, title, "tissue_surrogate")
        for label, endpoint, estimate, low, high in FISH_EFFECTS:
            aggregate(
                db,
                "PMID19553301:" + label + ":" + endpoint,
                "TrialFood:NCT00145015:" + label,
                "TrialOutcome:NCT00145015:" + endpoint,
                "N07-parent-RCT",
                fr,
                "19553301",
                {
                    "endpoint_class": "tissue_surrogate",
                    "analysis_unit": "trial-level aggregate contrast",
                    "covariate_adjustment": "not specified in abstract",
                    "analysis_denominator": "not unambiguously specified; biopsy n=213",
                    "arm_allocation_n": 82 if label == "SALMON" else 78,
                    "control_allocation_n": 82,
                    "p_value_not_reported": True,
                    "multiplicity_adjustment_not_reported": True,
                    "null_result": True,
                    "ci_includes_zero": True,
                    "molecule_effect_attribution": False,
                },
                estimate,
                "cells/crypt",
                (low, high),
                dose=300,
                dose_unit="g/week",
                duration="6 months",
                comparison="dietary advice only",
            )
        for identifier, start, end, relation, status, release, basis in [
            (
                "N07:parent-named-trial",
                "N07",
                "N07-parent-RCT",
                "same_named_trial_context_only",
                "verified",
                rr,
                "FISHGASTRO name, salmon/cod 300 g/week and six-month intervention; registry identifies primary paper; no individual health crosswalk",
            ),
            (
                "N06:clinical-protocol-candidate",
                "N06",
                "N06-clinical-RCT",
                "matching_protocol_possible_subgroup",
                "candidate",
                cr,
                "Matching authors, doses, randomized crossover and washout; GEO does not establish EudraCT or individual membership",
            ),
        ]:
            put_context(
                db,
                "ng_study_link",
                {
                    "id": identifier,
                    "from_study": start,
                    "to_study": end,
                    "relation": relation,
                    "review_status": status,
                    "participant_join_allowed": 0,
                    "release_id": release,
                    "basis": basis,
                },
            )
        for label, title in [
            ("FISHOIL", "n-3 fish-oil formulation"),
            ("FIBRATE", "fenofibrate treatment"),
            ("PLACEBO", "source placebo treatment"),
        ]:
            exposure = "TrialExposure:N06:" + label
            if label == "FIBRATE":
                previous = db.execute("SELECT * FROM ng_entity WHERE id=?", (exposure,)).fetchone()
                if previous and previous["kind"] == "diet":
                    if (
                        previous["namespace"] != "TrialExposure"
                        or previous["identifier"] != "N06:FIBRATE"
                    ):
                        raise ValueError("Unexpected prior pharmacologic exposure identity")
                    db.execute("UPDATE ng_entity SET kind='compound' WHERE id=?", (exposure,))
            entity(
                db,
                exposure,
                "compound" if label == "FIBRATE" else "diet",
                title,
                "TrialExposure",
                "N06:" + label,
                {"isolated_molecule": False, "formulation_not_analytically_quantified": True},
            )
            for study in ["N06", "N06-clinical-RCT"]:
                release = (
                    cr
                    if study.endswith("RCT")
                    else db.execute(
                        "SELECT release_id FROM ng_dataset WHERE id='GSE27385'"
                    ).fetchone()[0]
                )
                arm(
                    db,
                    study,
                    label,
                    exposure,
                    3.7 if label == "FISHOIL" else 200 if label == "FIBRATE" else None,
                    "g/day n-3 fatty acids"
                    if label == "FISHOIL"
                    else "mg/day"
                    if label == "FIBRATE"
                    else None,
                    "6 weeks",
                    release,
                    {
                        "washout": "2 weeks (GEO: at least 2 weeks)",
                        "constituent_doses_nominal": {"EPA_g_per_day": 1.7, "DHA_g_per_day": 1.2}
                        if label == "FISHOIL"
                        else {},
                        "quantity_transfer_to_free_compounds": False,
                        "molecule_effect_attribution": False,
                    },
                )
        for label, endpoint, estimate, dispersion, unit, p, reported_p, _ in CLINICAL_EFFECTS:
            outcome(db, "ClinicalOutcome:" + endpoint, endpoint, "circulating_biomarker")
            aggregate(
                db,
                "PMID21429719:" + label + ":" + endpoint,
                "TrialExposure:N06:" + label,
                "ClinicalOutcome:" + endpoint,
                "N06-clinical-RCT",
                cr,
                "21429719",
                {
                    "endpoint_class": "circulating_biomarker",
                    "analysis_unit": "trial-level aggregate",
                    "reported_plus_minus": dispersion,
                    "reported_plus_minus_type": "not specified in abstract",
                    "reported_p": reported_p,
                    "standard_error_inferred": False,
                    "confidence_interval_inferred": False,
                    "covariate_adjustment": "not specified in abstract",
                    "multiplicity_adjustment": "not reported",
                    "parent_trial_n": 20,
                    "effect_denominator_not_explicit": True,
                    "molecule_effect_attribution": False,
                    "null_result": (p == 0.076),
                },
                estimate,
                unit,
                p=p,
                duration="6 weeks",
                comparison="Source-reported treatment change; exact baseline/placebo contrast not specified in abstract",
            )
        for endpoint in ["sE-selectin", "MCP1"]:
            aggregate(
                db,
                "PMID21429719:FISHOIL:null:" + endpoint,
                "TrialExposure:N06:FISHOIL",
                "ClinicalOutcome:" + endpoint,
                "N06-clinical-RCT",
                cr,
                "21429719",
                {
                    "endpoint_class": "circulating_biomarker",
                    "null_result": True,
                    "effect_estimate_not_reported": True,
                    "statement": "Source reports fish oil had no effects on these markers",
                    "analysis_unit": "trial-level qualitative null finding",
                    "molecule_effect_attribution": False,
                },
                duration="6 weeks",
            )
        for dataset in db.execute(
            "SELECT * FROM ng_dataset WHERE accession IN ('GSE27385','GSE15284')"
        ):
            normalize_assays(db, dataset)
        errors = validate(db) + validate_context(db)
        if errors:
            raise ValueError(errors)
    report = coverage(db)
    report.update(
        normalized_assays=db.execute("SELECT count(*) FROM ng_assay_context").fetchone()[0],
        normalized_participants=db.execute(
            "SELECT count(DISTINCT participant_key) FROM ng_assay_context"
        ).fetchone()[0],
        trial_level_context_joins=trial_context_health(db, "GSE15284"),
        candidate_protocol_joins_excluded=trial_context_health(db, "GSE27385") == [],
        participant_matched_health_chains=0,
        molecular_effect_attribution=False,
    )
    (ROOT / "reports/trial_context_coverage.json").write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                key: report[key]
                for key in [
                    "normalized_assays",
                    "normalized_participants",
                    "evidence_layers",
                    "candidate_protocol_joins_excluded",
                    "validation_errors",
                ]
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
