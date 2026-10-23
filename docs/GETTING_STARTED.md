# Setup

[Overview](../README.en.md) · [Review guide](REVIEWER_GUIDE.md) · [Data availability](PROJECT_STATUS.md)

## Inspect the code without the full database

Install Git, Python 3.12 and [uv](https://docs.astral.sh/uv/getting-started/installation/).
Then run these commands in a terminal:

```sh
git clone https://github.com/kangk1204/nutriomics-db.git
cd nutriomics-db
uv sync --frozen --extra dev
uv run pytest tests/test_composition_policy.py tests/test_public_service_isolation.py -q
```

These checks use small test data. They test composition rules and the read-only service;
they do not download the full food database or rerun the omics analyses.
Use `uv run pytest -q` to run the complete Python test suite available in the checked-out code.

## Build the browser interface

Install Node.js 24 and the pnpm version specified in `frontend/explorer/package.json`.
The project uses React for the interface and Cytoscape.js for the network.

```sh
cd frontend/explorer
pnpm install --frozen-lockfile
pnpm build
cd ../..
```

The build writes the interface files to `frontend/explorer/dist`.
Building the interface does not provide a populated database.

## Open a prepared database locally

This step requires a separately prepared data directory containing `dataset-metadata.json`,
`manifest.json` and `nutriomics.sqlite3`, plus the files listed in that manifest.
The complete downloadable data archive is not yet published.

For a local review copy, set the following values and run the server from the repository root.
Replace the example data path with the directory on your computer.

Linux or macOS:

```sh
export NUTRIOMICS_PUBLIC_RELEASE="/path/to/prepared-release"
export NUTRIOMICS_ALLOW_CANDIDATE=1
uv run python -m uvicorn nutrigenomics.public_service:app_factory --factory --host 127.0.0.1 --port 8000
```

Windows PowerShell:

```powershell
$env:NUTRIOMICS_PUBLIC_RELEASE='C:\data\prepared-release'
$env:NUTRIOMICS_ALLOW_CANDIDATE='1'
uv run python -m uvicorn nutrigenomics.public_service:app_factory --factory --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/explorer/> for the interface or
<http://127.0.0.1:8000/docs> for the API documentation.
The review setting allows an unfinished data version on your own computer.
It does not mark that data as scientifically approved or ready for public deployment.

## Rebuild, analyze or update data

| Task | Instructions |
|---|---|
| Obtain source data and rerun analyses | [Rebuild instructions](WORKBENCH_PUBLIC_REBUILD_ko.md) |
| Read table and result definitions | [Data dictionary](PUBLIC_DATA_DICTIONARY.md) |
| Build a new data version and apply review decisions | [Operating instructions](PUBLICATION_RUNBOOK_ko.md) |
| Check source changes | [Update instructions](WORKBENCH_UPDATE_ko.md) |
| Inspect prediction and literature candidates | [Candidate data instructions](WORKBENCH_CANDIDATE_LAYERS_ko.md) |

The R scripts require their recorded R/Bioconductor environment and original inputs.
Installing the Python dependencies does not install R or reproduce the study analyses.
