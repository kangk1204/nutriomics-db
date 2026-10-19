# NutriOmics clinical endpoint source audit

Audit date: 2026-10-19. Layer: `clinical-source-facts-20261019-v1`.

The layer contains **44 source-located quantitative candidate records from four
open-access primary papers**, **nine study availability audits**, and **13
endpoint nodes**. All records remain **pending professor review**. The scope is
selected blood-pressure, lipid and obesity descriptors; it is not an exhaustive
clinical endpoint extraction or an independent systematic review.

## What each record means

The stored estimand is explicit: baseline median/range, arm/time mean/SD,
uncontrolled serial median/range, or crossover-order-specific median iAUC/IQR.
The layer does not subtract published group summaries to manufacture individual
change scores, paired effects, SEs, CIs, or causal dietary benefits. It preserves
non-significant and unreported tests without converting either to zero.

| Study | Quantitative candidates | Exact source scope | Remaining limitation |
|---|---:|---|---|
| S03, GSE54325/GSE54643 | 7 | [Milenkovic et al. 2014](https://doi.org/10.1371/journal.pone.0095527), Table 1: baseline BMI, BP and lipid medians/ranges, n=13 | Not intervention effects; expression/DNAm subsets differ. Table BMI unit is printed `kg/m`; the absent exponent is retained with a caveat rather than silently repaired. |
| S04, GSE107205 | 27 | [Arpón et al. 2018](https://doi.org/10.3390/nu10010015), Table 1: nine continuous endpoints × three 12-person arms, baseline/5-year mean(SD) | Within-arm tests and three-arm omnibus baseline/follow-up/change tests are distinct. No pairwise treatment effect is inferred. Binary diagnostic rows require clarification and were excluded. |
| S05, GSE219217 | 6 | [Sokolowska et al. 2022](https://doi.org/10.1186/s13148-022-01377-6), Table 1 C/D: n=18 cohort, days 0/30/60, median(range) and original P columns | Uncontrolled study. Source uses mean imputation; observed n per time/endpoint cell is not given. Original unadjusted and Bonferroni-adjusted tests are retained separately. |
| N15, GSE98645 | 4 | [Burton et al. 2018](https://doi.org/10.1371/journal.pone.0192947), [S3 table](https://journals.plos.org/plosone/article/file?id=10.1371/journal.pone.0192947.s008&type=supplementary): four lipid postprandial iAUC summaries, clinical n=13 | 800g acute exposure; order groups n=6/7 are distinct. RNA-seq n=7 is not clinical n=13. Source labels concentration units for iAUC but omits the integrated time unit; no unit conversion is made. |
| S01, GSE127530 | 0 | [Original DOI](https://doi.org/10.1016/j.jnutbio.2019.06.007) and [institutional author copy](https://escholarship.org/uc/item/6wf307cz) located | Clinical tables and redistribution scope not audited in this layer. Parent n=62, RT-PCR n=20 and RNA-seq n=5 must not be conflated. |
| S02, GSE56960 | 0 | [Original DOI](https://doi.org/10.1016/j.jnutbio.2017.02.012) and [institutional manuscript](https://arbor.bfh.ch/bitstreams/c778d67d-4e6d-484c-9bc5-b450ce435884/download) located | Retrieval timeout; Table 1 and supplementary clinical iAUC need transcription/rights review. Not labelled unmeasured. |
| N14, GSE13466 | 0 | [Original paper](https://doi.org/10.3945/ajcn.2009.28586), Figure 2 and methods identifiable in primary indexing | Publisher direct retrieval returned403; figure values/rights not audited. Clinical study has SFA/MUFA/PUFA, public omics has SFA/PUFA. Not labelled unmeasured. |
| N16, DIRECT PLUS | 0 | [Metabolism primary paper](https://doi.org/10.1016/j.metabol.2023.155594), [public study metadata](https://www.ebi.ac.uk/biostudies/arrayexpress/studies/E-MTAB-12527) | Registry/SDRF-only in this local release; direct full-text retrieval returned403. No completed local DNAm or clinical numeric analysis is asserted. |
| N06, GSE27385 | 0 | [Primary abstract](https://pubmed.ncbi.nlm.nih.gov/21429719/) includes lipid change values | Full-text contrast and plus/minus dispersion definition not audited; values not imported. Clinical n=20 differs from PBMC n=11; fenofibrate remains a drug comparator. |

Zero imported candidates means **not imported/audited**, not zero measured
effect, no clinical measurements, no significance, or no relevant evidence.
Study audit `not_measured` is deliberately null for all nine studies.

## Statistical checks that materially affect interpretation

- S05 BMI has an omnibus P=0.033. The T1–T3 unadjusted P=0.075 and adjusted
  P=0.226 do not support a significant baseline-to-day60 pairwise difference.
  The source narrative alone must not replace the numerical test scope.
- S04 HDL has P=0.022 for the three-arm comparison at five years, while the
  three-arm comparison of changes is `ns`. These are not interchangeable.
- S04 numeric arm values and their SDs are descriptive summaries. Three copies
  of a shared omnibus label, one per arm record, are not independent tests or
  independent trials.
- N15 four lipid intervention-test P values are non-significant; the record
  does not claim equivalence or that yoghurt has no lipid effects generally.
- S03 baseline BP/lipid summaries describe the 13-person pre-QC source group,
  not the smaller final gene-expression/DNAm participant sets.

## Provenance and redistribution

Four primary full-text JATS files and the N15 S3 PDF were downloaded from Europe
PMC or the original publisher. The download receipt records exact URLs, dates,
response status, byte lengths and SHA256 values. CC-BY4.0 permissions are checked
in each article's own `permissions` element; the supplementary S3 table has no
separate exclusion notice. The configuration stores only numerical facts, own
paraphrases, exact table/row locations and attribution metadata. It does not
copy a paper's full prose or third-party figures.

Raw sources, PDF text/layout inspection and `build_curated_facts.py` are retained
under `outputs/publication/2026-10-19/clinical-sources`. The build script parses
fixed XML table cells and validates its output. N15 PDF values were additionally
rendered and visually checked against the four transcribed rows. These are
machine-assisted source checks, **not expert scientific approval**.

Portable reproduction entrypoints are
[`scripts/reacquire_clinical_sources.py`](../scripts/reacquire_clinical_sources.py)
for the source-specific downloads/receipts and
[`scripts/build_clinical_source_facts.py`](../scripts/build_clinical_source_facts.py)
for generating the fixed candidate facts from those inputs. These producer files
belong in the code release with the configuration; no credentials are required.

## Integration contract

`nutrigenomics.clinical_endpoints.attach_clinical_endpoints(db, config)` is called
only while constructing a new derived release. It adds two science record types
(`clinical_endpoint_result`, `clinical_study_audit`), health endpoint nodes and
study-context links. It never writes `effect`, `measurement`, or their selectors.
It refuses duplicate installation and does not replace inherited science facts.
When the optional `workbench_evidence` table exists, it additionally adds one
clinical table row per candidate (44 rows), reusing exactly the graph edge ID.
Their review status is `source_checked_expert_pending`, with source-specific
export allowed. The common scalar effect/P/FDR/SE/CI fields remain null because
each typed record can contain different original comparison/test scopes.
`clinical_endpoints` and `hypothesis_comparisons` retain those source summaries
and test labels; a baseline descriptor is never converted to a treatment effect
to fill a generic table column.

Graph relations are `same_study_clinical_context` and
`reported_clinical_endpoint`. They mean common study context and published
summary, respectively. No gene→clinical causal edge is created. Outcome/study
metadata embeds the typed results and exact source references so the offline
HTML retains units, scopes, caveats and citations.

`study_clinical_summary(db, study_id)` returns the clinical candidate records,
study audit and explicit review/availability summary. Missing tables, unassessed
studies and true numeric zeros remain distinguishable. API callers preserve
existing response fields and add this summary.

## Human review and remaining work

The professor should review exact table-cell transcription, count scope,
statistical labels, unit caveats and coverage before marking a record approved.
A reviewer decision must bind the exact record SHA. The five studies without
curated quantities still need full-text/rights acquisition and endpoint audit.
Other endpoints within the four covered studies are not exhaustively extracted.
No dataset-level precision, independent agreement, complete clinical coverage,
individual response prediction, or journal readiness is claimed by this layer.

## Verification

Dedicated tests check source SHA/permissions when the raw audit bundle is
present, typed baseline/serial/iAUC semantics, source P-value scope, reported
range versus SD versus IQR, unit ambiguity, zero/missing separation, clinical
versus RNA-seq cohorts, provenance retention, missing-schema API behavior,
duplicate prevention and denied writes to protected quantitative tables.

Run `python -m pytest tests/test_clinical_endpoints.py -q` from the project root.
