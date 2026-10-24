# Current status

Updated: 24 October 2026. Base component: 22 October 2026; method-review collection: 24 October 2026.

[Overview](../README.en.md) · [한국어 안내](../README.md) · [Review guide](REVIEWER_GUIDE.md)

The repository provides code and configuration. The table describes a prepared database under
review. It is not a claim that the complete data archive has been published or scientifically approved.

## Prepared data

| Item | Count | Meaning |
|---|---:|---|
| Food records with composition | 5,794 | Source food records, including separate preparation states and food codes |
| Composition records | 368,208 | Food and component records, including their measurement status |
| Registered study groups | 9 | Study registrations; 8 have reanalysis results in this version |
| Analysis runs | 14 | Includes primary, historical and sensitivity analyses of shared study data |
| Quantitative result rows | 6,254,477 | Results by measured feature, comparison and analysis version |
| Computational regulatory links | 1,073,409 | GraphRegLR predictions; separate from measured dietary effects |
| Reported clinical result records | 44 | Curated source summaries awaiting scientific review |
| Core records awaiting human decisions | 263 | No expert-approved records are recorded in this version |

Neither result rows nor network links are participant counts. A verified total of independent
participants is not yet reported. Multiple analyses of the same people do not create additional
participants or independent studies.

The [machine-readable summary](reference-data-summary.json) records the data version, count
definitions and checksums of the source metadata. It allows the numbers on this page to be traced
to that version. The source archive itself is not included in this code repository.

## Work still in progress

| Item | Status |
|---|---|
| Human review | Decisions on 263 core records and two new pathway output versions remain pending |
| Final query evaluation | Awaits reviewed reference answers and a fixed final data version |
| Data access conditions | Source and output conditions need final confirmation before deposit |
| Data and code version DOIs | Not registered |
| Full data archive | Prepared for review; final downloadable deposit not published |
| External website | No public HTTPS service is provided at present |
| DIRECT PLUS extension | Primary and sensitivity/QC work is managed separately; final extension validation and inclusion review are incomplete; excluded from the counts above |
| Literature and model integration | Development in a separate branch; excluded from the counts above |

The project should therefore be described as a research database under review.
Current code availability does not mean that journal submission preparation is complete.

## Code versions

The default branch contains the current public code. Work on data analysis and literature
integration proceeds in separate branches. Results from another branch or a local experiment
should be reported with the exact code revision and data version used.

The data identifiers and existing result values are preserved when a new interpretation or
analysis version is prepared. New analyses receive their own versions; they are not additional subjects.

The GSE56960 method update selects 50,550 replacement rows and preserves their historical versions. It adds two output versions, not participants or independent fitted study models. The selected total remains 6,254,477 rows. The final merged code still requires its own operational performance checks.

[Structure and joins](DB_STRUCTURE.md) · [Code update and validation scope](UPDATES_20261024.md)
