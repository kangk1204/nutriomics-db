# Public NutriOmics data dictionary

The definitive structural definition is the release's `schema.sql`. This file
defines the scientific meaning and limits. Use `dataset-metadata.json` for
actual public counts; internal counts are not substituted.

| Table | Grain and key | Meaning |
|---|---|---|
| node | source entity; `id` | Food/state/release, component, native gene namespace, or other permitted entity |
| measurement | `id`, food/component/source | Original amount, unit, basis and measurement state; not a blood concentration |
| source | source/version/hash; `id` | Provenance and source-specific permissions; includes excluded-source catalog entries |
| edge | `id`, subject/object/relation | Source relation or candidate correspondence, not automatically causality |
| science_record | kind + `id` | Study, exposure, arm, contrast, assertion, analysis run and curation metadata |
| effect | five-part key; stable `effect:` hash | Processed feature-by-contrast-by-version quantitative result |
| effect_selector | accession/contrast/version/type | Exact result count; not independent participants |
| food_alias (service derivative) | stable food ID + legacy number | Compatibility lookup; source food ID remains the portable identifier |

## Composition

`amount` and `unit` retain their source definition. `basis=source_100g` is
eligible for gram scaling only when its original basis has been verified.
Other bases are retained and excluded from automatic gram conversion.
`status` distinguishes actual zero, missing, unverified and below-detection
states where the source permits this distinction. An unreported value is null,
not zero. Preparation, recipe and yield information must agree before combining
records. Group analytes such as total fat or total vitamin C are not necessarily
a single chemical molecule; a name correspondence does not establish identity.

## Study and exposure context

An exposure distinguishes food/product/mixed meal/diet/component, formulation,
assigned amount and unit, dose basis and duration. A contrast retains study,
arms, comparison, response time, tissue and analysis version. Assigned dose is
not proof of consumed dose. A whole-diet contrast is not the effect of one food.
Unknown information remains null or an explicit unresolved metadata state.

`n_people` describes the independent people reported for the result/contrast.
`n_samples` retains the producer's source-sample/assay scope; consult
`sample_scope`, fitted-model counts, array and channel counts. These can differ.
Repeated visits, matched modalities, SubSeries, two-color channels and historical
analysis versions are not new independent people.

## Quantitative effects

The five-part key is `series_accession, feature_type, feature_id, contrast,
analysis_version`. Each part is necessary. `feature_id` is the original assay
feature, and `feature_label` is not proof of a verified identifier equivalence.

`effect_estimate` uses `effect_unit`; RNA log-fold change and methylation beta
change have different meanings. `standard_error` and confidence intervals must
be interpreted using the producer's uncertainty-scale definition. Historical
outputs with missing or different uncertainty definitions are not silently
converted to beta-scale SE. `p_value`/`q_value` retain the original contrast or
study-family scope, as defined in analysis metadata. The same numeric FDR value
does not imply the same hypothesis family across analyses.

Fixed-window methylation-region summaries are not automatically statistical
DMRs. Genome build and annotation are required to interpret a genomic interval.
Nearest-gene annotations, observational eQTM, tested regulatory pairs and
predicted enhancer links remain different relationships.

`source_output_sha256` binds the derived result to an audited producer output.
`rights_policy_id` binds the public inclusion to a release-specific policy.
SQL/Parquet nulls preserve missing values; non-significant and opposing results
are retained. Query failures and exclusion from a public release are not zero
effects or evidence of biological absence.

## Review and release

`source_checked`, automated checks, and integrity validation are distinct from
human scientific approval. Human decisions require a matching record SHA256,
reviewer, date, primary-source location and reason. One human reviewer does not
provide inter-rater agreement or independent peer validation.

Every output uses the same release identifier. The manifest lists file hashes,
source/version scope and exclusions. Source terms do not change to CC BY merely
because a file is downloadable. The public subset cannot restore the complete
restricted internal database. Clinical benefit and individual risk are not
derived automatically from gene direction, methylation change or graph paths.
