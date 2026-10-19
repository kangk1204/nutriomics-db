"""Draft from actual public-release metadata; unresolved claims remain explicit."""
import argparse
import json
from pathlib import Path


def write(metadata_path, review_path, output):
    data = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
    review = json.loads(Path(review_path).read_text(encoding="utf-8"))
    counts = data["counts"]
    kinds = counts["science_kinds"]
    node_counts = counts["node_kinds"]
    versions = data["release_id"]
    review.setdefault("fact_candidates", review.get("primary_paper_fact_candidates", 0))
    clinical_n = kinds.get("clinical_endpoint_result", 0)
    clinical_audits = kinds.get("clinical_study_audit", 0)
    predictions = counts.get("by_tier", {}).get("predicted", 0)
    prediction_percent = 100 * predictions / counts["edge"] if counts.get("edge") else 0
    linked_foods = counts.get("composition_backed_foods")
    linked_foods_text = f"{linked_foods:,}" if isinstance(linked_foods, int) else "not separately inventoried"
    composition_quality = data.get("quality", {}).get("composition", {})
    source_states = composition_quality.get("source_states", {})
    methods = composition_quality.get("methods", {})
    composition_description = "Source-state and estimation-method distributions are retained in the release metadata; composition records are not a count of independent laboratory assays."
    if source_states:
        state_text = "; ".join(f"{key}: {value:,}" for key, value in source_states.items())
        method_text = "; ".join(f"{key}: {value:,}" for key, value in methods.items())
        composition_description += f" Source-state inventory: {state_text}. Method inventory: {method_text}."
    text = f'''# NutriOmics: A Versioned Dataset Connecting Food Composition with Human Dietary-Intervention Transcriptomic and DNA Methylation Results

**Data Descriptor draft. Candidate release: {versions}. Scientific review, final evaluation, version DOIs and public hosting are pending. This file is not a submitted or accepted manuscript.**

Authors, final affiliations, funding, conflicts of interest and institutional ethics determination: author confirmation required.

## Abstract

Food composition and dietary-intervention omics data are commonly distributed in separate resources, which makes experimental conditions difficult to retain during reuse. NutriOmics connects source food records and quantitative composition with structured descriptions of human dietary interventions, comparison conditions and processed molecular results. The candidate public package contains {node_counts.get('food', 0):,} food entities, {counts['measurement']:,} composition records, {kinds['study']} study records, {kinds['exposure']} exposures and {kinds['contrast']} contrasts. Its {counts['effect']:,} quantitative records describe features, contrasts and analysis versions; they do not represent independent participants. Source-specific permissions determine the deposited scope, while source identifiers, analysis keys, output hashes and review state preserve provenance. SQLite, Parquet and compressed CSV support programmatic reuse, and a read-only workbench supports food, study and result exploration. The dataset provides a framework for inspecting experimental evidence under specified dietary conditions. It does not estimate an individual's molecular response or disease risk. Human scientific review and the final release validation remain outstanding for this candidate.

Keywords: nutrigenomics; food composition; dietary intervention; transcriptomics; DNA methylation; provenance; data reuse

Dataset title: NutriOmics food composition and human dietary-intervention omics dataset.

Dataset identifier: `{versions}`; exact deposited version DOI pending. Dataset creators and publisher: final author list and repository confirmation required. Repository URL: pending.

Dataset license: CC BY 4.0 for project-authored curation and permitted project-derived results. Upstream composition and external records retain the source-specific terms in the catalog; a blanket third-party license is not asserted.

## 1. Summary

Connecting a food record with a molecular result requires more than a shared nutrient name. The administered product, dose, duration, tissue, population and comparator determine which result is relevant. A nutritional mixture also changes several constituents together, so a meal comparison rarely identifies the independent effect of every constituent. NutriOmics retains these distinctions in a versioned dataset that connects composition with dietary-study context and processed results.

Related resources support different parts of this task. FoodAtlas connects foods, chemicals and biomedical relationships, including source-specific reuse limits [1]. Eat4Genes provides a human dietary gene-expression resource and exploratory interface [2]. NutriGenomeDB supports nutrigenomics signature exploration, including experiments in cell systems [3]. The present dataset focuses on retaining quantitative exposure conditions, intervention contrasts, transcriptomic and DNA methylation results, and the distinction between observed and externally inferred relationships. These are scope differences; comparative superiority and first-of-kind claims are not made.

The release separates four primary data grains: food-composition records, project-authored intervention curation, quantitative molecular feature-level results, and source-located clinical summaries. External regulatory context is a separate layer. Values whose redistribution conditions are unconfirmed are excluded, with source access information and an exclusion reason retained. Internal workbench counts are not reported as deposited dataset counts.

## 2. Data Description

### 2.1. Files and identifiers

The public candidate is provided as a SQLite database, table-specific compressed CSV, analysis-specific Parquet and CSV, a data dictionary, source catalog, rights policies, exclusion records, and a file manifest. SQLite and Parquet preserve typed nulls and numerical values. CSV empty cells require the documented null convention; SQLite or Parquet should be used for exact type restoration.

Food identifiers retain their source code, preparation state and source release. Food names alone do not determine identity. Molecular results use the key `series_accession + feature_type + feature_id + contrast + analysis_version`. The public result identifier is derived from this key, so reuse does not depend on an internal database row number or a laboratory-specific path. Analysis versions remain separate; historical and revised results are not pooled as additional independent subjects.

| Public data grain | Records | Definition |
|---|---:|---|
| Foods | {node_counts.get('food', 0):,} | Source food entities; different preparations/releases remain distinct |
| Composition-linked foods | {linked_foods_text} | DISTINCT measurement.food; different from all food identifier nodes and not a guarantee of usable quantities |
| Composition | {counts['measurement']:,} | Food × component × source records; measured, derived, estimated and missing states remain distinct |
| Study curation | {kinds['study']} | Study records; trial membership is audited separately |
| Exposures | {kinds['exposure']} | Products, meals, dietary patterns or components |
| Contrasts | {kinds['contrast']} | Explicit comparisons, tissues, times and analysis versions |
| Assertions | {kinds['assertion']} | Curated descriptions with source and review state |
| Analysis records | {kinds['analysis_run']} | Version-specific execution and provenance records |
| Quantitative results | {counts['effect']:,} | Feature × contrast × analysis-version records |
| Clinical summary candidates | {clinical_n} | Source-located baseline, serial or crossover summaries; expert review pending |
| Study clinical audits | {clinical_audits} | Quantitative availability and unassessed scope, not a count of successful clinical effects |
| Regulatory predictions | {predictions:,} | Cell-line computational links; separate from dietary observations |

All figures and tables must be regenerated from the same release metadata before submission. Food identifier nodes, composition-linked food identifiers and prior food-registry count labels are different grains. A source food may have no composition record; such absence is not a measured zero. Count-definition corrections do not imply that composition rows were deleted. Entity namespaces are not equivalent to counts of unique biological genes. Relationship counts, publication counts and independent-study counts are separate quantities.

{composition_description}

### 2.2. Intervention context

`science_record` stores study, exposure, arm, contrast, assertion and analysis metadata. Exposures distinguish assigned dose from measured consumption and mixtures from individual chemicals. Contrasts describe within-person changes or differences in changes, as applicable to the source analysis. The public contrast register includes {counts.get('pharmacological_contrasts', 0)} pharmacological comparator(s); fenofibrate in GSE27385 is retained separately from dietary comparisons. Source participant counts, biological-sample counts, array/channel counts and fitted-model counts are retained separately when available; missing or conflicting scopes remain unresolved.

The dataset does not treat SuperSeries and SubSeries as independent studies. A published randomized trial does not imply that every publicly deposited subset is a randomized controlled comparison. An active-group pre/post omics subset is labelled accordingly. Likewise, weight-group comparisons and external healthy samples are not converted into randomized dietary controls.

### 2.3. Molecular results and regulatory context

Results retain the original feature type, feature identifier, effect unit, effect estimate, uncertainty when reported, P value, FDR and analysis version. Non-significant and opposing results remain available. Unreported results are not set to zero. DNA methylation tests and interpretable beta-scale effects may use different models; the analysis metadata defines the scale and scope of each statistic. A missing uncertainty-scale definition requires scientific review and is not inferred from the effect label.

CpG proximity annotations, eQTM associations, tested regulatory pairs and predicted enhancer links have separate meanings. An eQTM is an association, a tested pair is not necessarily a positive regulatory result, and a prediction is not an experimentally established dietary effect. Unassessed external payloads are not included in this candidate merely because their source can be downloaded.

### 2.4. Clinical summaries and evidence-layer balance

The release contains {clinical_n} source-located clinical summary candidates and {clinical_audits} study availability audits. Baseline characteristics, repeated group summaries and crossover iAUC summaries remain distinct. Means and standard deviations, medians and ranges, and medians with interquartile ranges retain their original definitions; none is silently converted to a causal effect, standard error or confidence interval. Original and multiplicity-adjusted comparisons are retained separately. The participant subset used in the clinical publication is not assumed to match the omics subset.

A shared study identifier supports co-navigation of clinical and molecular results, without establishing genomic mediation. Missing full-text tables, incomplete rights review and unassessed outcomes remain availability states rather than zero effects or statements that an endpoint was not measured. The source-labelled yogurt lipid iAUC time-integration unit is unresolved; no concentration-time conversion is performed. The NAFLD serial-study omnibus and adjusted pairwise endpoint tests are reported separately. The source's mean imputation of missing clinical values and unknown observed count per endpoint/time cell remain limitations.

The ancillary GraphRegLR layer contains {predictions:,} cell-line predictions, accounting for {prediction_percent:.1f}% of public graph relationships. Relationship totals include composition and identifier records and are not counts of experimentally validated food-health interactions. Predicted, source-curated, automatically extracted and human-reviewed states are distinct. All new clinical facts remain pending human scientific review in this candidate.

## 3. Methods

### 3.1. Acquisition, rights and release construction

Source records retain their original identifiers, access URLs, release information and available hashes. Project-authored curation and derived analysis outputs use CC BY 4.0 where an affirmative source/output policy permits inclusion. Own software uses MIT. Third-party source terms remain attached, and explicit record prohibitions override a general permission. GEO-derived results cite the original series and its recorded use policy [4]. DIRECT PLUS quantitative values remain excluded in this candidate pending a specific source-term assessment; its study context and exclusion reason are retained.

Construction uses an immutable Explorer parent and a consistent internal SQLite snapshot that includes committed WAL content. The original database is not checkpointed or overwritten for packaging. A new candidate directory is built and verified before atomic completion. An interrupted candidate is retained as a failed build rather than activated. The public package contains the permitted subset and does not reconstruct the complete restricted internal database.

### 3.2. Composition and matching

Intake calculations scale confirmed composition values by the entered edible quantity in grams. The original basis and preparation must be compatible. A 100 mL value is not converted to grams without density evidence. Missing, below-detection, unverified and actual-zero states remain separate. Direct mixture measurements take precedence over recipe summation, and cooking/yield corrections are applied only where their basis requires them.

Food-to-study matching distinguishes an exact product correspondence from a related food or component. Applicability reports food form, dose, duration, tissue and population separately as matched, mismatched or unknown. A serving is not used to scale a study's gene or CpG response, and ingested mass is not converted automatically to blood concentration.

### 3.3. Omics processing and provenance

The stored analysis metadata describes normalization, filtering, model formula, contrasts, included samples and statistical scope for each execution. RNA-seq analyses use recorded edgeR/TMM and limma-voom procedures; microarray and methylation workflows use platform-specific processing described in their execution records. The participant is the unit of independence for repeated measurements. Whole blood, PBMC, platforms and distinct studies are not simply pooled.

For methylation, the recorded primary analyses test M-value outcomes and retain beta-scale changes for interpretation. Uncertainty remains version-specific: the GSE54643 workflow exports the standard error of paired delta-beta summaries and retains M-scale estimates and standard errors in separate fields, whereas revised between-arm analyses use beta models for the corresponding contrast. A beta confidence interval is not inferred from an M-scale standard error. Historical outputs preserve their original definitions and require their own interpretation. Fixed 1 kb region tests remain distinct from discovered or validated DMRs. Probe annotations, genome build, cell-composition sensitivity analyses and hypothesis-family definitions are retained when supplied. Package and pathway-data versions are different provenance objects; a package version does not replace a gene-set file hash.

### 3.4. Scientific curation

The review package contains {review['records']} record-level review tasks and {review['fact_candidates']} primary-paper/repository fact candidates. Pending tasks do not represent completed decisions or gold labels. Each completed decision is bound to the original record hash, with reviewer, date, source location and reason required. Automated extraction and integrity checks support preparation; they are not independent expert review. One human reviewer is available for this project. Re-review may identify errors, but inter-rater agreement cannot be estimated from a single reviewer.

At this candidate stage, {review['expert_verified']} record-level tasks have recorded human approval. This count is not the number of expert-verified fact candidates or correctly answered sealed queries. Final scientific acceptance requires original publications, repository sample metadata and actual analysis inputs to be reconciled. Unresolvable records are excluded or remain explicitly unconfirmed.

## 4. Technical Validation

Validation separates value preservation, query correctness, performance, reproducibility and scientific interpretation. Full result comparisons check five-part keys, numeric/statistical fields, source metadata and typed missingness against the frozen source. Composition validation uses 50 selected Korean foods and 1,200 source measurements internally; that validation scope is distinct from the permitted public food subset.

Query benchmarks use frozen cases, 100 warm requests per case, separate first requests and four concurrent mixed requests. Graph caps are retained rather than reduced to achieve the target. Export validation checks selection, source policy, stable identifiers and standalone offline operation. Failure is not reported as zero evidence. New release changes invalidate related cache entries and hash-bound review decisions.

The scientific retrieval evaluation retains 70 development and 30 sealed queries. Expert gold labels must be fixed without inspecting final system outputs. Identifier/keyword retrieval, context-free linkage and context-aware linkage are compared on the same reviewed cases. Denominators, confidence intervals, repeated-paper dependence and failures are reported. This is an evidence-retrieval evaluation, not prediction of individual response or biological validation in an independent intervention cohort. Final scientific retrieval accuracy is currently not evaluated.

Final numerical validation results and figure 4 must be filled from hash-bound receipts for this release. Preliminary timings, unit tests and older release benchmarks are not substitutes for the final validation package. Exact producer-code hashes establish which code was preserved; re-downloaded source-file hash agreements establish source reproducibility. Neither establishes expert validity of all analysis inputs, model assumptions, clinical interpretations or dietary effects.

## 5. Usage Notes

A yogurt query of 150 g can retrieve an acute study that administered 800 g, with the quantity difference and unverified product identity displayed. A fatty-acid query can inspect the PUFA-versus-SFA six-hour PBMC comparison while retaining meal composition and tissue context. A single-food query related to a long-term dietary DNAm study remains a whole-diet association rather than an independent effect of that food. Gene direction and methylation change are not converted into health benefit or harm.

Source composition may be unavailable for an input, a study may have insufficient sample mapping, or a molecular result may lack usable uncertainty. These are valid dataset states. They should not be replaced with zero values or simulated connections. Users should cite the exact deposited version and retain source-specific terms when combining the dataset with other resources.

## Code and Data Availability

Own code: MIT; https://github.com/kangk1204/nutriomics-db . The tested candidate tag is recorded in the code-release receipt; the final submission must identify the exact matching tag and version DOI. Data and code version DOIs: pending. Public HTTPS URL and operational handover: pending. The public service loads only a specified permitted release and provides no acquisition, write or reanalysis routes. Monthly source checks create change candidates; expert approval and publication are separate operations.

## Author Statements

Author contributions/CRediT: confirmation required. Funding and grant identifiers: confirmation required. Conflicts of interest: confirmation required. Institutional review/ethics statement for reuse of public de-identified datasets: institutional/author determination required. No new participant recruitment is part of this dataset project.

Generative AI assisted code implementation, source checking and preparation of this draft. Scientific review and responsibility for the final dataset and manuscript remain with the human authors. The exact tool/version disclosure and journal-required wording require final author confirmation.

## References

1. A unified knowledge graph linking foodomics to chemical-disease networks and flavor profiles. *npj Science of Food*. https://doi.org/10.1038/s41538-025-00680-9
2. Eat4Genes: a bioinformatic rational gene targeting app and prototype model for improving human health. *Frontiers in Nutrition*. 2023. https://doi.org/10.3389/fnut.2023.1196520
3. NutriGenomeDB: a nutrigenomics exploratory and analytical platform. *Database*. 2019; baz097. https://doi.org/10.1093/database/baz097
4. NCBI. GEO disclaimer and use policy. https://www.ncbi.nlm.nih.gov/geo/info/disclaimer.html

Source-study citations and full author/year bibliographic fields must be finalized from the {kinds['study']} study records after scientific review. This draft does not claim completed expert validation, SOTA superiority, causal dietary effects or individual prediction.
'''
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return {"path": str(output.resolve()), "release_id": versions, "submission_ready": False}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("metadata", "review", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(write(a.metadata, a.review, a.output)))
