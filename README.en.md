# NutriOmics

Food composition and results from human dietary intervention studies.

[한국어 안내](README.md) · [Review guide](docs/REVIEWER_GUIDE.md) · [Current status](docs/PROJECT_STATUS.md) · [Setup](docs/GETTING_STARTED.md)

NutriOmics lets researchers look up food composition and examine related dietary studies.
For each study, it keeps the tested food or supplement, dose, duration, tissue and comparison
alongside gene expression, DNA methylation and reported clinical results.

The repository contains the code for collecting data, running analyses, building the database
and displaying results in a browser. The data download, scientific review and public website
are still being prepared. See [current status](docs/PROJECT_STATUS.md) for the dated counts and remaining work.

## What you can examine

| Part | Contents |
|---|---|
| Food composition | Food names, preparation states, component amounts, units and original sources |
| Human studies | Intervention, dose, study groups, sampling times and comparisons |
| Analysis results | Gene expression, DNA methylation, pathway results and reported clinical measurements |
| Network view | Foods, components, genes and related records, with the source of each connection |
| Data preparation | Collection, analysis, update and validation scripts |

Experimental observations, computational predictions and automatically extracted literature
candidates have separate labels. A network connection does not by itself show that a food
improves health.

## An example

A researcher enters 150 g of yogurt. The program calculates component amounts from a selected
food record and can show a related study that tested 800 g of yogurt. It retains the study's
800 g dose and sampling times. It does not scale that study's gene expression changes to 150 g
or calculate a personal health score.

## Start here

- To assess the work, read the [review guide](docs/REVIEWER_GUIDE.md).
- To check the data size and review status, read [current status](docs/PROJECT_STATUS.md).
- To install the code and run a small check, follow [setup](docs/GETTING_STARTED.md).
- To understand the tables, read the [data dictionary](docs/PUBLIC_DATA_DICTIONARY.md).
- To rebuild or update the data, use the [rebuild instructions](docs/WORKBENCH_PUBLIC_REBUILD_ko.md) and [operating instructions](docs/PUBLICATION_RUNBOOK_ko.md).

## Code and data

Python 3.12 runs the API and data preparation code. React and Cytoscape.js provide the browser
interface. R and Bioconductor run the omics analyses in a separate environment.

The Git repository contains code and configuration, not the full database or model files.
A database version includes its table definitions, source information, file checksums and review
records. The [data status page](docs/PROJECT_STATUS.md) explains what is available and what still
requires review. Copying this repository alone does not start a populated website.

## License and citation

The project code uses the [MIT license](LICENSE). Data from other providers retain their own
terms. The conditions for our curation and analysis outputs are in [LICENSE-DATA.md](LICENSE-DATA.md).

The dataset and code do not yet have version DOIs. For now, identify the repository URL and the
exact commit used. A dataset citation will be provided when the data deposit is complete.

Questions and reproducible problems can be reported through [GitHub Issues](https://github.com/kangk1204/nutriomics-db/issues).
