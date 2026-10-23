# Review guide

[한국어 안내](REVIEWER_GUIDE.ko.md) · [Project overview](../README.en.md) · [Current status](PROJECT_STATUS.md)

## Purpose

NutriOmics organizes food composition and human dietary study results so that a researcher can
inspect the conditions behind a reported result. The main contribution is the connection between
composition records, study design and quantitative omics results, with their original sources.

The intended use is research and evidence lookup. The program does not provide a validated
prediction of a person's gene expression, DNA methylation or disease risk.

## Suggested reading order

1. Read the [overview](../README.en.md) for the purpose and a simple example.
2. Check [current status](PROJECT_STATUS.md) for the dated data counts and unfinished work.
3. Read the [data dictionary](PUBLIC_DATA_DICTIONARY.md) for the meaning of each record.
4. Follow [setup](GETTING_STARTED.md) to inspect the code and run checks without downloading large data files.
5. Consult the [rebuild instructions](WORKBENCH_PUBLIC_REBUILD_ko.md) for collection and analysis commands.

## What to check in the data

| Question | What the data should retain |
|---|---|
| Which food record was used? | Original food code, preparation state, component amount and measurement basis |
| What did the study test? | Food, supplement or diet; assigned dose, duration and study groups |
| What does a result compare? | Tissue, time points, comparison, participants included and analysis version |
| What was measured? | Original feature identifier, effect, unit, reported uncertainty and statistical values |
| Where does the statement come from? | Source article or dataset, version, location and review status |

Reported group averages, changes within one group and differences between groups are different
results. A non-significant result is retained; an unreported result remains missing. A gene
annotation near a methylation probe is separate from evidence that methylation regulates that gene.

## How to interpret the network

Food composition, human intervention observations, regulatory predictions and automatic literature
candidates are separate record types. The network helps locate related records. Following several
connections does not demonstrate a causal path from a food to a clinical outcome.

GraphRegLR links are computational predictions between regulatory regions and genes.
Their number is not a count of tested dietary effects. Automatically extracted literature
candidates remain candidates until a person checks the source and interpretation.

## What has and has not been checked

The repository includes checks for food calculations, preservation of analysis values, source
references, API behavior and release handling. Automated checks test the implemented behavior.
They do not establish that every scientific interpretation is correct.

The data summary in this repository describes the 22 October 2026 review version.
Its 263 core records still require recorded human decisions. Final query evaluation, data deposit
and external website publication are also unfinished. [Current status](PROJECT_STATUS.md) lists these separately.
The ongoing DIRECT PLUS analysis and literature integration are not included in that version's counts.

## Availability and reuse

The code and configuration are available here. The full prepared data files, final scientific
decisions and dataset DOI are not yet available for download. Rebuilding also requires original
source files and compliance with each provider's terms.

Code inspection, small automated checks and frontend compilation can be performed from the
repository. Reproducing every omics analysis requires the relevant input files and R environment.
We provide [setup instructions](GETTING_STARTED.md), a [data dictionary](PUBLIC_DATA_DICTIONARY.md)
and [operating instructions](PUBLICATION_RUNBOOK_ko.md) for those steps.
