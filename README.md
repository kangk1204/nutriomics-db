# NutriOmics

A research dataset and workbench for food composition and human dietary-
intervention transcriptomic and DNA methylation results. This repository
contains acquisition, curation, analysis, release, update and validation code.
It does not predict individual disease risk or health benefits.

The current release is a **Data Descriptor candidate**. Scientific review,
sealed-query evaluation, DOI registration and public hosting remain explicit
release gates. A source-checked record is not an expert-approved result.
The [source precheck and human-review workflow](docs/WORKBENCH_SUBMISSION_REVIEW_ko.md)
provides hash-bound decision proposals, source-only query review and a portable
submission supplement while preserving these gates.

## Install

Python 3.12, Node 24 and R/Bioconductor are used in separate project environments.

```bash
uv sync --frozen --extra dev
uv run pytest
cd frontend/explorer
corepack pnpm install --frozen-lockfile
corepack pnpm build
```

The frontend uses React, Cytoscape.js, Dagre and TanStack Table. R analysis code
and its environments are separate from the public read-only service.

## Data and releases

Use a versioned public release's `dataset-metadata.json` for actual public counts.
Food measurements, intervention curation, quantitative omics results and external
regulatory context have different count units and rights. Effect rows represent
features × contrasts × analysis versions, not independent participants.

`effect` records use the five-part key:
`series_accession, feature_type, feature_id, contrast, analysis_version`.
SQLite and Parquet preserve nulls, original feature types, effect and statistical
values. Source policy, file hashes, exclusions and human-review state accompany
each release. Third-party data not cleared for redistribution must be acquired
from the original source; the public package does not reconstruct the complete
internal source database.

Build candidates with `python -m scripts.build_publication_release`; prepare
the service derivative with `python -m scripts.prepare_public_service_release`.
Both create new directories. See [the runbook](docs/PUBLICATION_RUNBOOK_ko.md) for
review, validation, restore, DOI deposit and monthly update procedures.
For rebuilding on a new host from official public originals, see
[the fresh-source rebuild procedure](docs/WORKBENCH_PUBLIC_REBUILD_ko.md).
It includes versioned execution of the 14 analysis outcomes and separate
computational, public-value preservation and browser/performance gates.

## Public service

Set `NUTRIOMICS_PUBLIC_RELEASE` explicitly and run
`uvicorn nutrigenomics.public_service:app_factory --factory --host 127.0.0.1`.
Only public data is loaded; write/collection/reanalysis routes are absent.
Candidate review requires `NUTRIOMICS_ALLOW_CANDIDATE=1` and a loopback binding.
Provider-neutral container examples are in `deploy/public`.

## License

Own software: [MIT](LICENSE). Own curation and derived analyses: CC BY 4.0
when affirmatively cleared by the per-source/output policy. Upstream data and
dependencies retain their own terms; see [LICENSE-DATA.md](LICENSE-DATA.md).

The software is supplied for research. Dietary study conditions and observed
gene/CpG changes do not establish individual efficacy or causal health effects.
# Prediction and literature candidate extension

The optional update pipeline reacquires SCREEN GraphRegLR rows and a frozen PMID
corpus from PubMed. Computational predictions, automatic text candidates and
observed human intervention results use distinct layers and review states.
Source-span preservation is not expert semantic validation. Article-specific
redistribution restrictions remain enforced in the public dataset and exports.
See [candidate layer acquisition, interpretation and verification](docs/WORKBENCH_CANDIDATE_LAYERS_ko.md).
