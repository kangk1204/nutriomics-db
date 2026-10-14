"""Render the Data Descriptor draft from measured release/package metadata."""

import argparse
import json
from pathlib import Path


def write_descriptor(manifest, public_metadata, output):
    m, public = manifest, public_metadata
    c = m["counts"]
    kinds, layers = c["by_kind"], c.get("workbench_by_layer", {})
    clinical = layers.get("clinical", 0)
    text = f"""# NutriOmics: a versioned evidence dataset connecting food composition, human dietary interventions and gene regulation

**Draft status:** Local Data Descriptor draft, not submitted or published. Release {m["release_id"]}. Authors, contributions, funding, dataset DOI, final licensing and institutional ethics determination require author confirmation. Counts below were read from the built release and permitted package, rather than estimated from a plan.

**Author information:** [TO CONFIRM: author list and order]. Corresponding contact supplied for project preparation: Keunsoo Kang, Dankook University, Department of Biomedical Science [official English affiliation to confirm], kangk1204@dankook.ac.kr.

**Dataset:** NutriOmics evidence dataset, {m["release_id"]}. Creator and publisher: [TO CONFIRM]. Public repository and DOI: [NOT YET DEPOSITED].

**Dataset license:** The original-source catalog and derived dataset require a final license decision. Third-party records retain source-specific conditions. No blanket license is assigned to unassessed third-party values. The accompanying SQLite subset contains records allowed by the recorded export policy. The full internal Explorer projection is not the same as this subset.

## Abstract

NutriOmics organizes food composition, human dietary intervention results and external gene-regulatory evidence in a versioned research dataset. The internal release contains {kinds.get("food", 0):,} food records, {c.get("measurement", 0):,} composition measurements, {c["node"]:,} entities and {c["edge"]:,} relationship records. Dietary results retain study, tissue, exposure, contrast and analysis-version identifiers; expression and methylation results are referenced without duplicating the underlying effect matrix. The regulatory extension distinguishes observational CpG–expression associations, experimentally tested regulatory-element–gene pairs and computational predictions. Native identifiers, source hashes, missingness and redistribution conditions are preserved. A local workbench supports source-backed comparisons, bounded network exploration and snapshot export. Repeatable acquisition and immutable build code allow future releases to be constructed without overwriting their inputs. The dataset supports hypothesis development and evidence inspection; it does not estimate individual molecular responses or disease risk. The data package and technical validation are described separately from expert interpretation and public repository deposit.

**Keywords:** nutrigenomics; food composition; dietary intervention; DNA methylation; gene expression; provenance; evidence dataset.

## 1. Summary

Food composition and molecular study results are represented at different levels of observation. A measured nutrient amount describes an edible food record and its reference basis. A dietary contrast describes a study population, exposure and sampling design. An external CpG–expression association describes variation observed in a specific tissue or cohort. NutriOmics preserves these distinctions while providing a shared route for inspecting their relationships.

The dataset extends an existing research database with an immutable presentation index and a regulatory evidence layer. Blood pressure, lipid measures and obesity form the initial health-navigation scope. A disease association is displayed separately from a measured clinical endpoint. Relationships from different studies can be followed as candidate paths, but the resulting path is not treated as an intervention effect or a causal chain.

The Framingham Heart Study provides published whole-blood methylation–expression summaries and separate replication results [1]. Primary-monocyte eQTM evidence is taken from the original EvoImmunoPop publication [2]. SCREEN provides candidate regulatory-element annotations and gene links [3]. Their records support different questions and are not pooled into a common effect estimate. The contribution of NutriOmics is an inspectable data structure, preservation of study context and repeatable release generation. No claim of global database completeness, superior retrieval accuracy or clinical utility is made without a completed comparison evaluation.

## 2. Data Description

### 2.1. Release and counting units

Table 1 reports the full internal presentation release. Relationship, assay, feature, publication and participant counts are distinct units. The dataset does not report the sum of relationship records as a cohort size. Native gene namespaces remain separate unless an independently verified mapping is available.

| Record class | Count | Counting unit |
|---|---:|---|
| Food | {kinds.get("food", 0):,} | Original food record; same-name foods are not merged |
| Diet/exposure nodes | {kinds.get("diet", 0):,} | Source-specific intervention or diet node |
| Composition | {c.get("measurement", 0):,} | Food–component measurement with unit, basis and status |
| Gene nodes | {kinds.get("gene", 0):,} | Native namespaced identifier, not deduplicated biological genes |
| CpG nodes | {kinds.get("cpg", 0):,} | Original cg identifier |
| Other methylation probes | {kinds.get("methylation_probe", 0):,} | Native non-cg probe identifier |
| Regulatory-region nodes | {kinds.get("regulatory_region", 0):,} | Native ENCODE cCRE identifier |
| All relationship records | {c["edge"]:,} | Typed relation records; includes composition |
| eQTM records | {layers.get("eqtm", 0):,} | Cohort- or group-specific CpG–expression result |
| Tested cCRE–gene pairs | {layers.get("regulatory_tested", 0):,} | Tested pairs; not classified here as positive functional effects |
| Predicted cCRE–gene links | {layers.get("regulatory_predicted", 0):,} | GraphRegLR prediction records |
| Clinical endpoint records | {clinical:,} | Recorded human endpoint comparison |
| Original source records | {c["source"]:,} | Recorded source release/curation entries |

The dietary evidence layer retains the study and contrast catalog from the parent release. Omics reference rows retain the existing key `(series, feature_type, feature_id, contrast, analysis_version)`. A reference may appear under multiple gene annotations; it is not an additional participant, assay or independent experiment. No core effect-value rows were copied by this extension.

### 2.2. Regulatory summaries

FHS discovery entries correspond to published selected cis/trans summaries rather than all tested pairs. The ST3/ST4 adapter adds 4,000 WHI/JHS replication records, including 300 unreported results. Cohort-level participant counts are stored in study metadata; pair-specific counts remain unreported when absent from the table. Discovery and replication coefficients are kept separately and are not combined across expression platforms. This replication is unrelated to validation of a dietary-response prediction.

SCREEN acquisition processes every supported row of the pinned input files without a selected gene panel. The row-level method determines inclusion. GraphRegLR rows are stored as predictions, while other methods in the same file are counted as excluded by this adapter. Repeated identical raw rows are deduplicated using source and native-field hashes. CRISPR rows describe tested pairs. Original numeric columns are retained with untyped definitions until their statistical meaning is verified; they are not relabeled as effect sizes, P values or FDRs.

Native hg38 source intervals can be displayed in the workbench. Their coordinate convention remains source-native unless independently verified. Unknown coordinates or genome builds are excluded from the position view. No proximity-based gene assignment, interval overlap inference or cross-build conversion is performed.

Approved HGNC rows supply {c.get('by_relation', {}).get('verified_gene_identifier_cross_reference', 0):,} exact NCBI Gene–Ensembl cross-references between identifiers already represented in the parent dataset. The adapter audits the complete pinned TSV, excludes ambiguous mappings and does not match symbols or remove identifier version suffixes. Identifier equivalence is a separate descriptive relation and does not combine regulatory statistics or establish a dietary effect. HGNC data are provided under CC0; attribution and snapshot hashes are retained [5].

### 2.3. Files and schema

The permitted SQLite package has {public["counts"]["node"]:,} nodes, {public["counts"]["edge"]:,} relations and {public["counts"]["measurement"]:,} composition measurements. These counts differ from Table 1 because redistribution conditions are applied. External regulatory values currently withheld by policy are obtained from the original repositories through the acquisition code. `dataset-metadata.json` records the subset scope, parent hash and withheld counts. `source-catalog.json/csv` describes original source identifiers, URLs, versions, hashes and rights status. `explorer-schema.sql` and `schema-columns.csv` document the complete internal presentation schema. `SHA256SUMS.json` records package-file hashes.

Food composition records include food and component identifiers, amount, unit, reference basis, value status and source. Gene-regulatory records include subject/object identifiers, relation, evidence layer, organism, tissue, study/source references, effect definition, statistics, missingness and original context. Expert-review records are tied to the hash of the exact evidence payload. Source-preservation checks do not constitute expert biological approval.

## 3. Methods

### 3.1. Acquisition and version control

The update registry declares HTTPS source URLs, allowed hosts, format, byte budget, adapter and export policy. Conditional requests use source validators where available. Interrupted downloads may resume only when byte identity is supported by a retained validator. Received objects are checked against expected hashes when supplied and are stored by their computed SHA-256. A changed source creates a new object and receipt; previous objects remain unchanged.

Adapters create independent SQLite sidecars with source-native entities and original row locations. They reject unexpected schemas, non-finite numeric fields and unsupported identifiers. A single writer lock prevents simultaneous release updates within the same update root. Download, parsing, projection and production promotion are separate stages. A failed acquisition or build preserves the existing active release.

### 3.2. Evidence projection and interpretation

The presentation builder first verifies and backs up the immutable parent SQLite release. Accepted sidecar records are then projected with their source hashes. Candidate or rejected source rows are withheld from the accepted evidence layer. Identical raw rows across overlapping source collectors are deduplicated. New versions replace only the previous derived records for the same source family within the new derivative. Older releases retain their prior contents.

Name similarity is not used to establish chemical or gene identity. Native symbols supplied by a source can support search aliases, while the original namespace and identifier remain visible. Nutrient groups and single compounds retain separate types. CpG annotation, observational eQTM, predicted regulatory links and tested pairs retain separate relation classes. Non-significant results and unreported values are not converted into zero effects. A source-reported P value of zero is marked as unresolved precision/underflow rather than an exact zero probability. Published inequality bounds are preserved where available. Unreported FDR and hypothesis families remain missing.

Food quantity calculations use existing verified food IDs and the recorded measurement basis. The extension does not infer density for a 100-mL record or multiply already cooked composition by another yield factor. Similarity to a dietary experiment is represented by separate checks for identity, dose, duration, tissue and comparison conditions. Intake amounts are not converted to blood or cell-culture concentrations.

### 3.3. Workbench and technical validation

The service uses FastAPI, SQLite and a React/TypeScript frontend. The recorded frontend dependency versions are React 19.2.0, Cytoscape.js 3.33.1, TanStack Table 8.21.3, ECharts 6.0.0, TypeScript 5.9.3 and Vite 7.3.1. Cytoscape.js is used for bounded relationship exploration [4]. A table presents comparable study contexts and statistical definitions before a graph is opened. Related records can be pinned for comparison. Expression/methylation results are requested through their complete analysis key. The position display uses original verified-build intervals. Independent HTML exports embed the selected data and application assets, with prohibited values withheld.

Technical checks include SQLite integrity and foreign keys, unchanged input hashes, source schema and method separation, exact record origins, missingness, tissue filtering, analysis-key validation and export restrictions. Regression tests cover the existing API and scientific-matching layer. Atomic promotion and restoration are checked on Linux. Performance and browser checks are reported with the actual queries, release and environment. Completed technical checks do not establish expert retrieval precision. Human interpretation review, controlled usability evaluation and a held-out retrieval benchmark are separately identified as pending where not performed.

## 4. Usage Notes

Researchers can search a food, component, gene, CpG or regulatory region and inspect the original conditions behind each relationship. The default food quantity is 100 g, while an exported snapshot preserves its selected quantity and reference context. Unknown reference conditions remain unknown. Trial results apply to their recorded population, exposure, timing and tissue. The graph does not infer that an arbitrary food changes every downstream gene or improves the linked disease.

Update code and command examples are provided in `WORKBENCH_UPDATE_ko.md`. A new immutable release ID is required for each build. Source changes that require revised omics preprocessing or statistical models receive a separate analysis version and are not silently recalculated by the source updater. The web service remains bound to 127.0.0.1 and is accessed through the existing tunnel. Login, public hosting and personalized risk estimation are outside this release.

The full internal projection contains source-specific material that is not yet approved for redistribution. The public-review package therefore includes only permitted values, source facts, schema and reconstruction code. Further rights review may permit a larger deposited dataset. Repository deposit, DOI assignment and final source-license decisions must be completed before the manuscript can claim an openly available dataset.

## Back Matter

**Supplementary materials:** Release manifest and coverage; original-source catalog; schema and column dictionary; package hashes; code and environment locks; update guide; actual technical verification receipts. [ADD AFTER COMPLETION: final human-review and benchmark results, if performed.]

**Author contributions:** [TO CONFIRM; do not infer from tool activity.]

**Funding:** [TO CONFIRM; do not copy project/grant values without evidence.]

**Institutional Review Board statement:** This extension uses existing public source summaries and derived research records and does not recruit participants. [TO CONFIRM: institutional determination and any original-data restrictions; this statement is not an IRB exemption determination.]

**Informed consent statement:** [TO CONFIRM: applicability and original-study conditions.]

**Data availability statement:** The manuscript and data/code package are currently local review artifacts. Public repository and DOI are not yet assigned. Original datasets are accessed through the source URLs and accessions in the source catalog. The complete internal release and the permitted export subset have different coverage because of source-specific redistribution conditions. Final deposited files and permanent identifiers must replace this statement before submission.

**Code availability:** Repeatable acquisition, sidecar generation, immutable projection, validation, rollback and rights-filtered packaging code accompany the review package. [TO CONFIRM: public repository, tagged commit, archive DOI and software license.]

**Generative AI disclosure draft:** An OpenAI coding assistant assisted implementation, technical troubleshooting and manuscript drafting. Data records were produced from deterministic source adapters; the assistant did not create molecular measurements or clinical observations. Automated literature candidates remain distinct from expert-reviewed assertions. The authors will verify scientific claims and take responsibility for the final manuscript. [TO CONFIRM: final tool/model details and disclosure wording.]

**Conflicts of interest:** [TO CONFIRM.]

## References

1. Keshawarz, A., et al. Expression quantitative trait methylation analysis elucidates gene regulatory effects of DNA methylation: the Framingham Heart Study. *Scientific Reports* **2023**, *13*, 12952. https://doi.org/10.1038/s41598-023-39936-3. PMID: 37563237.
2. Husquin, L.T., et al. Exploring the genetic basis of human population differences in DNA methylation and their causal impact on immune gene regulation. *Genome Biology* **2018**, *19*, 222. https://doi.org/10.1186/s13059-018-1601-3.
3. SCREEN. About the ENCODE Registry of cCREs. https://screen.wenglab.org/about. Accessed 9 October 2026. The pinned input release is V4-hg38; source hashes are recorded independently of the current website release.
4. Cytoscape.js. Official documentation. https://js.cytoscape.org/. Accessed 9 October 2026.
5. HGNC. Download files overview and license agreement. https://www.genenames.org/download/; https://www.genenames.org/about/license/. Accessed 9 October 2026.
"""
    Path(output).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--dataset-metadata", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    write_descriptor(
        json.loads(a.manifest.read_text(encoding='utf-8')), json.loads(a.dataset_metadata.read_text(encoding='utf-8')), a.output
    )
