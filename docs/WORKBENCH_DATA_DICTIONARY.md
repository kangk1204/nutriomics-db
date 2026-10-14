# NutriOmics evidence definitions

The release-generated `schema-columns.csv` lists the actual SQLite columns. This document defines their scientific meaning.

| Area | Meaning and constraints |
|---|---|
| `node.id` | Original namespaced identity. Names and symbols support search; they do not merge namespaces. A gene-node count is not a unique biological-gene count. |
| Food `metadata` | Original code, dataset/preparation state and exact verified `core_food_id`, when available. Absent mapping disables the existing food calculator. |
| `measurement` | Amount, unit, basis and status from a food composition source. A recorded zero, missing value and detection limit are different states. |
| `edge` | Typed source relation, with original provenance. Edge count includes composition; it must not be labeled a count of biological interactions. |
| `source` | Source-specific version, URL, hash and rights context. Public access and redistribution permission are separate. |
| `workbench_evidence.layer` | `eqtm`, `regulatory_tested`, `regulatory_predicted`, `clinical`, or preserved original mechanism/identity layer. These are distinct statistical and scientific evidence classes. |
| `relation` | Original observation or prediction type. A tested cCRE–gene pair is not automatically a positive regulatory effect. |
| `effect` and `effect_unit` | Original statistical estimate and its scale; correlation, regression coefficient, differential expression and clinical mean difference cannot be pooled by numeric similarity. Missing means unreported. |
| `p_value_original` | Original inequality or a precision/underflow note. P=0 in an exported source is not treated as an exact zero probability. |
| `q_value` and `hypothesis_family` | Source-adjusted statistic and original test family. No unreported FDR is generated. Contrast-specific and study-family FDR remain separately versioned in the underlying analysis. |
| `n_people` | Participants for that result when reported. A parent-cohort count is stored separately in study metadata and does not fill unknown pair-specific N. |
| `n_samples` | Assays/sample records when reported; visits and channels are not additional people. |
| `tissue` and `organism` | Original experimental context. K562/THP-1/Jurkat are not relabeled as whole blood. Unknown source species stays unknown. |
| `source_ref` | Original sheet/row, source line, table or publication location. |
| `context.source_raw_fields` | Source-native fields with source hash, method and original interval. Untyped numeric fields stay untyped. |
| `genome_build` | Original verified source build for position display. Unknown builds are not assigned by default. |
| `coordinate_convention` | Original indexing convention; unresolved values are visible and not used for overlap inference. |
| `review_status` | Source-preservation/curation status. `source_checked` does not mean human biological approval. |
| `expert_review_status` | Actual recorded reviewer decision for the matching payload hash, otherwise `pending`. |
| `export_allowed` | Affirmative source/row redistribution policy. False/unknown retains source links or withheld explanations, not prohibited numeric values. |
| `omics_reference` | Existing five-part analysis key and source gene annotation. An annotation is separate from an eQTM or experimentally demonstrated regulation. |
| Native feature type | A request for `gene_expression` may resolve to one declared native label (`gene` or `expression`). The response and five-part key retain that native label; types are never pooled. Missing or ambiguous types return unavailable with null total. |
| `science_record` | Preserved studies, exposures, arms, contrasts, assertions and analysis runs. Their original participant and comparison designs remain authoritative. |
| `applicability` | Separate match/mismatch/unknown conditions. No averaged score turns unknown applicability into a clinical prediction. |
| `workbench_record_origin` | Hash of source version, relation, native identities and raw row identity. Overlapping collectors cannot inflate the same source row. |
| `extracted_only` | Precomputed presentation eligibility for nodes supported only by automated extraction. It controls display; it is not a biological annotation. |

The internal graph may contain multiple-source candidate paths. These are not independent studies or food-to-disease causal effects. The rights-filtered portable package has its own counts and does not claim full internal coverage.
