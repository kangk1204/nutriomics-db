"""Recreate pending clinical facts from fixed, CC-BY source tables.

Run with bundled document Python when recreating the PDF transcription check.
No hypothesis tests, effects or uncertainty intervals are recomputed.
"""
from pathlib import Path
import hashlib
import json
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[3]
VERSION = "clinical-source-facts-20261019-v1"
XML = {"S03": "S03-PMC3998980.xml", "S04": "S04-PMC5793243.xml",
       "S05": "S05-PMC9710135.xml", "N15": "N15-PMC5831037.xml"}
DOIS = {"S03": "10.1371/journal.pone.0095527", "S04": "10.3390/nu10010015",
        "S05": "10.1186/s13148-022-01377-6", "N15": "10.1371/journal.pone.0192947"}
PMIDS = {"S03": "24763279", "S04": "29295516", "S05": "36447285", "N15": "29489876"}
SERIES = {"S03": ["GSE54325", "GSE54643"], "S04": ["GSE107205"],
          "S05": ["GSE219217"], "N15": ["GSE98645"]}


def text(e):
    return " ".join(" ".join(e.itertext()).split())


def table_rows(study):
    table = ET.parse(ROOT / XML[study]).find(".//table-wrap")
    return {text(list(tr)[0]): [text(x) for x in list(tr)] for tr in table.findall(".//tr") if len(tr)}


def pair(s, kind):
    match = re.fullmatch(r"([0-9.]+)\s*\(([0-9.]+)(?:[–-]([0-9.]+))?\)", s)
    if not match:
        raise ValueError("unexpected literal table value: " + s)
    a, b, c = match.groups()
    return (float(a), {"kind": kind, "value": float(b)} if kind == "SD" else
            {"kind": kind, "lower": float(b), "upper": float(c)})


def pvalue(label, scope, *, adjusted=False, method):
    raw = label.replace("−", "-")
    return {"comparison_scope": scope, "p_value": None if raw in {"ns", "–"} else float(raw),
            "reported_label": "not_reported" if raw == "–" else raw,
            "test_method": method, "multiplicity": "Bonferroni-adjusted pairwise" if adjusted else
                "source-reported; no new correction applied", "adjusted": adjusted}


def endpoint(key, label, ko, domain, unit):
    return {"id": "ClinicalEndpoint:" + key, "label": label, "label_ko": ko,
            "domain": domain, "unit": unit, "canonical_unit": unit}


def record(study, key, ep, result_type, n, scope, comparison, location):
    return {"id": f"ClinicalResult:{study}:{key}:{VERSION}", "study_id": study,
            "series_accessions": SERIES[study], "pmid": PMIDS[study], "endpoint": ep,
            "result_type": result_type, "n_people": n, "n_people_scope": scope,
            "comparison": comparison, "source_location": location,
            "source_ids": ["ClinicalSource:" + study], "clinical_layer_version": VERSION,
            "expert_review_status": "pending", "source_check_method": "machine_assisted_source_table_transcription",
            "clinical_effect_estimate": None, "standard_error": None, "confidence_interval": None,
            "genomic_mediation_demonstrated": False, "individual_prediction_available": False,
            "individual_omics_clinical_linkage": "not_established_in_this_curation",
            "missing_reasons": {"clinical_effect_estimate": "only published summaries are transcribed; no effect is derived",
                                "standard_error": "not reported for this summary; SD/range/IQR are not SE",
                                "confidence_interval": "not reported; not reconstructed from a P value"},
            "observations": [], "statistical_tests": []}


def build():
    sources = []
    for study, filename in XML.items():
        tree = ET.parse(ROOT / filename)
        permissions = ET.tostring(tree.find(".//permissions"), encoding="unicode")
        if "creativecommons.org/licenses/by/4.0" not in permissions:
            raise ValueError("source-specific CC BY 4.0 permission not found")
        title = text(tree.find(".//article-title"))
        author_names = [text(name) for name in tree.findall(".//article-meta/contrib-group/contrib/name")]
        sources.append({"id": "ClinicalSource:" + study, "source": "Published clinical source facts · " + study,
                        "source_url": "https://doi.org/" + DOIS[study], "version": DOIS[study],
                        "sha256": hashlib.sha256((ROOT / filename).read_bytes()).hexdigest(),
                        "source_file": filename, "license": "CC-BY-4.0", "export_allowed": True,
                        "rights_status": "verified", "access_status": "open_access",
                        "rights_reviewed_at": "2026-10-19", "rights_location": "JATS article/front/article-meta/permissions",
                        "rights_scope": "own paraphrase and numerical facts from the article tables; no third-party images",
                        "article_title": title, "authors": author_names,
                        "attribution": "; ".join(author_names) + ". " + title + ". " + DOIS[study]})
    records = []
    s03 = table_rows("S03")
    specs03 = [
        ("BMI, kg/m", "bmi", "Body mass index", "체질량지수", "obesity", "kg/m²"),
        ("SBP, mmHg", "systolic_blood_pressure", "Systolic blood pressure", "수축기 혈압", "blood_pressure", "mmHg"),
        ("DBP, mmHg", "diastolic_blood_pressure", "Diastolic blood pressure", "이완기 혈압", "blood_pressure", "mmHg"),
        ("tChol, mmol/L", "total_cholesterol", "Total cholesterol", "총 콜레스테롤", "lipids", "mmol/L"),
        ("LDL, mmol/L", "ldl_cholesterol", "LDL cholesterol", "LDL 콜레스테롤", "lipids", "mmol/L"),
        ("HDL, mmol/L", "hdl_cholesterol", "HDL cholesterol", "HDL 콜레스테롤", "lipids", "mmol/L"),
        ("TG, mmol/L", "triglycerides", "Triglycerides", "중성지방", "lipids", "mmol/L"),
    ]
    for row_label, key, en, ko, domain, unit in specs03:
        value, dispersion = pair(s03[row_label][1], "range")
        r = record("S03", key + ":baseline", endpoint(key, en, ko, domain, unit), "baseline_summary", 13,
                   "13 volunteers with blood available before selection/QC; expression and DNAm subsets are smaller",
                   {"kind": "descriptive_baseline", "arm": "MOF recipients", "duration_weeks": 8},
                   "Table 1 (pone-0095527-t001), row " + row_label + "; values are median (range)")
        r["observations"] = [{"arm": "MOF recipients", "time_days": 0, "time_label": "baseline",
                              "value": value, "summary_statistic": "median", "dispersion": dispersion}]
        if key == "bmi":
            r["endpoint"].update(unit="kg/m (as labelled in source Table 1)", canonical_unit=None,
                unit_caveat="The source Table 1 XML prints kg/m without an exponent, whereas source prose describes BMI in kg/m². The table label is retained and not silently repaired.")
        r["interpretation_limit"] = "Baseline participant characteristic, not an 8-week treatment effect or a randomized control comparison."
        records.append(r)
    s04 = table_rows("S04")
    specs04 = [
        ("Weight (kg) 1", "body_weight", "Body weight", "체중", "obesity", "kg"),
        ("Waist circumference (cm) 1", "waist_circumference", "Waist circumference", "허리둘레", "obesity", "cm"),
        ("BMI (kg/m 2 ) 1", "bmi", "Body mass index", "체질량지수", "obesity", "kg/m²"),
        ("HDL cholesterol (mg/dL) 1", "hdl_cholesterol", "HDL cholesterol", "HDL 콜레스테롤", "lipids", "mg/dL"),
        ("LDL cholesterol (mg/dL) 1", "ldl_cholesterol", "LDL cholesterol", "LDL 콜레스테롤", "lipids", "mg/dL"),
        ("Total cholesterol (mg/dL) 1", "total_cholesterol", "Total cholesterol", "총 콜레스테롤", "lipids", "mg/dL"),
        ("Triglycerides (mg/dL) 1", "triglycerides", "Triglycerides", "중성지방", "lipids", "mg/dL"),
        ("Systolic arterial pressure (mmHg) 1", "systolic_blood_pressure", "Systolic blood pressure", "수축기 혈압", "blood_pressure", "mmHg"),
        ("Diastolic arterial pressure (mmHg) 1", "diastolic_blood_pressure", "Diastolic blood pressure", "이완기 혈압", "blood_pressure", "mmHg"),
    ]
    method04 = "source Table 1 footnotes: Student t / Wilcoxon / sign / Cochran as appropriate; exact endpoint-specific test not separately identified"
    omnibus04 = "source Table 1 footnotes: ANOVA or Kruskal-Wallis as appropriate; not an identified pairwise contrast"
    for row_label, key, en, ko, domain, unit in specs04:
        row = s04[row_label]
        for arm, col in [("low-fat control", 1), ("MedDiet + EVOO", 4), ("MedDiet + nuts", 7)]:
            r = record("S04", key + ":" + {1: "control", 4: "evoo", 7: "nuts"}[col],
                       endpoint(key, en, ko, domain, unit), "repeated_group_summary", 12,
                       "12 selected PREDIMED-Navarra participants per arm in the methylation sub-study; 36 total, not the parent trial",
                       {"kind": "published_arm_time_summaries", "arm": arm, "timepoints": ["baseline", "5 years"],
                        "between_arm_change_effect_estimated": False}, "Table 1 (nutrients-10-00015-t001), row " + row_label)
            for time, label, idx in [(0, "baseline", col), (None, "5 years", col + 1)]:
                value, dispersion = pair(row[idx], "SD")
                r["observations"].append({"arm": arm, "time_days": time, "time_label": label,
                                          "value": value, "summary_statistic": "mean", "dispersion": dispersion})
            r["statistical_tests"] = [pvalue(row[col+2], "within-arm baseline versus 5 years", method=method04),
                                      pvalue(row[10], "three-arm omnibus baseline difference", method=omnibus04),
                                      pvalue(row[11], "three-arm omnibus difference at 5 years", method=omnibus04),
                                      pvalue(row[12], "three-arm omnibus difference in 5-year changes", method=omnibus04)]
            r["interpretation_limit"] = "Reported arm means (SD), not participant-level changes or an estimated pairwise randomized treatment effect. 'ns' is not P=1 or zero change; no clinical improvement inferred."
            records.append(r)
    s05 = table_rows("S05")
    specs05 = [
        ("Body weight (kg)", "body_weight", "Body weight", "체중", "obesity", "kg"),
        ("BMI (kg/m2)", "bmi", "Body mass index", "체질량지수", "obesity", "kg/m²"),
        ("Total cholesterol (mg/dl)", "total_cholesterol", "Total cholesterol", "총 콜레스테롤", "lipids", "mg/dL"),
        ("LDL (mg/dl)", "ldl_cholesterol", "LDL cholesterol", "LDL 콜레스테롤", "lipids", "mg/dL"),
        ("HDL (mg/dl)", "hdl_cholesterol", "HDL cholesterol", "HDL 콜레스테롤", "lipids", "mg/dL"),
        ("Triglycerides (mg/dl)", "triglycerides", "Triglycerides", "중성지방", "lipids", "mg/dL"),
    ]
    for row_label, key, en, ko, domain, unit in specs05:
        row = s05[row_label]
        r = record("S05", key, endpoint(key, en, ko, domain, unit), "repeated_group_summary", 18,
                   "single cohort 18 participants (10 women/8 men); source methods impute missing values by the parameter mean; observed n per cell not reported",
                   {"kind": "uncontrolled_repeated_measurements", "arm": "diet-counselled NAFLD cohort",
                    "control_arm": None, "timepoints_days": [0, 30, 60]}, "Table 1 (Tab1), parts C/D, row " + row_label)
        for idx, day in enumerate([0, 30, 60], 1):
            value, dispersion = pair(row[idx], "range")
            r["observations"].append({"arm": "diet-counselled NAFLD cohort", "time_days": day,
                                      "time_label": {0: "T1 baseline", 30: "T2 30 days", 60: "T3 60 days"}[day],
                                      "value": value, "summary_statistic": "median", "dispersion": dispersion,
                                      "observed_n": None, "observed_n_missing_reason": "source reports cohort n, not non-imputed n per endpoint/time cell"})
        r["statistical_tests"] = [pvalue(row[4], "omnibus comparison across T1/T2/T3", method="repeated measures ANOVA or Friedman; exact per-endpoint choice not reported")]
        for i, label in [(5, "T1 versus T2"), (7, "T1 versus T3"), (9, "T2 versus T3")]:
            for j, adjusted in [(i, False), (i+1, True)]:
                r["statistical_tests"].append(pvalue(row[j], label, adjusted=adjusted,
                    method="source paired t or Wilcoxon; exact per-endpoint choice not reported"))
        r["interpretation_limit"] = "Single-group observational time changes with no randomized dietary control. Omnibus significance is not significance for a specific pair; adjusted and unadjusted P values remain separate. No genomic mediation inferred."
        records.append(r)
    # Literal S3 PDF values were visually/text checked; layout extraction is
    # retained with the PDF SHA. The paper labels units as concentration while
    # reporting an integrated response: its time unit is not supplied here.
    auc = [
        ("total_cholesterol", "Total cholesterol", "총 콜레스테롤", [69.0,27.4,101.6,25.5,-12.4,64.5,70.5,60.0,87.0,70.5,60.0,111.8], .138, .295),
        ("hdl_cholesterol", "HDL cholesterol", "HDL 콜레스테롤", [9.7,.4,18,-8.3,-16.5,1.1,18,9.7,18,18,0,26.3], .097, .612),
        ("ldl_cholesterol", "LDL cholesterol", "LDL 콜레스테롤", [60,39.8,70.1,25.5,-21.8,59.3,51,24.8,86.2,67.5,35.3,120], .223, .534),
        ("triglycerides", "Triglycerides", "중성지방", [57,41.6,85.9,96,79.9,102,43.5,34.5,96.8,82.5,57.8,107.3], .945, .295),
    ]
    sources.append({**sources[-1], "id": "ClinicalSource:N15-S3", "source": "Burton et al. 2018 · S3 clinical table",
                    "source_url": "https://journals.plos.org/plosone/article/file?id=10.1371/journal.pone.0192947.s008&type=supplementary",
                    "sha256": hashlib.sha256((ROOT / "N15-S3Table.pdf").read_bytes()).hexdigest(),
                    "source_file": "N15-S3Table.pdf", "rights_location": "Main article copyright/permissions, no separate exclusion in S3 table"})
    for key, en, ko, values, carry, intervention in auc:
        ep = endpoint(key + "_iauc", en + " postprandial iAUC", ko + " 식후 iAUC", "lipids", "mmol/L (source-labelled iAUC)")
        ep.update(canonical_unit=None, unit_caveat="Source table labels concentration unit mmol/L; integrated time unit is not stated. No concentration or time-unit conversion is performed.")
        r = record("N15", key + ":postprandial_iauc", ep, "crossover_iAUC_summary", 13,
                   "clinical cohort 13 after one post-hoc exclusion; crossover order groups n=6 and n=7; RNA-seq subset n=7 is not the full clinical cohort",
                   {"kind": "crossover_order_stratified_reported_iAUC", "dose_g": 800, "response_window_h": [0,6],
                    "orders": ["acidified milk then probiotic yoghurt", "probiotic yoghurt then acidified milk"]},
                   "S3 Table (pone.0192947.s008), row " + en + "; median iAUC (IQR) and intervention/carry-over columns")
        r["source_ids"] = ["ClinicalSource:N15", "ClinicalSource:N15-S3"]
        for i, (order, arm, n) in enumerate([("milk then yoghurt", "acidified milk", 6), ("milk then yoghurt", "probiotic yoghurt", 6),
                                            ("yoghurt then milk", "acidified milk", 7), ("yoghurt then milk", "probiotic yoghurt", 7)]):
            r["observations"].append({"arm": arm, "crossover_order": order, "n_people": n,
                                      "time_days": None, "time_label": "0–6 h incremental AUC",
                                      "value": values[i*3], "summary_statistic": "median_iAUC",
                                      "dispersion": {"kind": "IQR", "lower": values[i*3+1], "upper": values[i*3+2]}})
        r["statistical_tests"] = [pvalue(str(carry), "crossover carry-over sum test", method="Wellek-Blettner crossover procedure; paired Wilcoxon as described by source"),
                                  pvalue(str(intervention), "crossover intervention effect yoghurt versus acidified milk", method="paired Wilcoxon in source crossover analysis")]
        r["interpretation_limit"] = "Order-stratified medians of an integrated response are not fasting concentrations, individual trajectories or a mean paired treatment effect. Lipid response differences were not significant in this source; this is not equivalence."
        records.append(r)
    # Source availability is an audit of this build, not an assertion that an
    # endpoint was absent or never measured by the investigators.
    audit_specs = [
        ("S01", ["https://doi.org/10.1016/j.jnutbio.2019.06.007", "https://escholarship.org/uc/item/6wf307cz"], "fulltext_quantitative_table_not_audited",
         "Original publication and institutional author copy located; quantitative clinical tables/rights not audited in this layer. Parent trial 62, RT-PCR 20 and RNA-seq 5 must remain separate."),
        ("S02", ["https://doi.org/10.1016/j.jnutbio.2017.02.012", "https://arbor.bfh.ch/bitstreams/c778d67d-4e6d-484c-9bc5-b450ce435884/download"], "fulltext_quantitative_table_not_audited",
         "Author institutional manuscript located; source retrieval timed out in this audit. Clinical Table 1 and supplementary iAUC table require full transcription/rights review. No claim of non-measurement."),
        ("S03", ["https://doi.org/"+DOIS["S03"]], "quantitative_source_checked_candidate",
         "Baseline BP/lipid/BMI medians and ranges from OA Table 1; no intervention change inferred from baseline. Gene-expression/DNAm subsets are not the 13-person descriptor cohort."),
        ("S04", ["https://doi.org/"+DOIS["S04"]], "quantitative_source_checked_candidate",
         "Three arms' baseline/5-year mean(SD) values transcribed from Table 1 with within-arm and three-arm P labels. No pairwise clinical change effect is derived. Binary diagnosis rows excluded pending clarification of definition/medication coding."),
        ("S05", ["https://doi.org/"+DOIS["S05"]], "quantitative_source_checked_candidate",
         "OA Table 1 serial medians/ranges and original omnibus/unadjusted/adjusted pairwise P values retained separately. Source mean-imputes missing clinical data; per-cell observed n unknown. No dietary control."),
        ("N14", ["https://doi.org/10.3945/ajcn.2009.28586", "https://ajcn.nutrition.org/article/S0002-9165%2823%2901632-5/fulltext"], "fulltext_quantitative_table_not_audited",
         "Primary full text is indexed as an open archive under an Elsevier user license; direct retrieval returned403 and numerical figure transcription/redistribution review is unfinished. Public PBMC expression conditions are SFA/PUFA, not all three clinical shake conditions."),
        ("N15", ["https://doi.org/"+DOIS["N15"]], "quantitative_source_checked_candidate",
         "OA full text and S3 PDF audited for four lipid iAUC summaries. Clinical n13 and RNA-seq n7 separated; unit integration ambiguity and crossover order strata preserved. Other biomarkers/chronic endpoints not exhaustively transcribed."),
        ("N16", ["https://doi.org/10.1016/j.metabol.2023.155594", "https://www.ebi.ac.uk/biostudies/arrayexpress/studies/E-MTAB-12527"], "metadata_only_clinical_results_not_audited",
         "DIRECT PLUS registry/SDRF metadata only in this release. Primary clinical table direct retrieval returned403; no clinical numeric values or completed local DNAm analysis asserted. Public paired sample subset and parent clinical cohorts require separate audit."),
        ("N06", ["https://doi.org/10.1016/j.numecd.2010.12.010", "https://pubmed.ncbi.nlm.nih.gov/21429719/"], "abstract_only_quantitative_semantics_not_audited",
         "Primary abstract reports numeric lipid changes, but plus/minus dispersion definition and exact contrast require full text; no reinterpretation as SD/SE or redistribution approval. Clinical trial n20 and PBMC subset n11 separate; fenofibrate is pharmacological."),
    ]
    audits = [{"study_id": study, "source_urls": urls, "status": state, "reason": reason,
               "not_measured": None, "expert_review_status": "pending", "audit_date": "2026-10-19",
               "candidate_result_count": sum(r["study_id"] == study for r in records),
               "coverage_is_exhaustive": False,
               "assessed_scope": ["blood_pressure", "lipids", "obesity"]} for study,urls,state,reason in audit_specs]
    by_source = {source["id"]: source for source in sources}
    for r in records:
        r["source_references"] = [{"id": source_id, "url": by_source[source_id]["source_url"],
            "label": by_source[source_id]["article_title"], "version": by_source[source_id]["version"],
            "sha256": by_source[source_id]["sha256"], "license": by_source[source_id]["license"],
            "attribution": by_source[source_id]["attribution"]} for source_id in r["source_ids"]]
    return {"schema_version": 1, "version": VERSION, "expert_review_status": "pending", "human_approved_records": 0,
            "curation_scope": "Selected OA source-located clinical summaries; not an exhaustive clinical endpoint database",
            "sources": sources, "study_audits": audits, "records": records}


if __name__ == "__main__":
    data = build()
    from nutrigenomics.clinical_endpoints import validate_clinical_config
    validate_clinical_config(data)
    out = PROJECT / "config" / "clinical_endpoints.v1.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(out), "records": len(data["records"]), "audits": len(data["study_audits"])}))
