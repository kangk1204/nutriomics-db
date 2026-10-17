"""Derived public indexes and audits; no numerical effect is edited or copied."""
import json
from collections import Counter

from .composition_policy import POLICY_VERSION, assess
from .explorer_build import canonical


def analysis_role(version):
    version = version.split("-rebuild", 1)[0]
    if "blood-composition" in version:
        return "sensitivity"
    if "regions-fixed" in version:
        return "secondary_fixed_region_screen"
    if version in {"gse56960-rma-limma-v1", "gse107205-noob-limma-cpg-v1"}:
        return "historical"
    return "primary_candidate"


def build_quality(db):
    """Only called on a newly derived release, never on the original candidate."""
    foods = {r[0]: json.loads(r[1]) for r in db.execute("SELECT id,metadata_json FROM node WHERE kind='food'")}
    db.executescript("""
        CREATE TABLE composition_assessment(
          measurement_id TEXT NOT NULL,eligible INTEGER NOT NULL,
          method TEXT NOT NULL,assessment_json TEXT NOT NULL);
        CREATE TABLE contrast_selector(
          series_accession TEXT,contrast TEXT,analysis_version TEXT,
          contrast_record_id TEXT,study_id TEXT,analysis_role TEXT,context_type TEXT,
          review_status TEXT NOT NULL,PRIMARY KEY(series_accession,contrast,analysis_version));
    """)
    methods, states, excluded, eligible_foods = Counter(), Counter(), Counter(), set()
    rows = db.execute("SELECT id,food,amount,unit,basis,status,source_id,raw_json FROM measurement")
    batch = []
    for row in rows:
        raw = json.loads(row[7]) if row[7] else {}
        result = assess(status=row[5], amount=row[2], unit=row[3], basis=row[4],
                        metadata=raw, food_metadata=foods.get(row[1], {}), source_id=row[6])
        methods[result["derivation_method"]] += 1
        states[row[5]] += 1
        excluded.update(result["exclusion_reasons"])
        if result["eligible"]:
            eligible_foods.add(row[1])
        batch.append((row[0], int(result["eligible"]), result["derivation_method"], canonical(result)))
        if len(batch) == 5000:
            db.executemany("INSERT INTO composition_assessment VALUES(?,?,?,?)", batch)
            batch.clear()
    db.executemany("INSERT INTO composition_assessment VALUES(?,?,?,?)", batch)
    db.execute("CREATE UNIQUE INDEX composition_assessment_id ON composition_assessment(measurement_id)")
    db.execute("CREATE INDEX composition_eligible ON composition_assessment(eligible,measurement_id)")
    contrasts = {}
    for row in db.execute("SELECT id,metadata_json FROM science_record WHERE kind='contrast'"):
        value = json.loads(row[1])
        contrasts[(value.get("series_accession"), value.get("contrast"), value.get("analysis_version"))] = (row[0], value)
    unlinked, withheld = [], []
    selectors = list(db.execute("SELECT DISTINCT series_accession,contrast,analysis_version FROM effect_selector"))
    for accession, contrast, version in selectors:
        key = (accession, contrast, version)
        record_id, value = contrasts.get(key, (None, {}))
        context_type = "pharmacological_comparator" if accession == "GSE27385" and contrast == "fenofibrate_vs_placebo_6weeks" else "dietary_intervention"
        # This source-derived comparator was in the result file but omitted from
        # curation. Add an explicitly pending record, not a human-approved claim.
        if not record_id and context_type == "pharmacological_comparator":
            record_id = ":".join(key)
            value = {"id": record_id, "study_id": "N06", "series_accession": accession,
                     "contrast": contrast, "analysis_version": version,
                     "comparison_kind": "pharmacological_comparator",
                     "context_type": context_type, "exposure_ids": [], "arm_ids": [],
                     "dietary_effect_interpretation_allowed": False,
                     "source_url": "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE27385",
                     "publication_review_status": "pending", "expert_review_status": "pending",
                     "curation_method": "exact existing result-selector reference; source study must be reviewed"}
            db.execute("INSERT INTO science_record VALUES(?,?,?)", (record_id, "contrast", canonical(value)))
        if not record_id:
            unlinked.append(list(key))
        db.execute("INSERT INTO contrast_selector VALUES(?,?,?,?,?,?,?,?)",
                   (*key, record_id, value.get("study_id"), analysis_role(version), context_type, "pending"))
    available = {tuple(r) for r in selectors}
    for key in contrasts:
        if key not in available:
            withheld.append(list(key))
    report = {"composition_policy_version": POLICY_VERSION,
              "composition": {"measurements": sum(states.values()), "source_states": dict(states),
                              "methods": dict(methods), "eligible_foods": len(eligible_foods),
                              "food_count": len(foods), "exclusion_reasons": dict(excluded)},
              "contrast_coverage": {"public_selectors": len(selectors),
                                    "unlinked": unlinked, "curated_without_public_values": withheld},
              "scientific_review": "pending", "independent_trials_audited": False}
    return report
