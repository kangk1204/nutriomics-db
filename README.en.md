**English** | [한국어](README.ko.md)

# NutriOmics

NutriOmics connects food composition with gene-expression and DNA-methylation results from human dietary intervention studies. Researchers can calculate component amounts for a food portion, then examine the dose, duration, tissue, comparison and source of related studies.

The web interface also includes literature search, structured Korean summaries, source quotations, evidence maps and model evaluation.

[Database structure](docs/DB_STRUCTURE.md) | [Reviewer guide](docs/REVIEWER_GUIDE.md) | [Getting started](docs/GETTING_STARTED.md) | [Literature results](docs/reviewer/RESULTS.md)

![NutriOmics data structure and connections](docs/figures/db-structure/보고서용_DB_구조도.png)

<a name="무엇을-확인할-수-있나요"></a>

## What the system provides

| Function | Information available |
|---|---|
| Food and component search | Food identity, preparation state, composition, units and source |
| Portion calculation | Component amounts for a food and its mass in grams |
| Human study search | Intervention, study dose, duration, tissue and comparison |
| Omics results | Expression, methylation and pathway changes with statistical results |
| Literature search | Topics, genes, chemicals, structured summaries, source spans and identifiers |
| Evidence maps and review | Paper-level relation candidates and review decisions |
| Exports | HTML, Markdown, CSV and JSON reports |

For example, a 150 g yogurt portion produces a composition estimate for the selected food record. If a related study used 800 g, the interface shows that study dose and its observed response. Portion composition and study responses retain their own evidence and conditions.

<a name="db-구축-규모"></a>

## Database size

These counts describe the web-serving dataset dated 22 October 2026 and checked on 24 October. Definitions and hashes are in the [reference summary](docs/reference-data-summary.json).

| Item | Count | Unit |
|---|---:|---|
| Foods | **5,794** | Source-coded food records with preparation state |
| Composition records | **368,208** | Food, component and source records, including value status |
| Study groups | 9 registered; 8 with reanalysis data | Registry groups |
| Contrasts | 53 | Group, time and analysis comparisons |
| Analysis runs | 14 | Existing runs, including sensitivity analyses |
| Quantitative results | **6,254,477 rows** | Feature, contrast and analysis-version results |

An additional 50 Korean foods and 1,200 composition records belong to the local research dataset. The larger project-wide ingestion database has a separate count in the [report repository](https://github.com/kangk1204/nutriomics-final-report).

<a name="문헌-분석-결과"></a>

## Literature results

At 12:46 KST on 24 October 2026, the system held **44,828 papers**, including 43,365 abstracts and 2,314 open full texts. Extraction results matching the stored abstract or full text were available for **28,977 papers**, with 9 registered reports. A separate search inventory contained 133,261 unique PMIDs.

| Task | Result | Evaluation |
|---|---|---|
| Chemical-gene relation presence | **AUROC 0.876**, 95% interval 0.851 to 0.898 | 224 eligible papers from 400; 4,362 pairs; supplied gold entities and IDs |
| TF-IDF comparison | AUROC 0.666 | The same pairs and task |
| Gene-name detection | F1 83.35%; precision 79.02%; recall 88.17% | Article-level cross-validation on 415 official training papers |
| Chemical-name detection | F1 83.00%; precision 80.64%; recall 85.49% | Article-level cross-validation on 415 official training papers |
| Fully automatic relation extraction | **F1 46.10%**; precision 50.78%; recall 42.21% | Repeated development comparison on 100 papers and 154 gold relations |
| Clinical outcome spans: EBM-NLP | F1 54.38% | Outcome tokens in 200 development papers |
| Clinical outcome spans: EvidenceOutcomes | F1 46.24% | Outcome tokens in 200 development papers |

Relation presence evaluates whether a supplied pair has textual support. Automatic extraction evaluates entity detection, standard identifiers and relation types together: 65 true positives, 63 false positives and 89 false negatives. AUROC measures ranking; precision and recall measure the proportion of correct predictions and recovered gold records, respectively. F1 is the harmonic mean of precision and recall.

Detection and extraction rows summarize the completed configuration with the highest F1 for each task. They use different configurations. See the [results](docs/reviewer/RESULTS.md), [metrics CSV](docs/reviewer/metrics.csv) and [source manifest](docs/reviewer/summary.json) for the evaluation units and source files.

<a name="코드와-데이터-구성"></a>

## Repository structure

| Directory | Purpose |
|---|---|
| `nutrigenomics/` | Acquisition, processing, analysis, database and FastAPI |
| `literature_service/` | Literature collection, summaries, extraction and evaluation |
| `frontend/explorer/` | React interface for foods, studies and literature |
| `docs/` | Data dictionary, methods, results and operating guides |
| `tests/` | Calculation, query, provenance and service checks |

The literature system combines dictionaries, rules, biomedical encoders and task-specific classifiers. The default web summary engine is `heuristic-v1`; the representative automatic relation evaluation uses `chatgpt-repair-v5-r1`. Configuration details are in the [model records](docs/reviewer/VERSIONS.md).

This repository provides code, small test inputs, summary results and rebuild instructions. Full databases and models are stored in the research environment. Public service and full-data distribution follow the [publication procedure](docs/PUBLICATION_RUNBOOK_ko.md). Expert review of 263 core records and the final 30-query evaluation are tracked in the [review workflow](docs/WORKBENCH_SUBMISSION_REVIEW_ko.md).

<a name="설치와-실행"></a>

## Getting started

With Git, Python 3.12 and uv installed:

```sh
git clone https://github.com/kangk1204/nutriomics-db.git
cd nutriomics-db
uv sync --frozen --extra dev
uv run pytest tests/test_composition_policy.py tests/test_public_service_isolation.py -q
```

These checks use small test inputs. The web build uses Node.js 24 and pnpm 11.25.0. Follow [Getting started](docs/GETTING_STARTED.md) to prepare data and run the interface, and the [literature runbook](docs/LITERATURE_RUNBOOK_ko.md) to connect the literature database and models. Literature integration is included in the current `main` branch.

On a prepared local service:

```sh
curl --fail-with-body "http://127.0.0.1:8064/api/v1/explorer/literature/stats"
curl --fail-with-body "http://127.0.0.1:8064/api/v1/explorer/literature/papers?page=1&page_size=5"
```

| Topic | Documentation |
|---|---|
| Tables, identifiers and missing values | [Data dictionary](docs/PUBLIC_DATA_DICTIONARY.md) |
| Rebuilding from sources | [Rebuild guide](docs/WORKBENCH_PUBLIC_REBUILD_ko.md) |
| Updating data | [Update guide](docs/WORKBENCH_UPDATE_ko.md) |
| API examples and responses | [Literature API](docs/LITERATURE_API_ko.md) |
| Web integration checks | [Integration record](docs/MAIN_LITERATURE_INTEGRATION_ko.md) |
| Database structure figure | [PNG, SVG and PDF](docs/figures/db-structure/README.md) |

<a name="사용-조건과-인용"></a>

## License and citation

Software is released under the [MIT license](LICENSE). Eligible original curation and derived analyses use CC BY 4.0. Source datasets and article texts retain their respective terms; see [LICENSE-DATA.md](LICENSE-DATA.md).

Please cite the repository URL and the commit used in your work. Questions and corrections can be submitted through [GitHub Issues](https://github.com/kangk1204/nutriomics-db/issues).
