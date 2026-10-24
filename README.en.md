# NutriOmics

A research database linking food composition with results from human dietary intervention studies.

[한국어](README.md) · [Database structure](docs/DB_STRUCTURE.md) · [Review guide](docs/REVIEWER_GUIDE.md) · [Setup](docs/GETTING_STARTED.md)

Researchers can calculate component intake from a selected food record and inspect related
human studies. Each study keeps the tested food or supplement, dose, duration, tissue,
sampling time and comparison alongside expression, DNA methylation and pathway results.

![NutriOmics database structure; Korean labels with an English explanation below](docs/figures/db-structure/보고서용_DB_구조도.png)

The main sequence is food and amount, composition, human intervention context, then observed
molecular results. Clinical summaries are linked by study. Mechanistic and computational
relationships remain separate. The arrows describe data joins, not dietary causality or complete coverage.

## Example

For 150 g of yogurt, the program calculates component amounts from the selected food record.
A related study may have tested 800 g. The original dose and measurement times remain visible;
the program does not scale its molecular effects to 150 g or predict an individual's health response.
Users can also search by component, gene name or identifier and inspect the corresponding study comparisons.
Missing evidence and unknown conditions remain explicit.

## Start here

| Purpose | Guide |
|---|---|
| Understand the records and their connections | [Database structure](docs/DB_STRUCTURE.md) |
| Assess scope, counts and unfinished review | [Review guide](docs/REVIEWER_GUIDE.md) · [Status](docs/PROJECT_STATUS.md) |
| Check the code without the full database | [Setup and small tests](docs/GETTING_STARTED.md) |
| Read table and result definitions | [Data dictionary](docs/PUBLIC_DATA_DICTIONARY.md) |
| Obtain sources and rebuild or update data | [Rebuild](docs/WORKBENCH_PUBLIC_REBUILD_ko.md) · [Update](docs/WORKBENCH_UPDATE_ko.md) |
| Inspect the current code changes | [24 October update](docs/UPDATES_20261024.md) |

## Prepared data

Scope checked on 24 October 2026:

| Item | Count |
|---|---:|
| Food records | 5,794 |
| Composition records | 368,208 |
| Registered study groups | 9, including 8 with reanalysis results |
| Contrasts | 53 |
| Existing analysis runs | 14 |
| Selected quantitative rows | 6,254,477 |

Rows are measured features by contrast and analysis version, not participants or established
health effects. The additional 50 Korean food records and 1,200 composition records in the local
research version are excluded. The GSE56960 method update replaces 50,550 selected pathway rows
without increasing the selected result total. See the [dated count record](docs/reference-data-summary.json).

## Availability and review

This repository includes collection, analysis, database, update and browser-interface code with
small test fixtures. The complete data deposit, version DOIs and external HTTPS service are not
yet available. Human decisions on 263 core records and two new pathway output versions,
source/output access conditions and the final 30-query evaluation remain pending.

Literature and model integration is under development in a [separate branch](https://github.com/kangk1204/nutriomics-db/tree/feature/20261023-literature-integration) and is excluded
from this default version. Passing code tests does not establish scientific acceptance or submission readiness.

To check the composition rules and read-only service using small test data:

```sh
git clone https://github.com/kangk1204/nutriomics-db.git
cd nutriomics-db
uv sync --frozen --extra dev
uv run pytest tests/test_composition_policy.py tests/test_public_service_isolation.py -q
```

[Setup](docs/GETTING_STARTED.md) covers the browser build and opening a separately prepared database.

## License and citation

Code uses [MIT](LICENSE). Third-party data retain their source conditions; curation and analysis
output conditions are described in [LICENSE-DATA.md](LICENSE-DATA.md).
Until version DOIs are registered, cite the repository URL and the exact commit used.
Questions and reproducible problems can be reported through [GitHub Issues](https://github.com/kangk1204/nutriomics-db/issues).
