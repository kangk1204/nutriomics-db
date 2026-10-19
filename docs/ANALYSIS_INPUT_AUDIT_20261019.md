# Read-only analysis input and sample-scope audit

The portable audit is `scripts/audit_analysis_inputs.py`. It reads analysis-run
science records from an immutable Explorer SQLite or an exported science JSON.
It never modifies SQLite, source files, analysis metadata, R objects, or effects.
No analysis is rerun and no expert approval is assigned.

Example against the healthy server's approved permanent project directory:

```bash
python scripts/audit_analysis_inputs.py \
  --database /path/to/new-derived-release/explorer.sqlite \
  --search-root <project-root>/rebuild/20261017 \
  --search-root <project-root>/toolchain \
  --expected-analysis-count 14 \
  --output /path/to/audit/analysis-input-audit.json
```

Use narrower raw/output/producer directories if available. Search is bounded to
explicit roots, skips dependency/cache directories, and does not follow directory
symlinks. The default hashing budget is64GiB; `--max-hash-gib` changes it. Each
unchanged physical file is hashed once in the process even if several analysis
versions reference it. No system configuration or shared cache is changed.

For a public metadata export whose operational paths have been withheld, the
resolver searches exact filenames and accepts a file only if its full SHA256
matches. A same-name different-version file is not silently substituted. If an
explicit absolute declared path exists inside a search root, a mismatch there
cannot be rescued by a different archived copy. Missing expected content,
missing hashes, budget limits, and bound-path mismatches are distinct states.

The report checks producer, helper, environment lock, overlay lock, every
declared input and every declared output. Analysis metadata used as a parent
input is linked by its verified bytes and original version. It does not claim a
postprocessing producer independently refitted its inherited parent model.

Only hash-verified sample/QC/person TSVs are parsed. Checks recompute row counts,
included assays, participant IDs, sample IDs, condition/visit duplicates,
per-person assay counts, excluded participants and paired exclusion consistency.
Person-level tables are not counted as assay tables. GSE54325 channel labels and
source-time labels are compared to the paired Cy3-baseline/Cy5-eight-week
orientation. Unavailable tables leave mapping unverified.

The method audit reports specific unresolved interpretation issues:

- GSE127530v1 and GSE219217v2 declare only within-contrast FDR. Their existing
  results must not be described as passing a study-wide primary-contrast family.
  Historical GSE56960v1/GSE107205v1 have the same scope limitation and remain
  separate from later between-condition analyses.
- GSE54643 beta-scale SE is the unmoderated sample SD of paired beta deltas
  divided by the square root of their number. P/FDR use the moderated M-value
  model. No same-beta-model confidence interval is reconstructed.
- Fixed1kb regions are prespecified tile screens, not discovered segmented DMRs.
- FDR postprocessors inherit fitted models; estimated blood-composition
  adjustment is a sensitivity association and not a cell-intrinsic causal effect.
- Declared design-rank or residual-df contradictions fail the machine audit.
  RDS model matrices, contrast algebra, normalization, and statistical assumptions
  are not independently recomputed by this Python-only audit.

`failed` means a machine-detectable contradiction or expected-count failure.
`partial_unavailable_evidence` means some required bytes/mapping remain
unavailable. `artifact_verified_with_scientific_limits` means the declared
artifact checks and available sample counts passed, with the scientific limits
still listed. All outputs keep `scientific_approval=false`,
`expert_review_status=pending`, and `journal_submission_ready=false`.

The CLI returns nonzero for a hard failure. An exit code0 only means that the
audit ran without a hard contradiction: callers must inspect the JSON status
and per-analysis findings before declaring verification complete.

The 2026-10-19 local run verified14/14 producer hashes and71 artifact references;
the raw input/output matrices are on the server, so all14 local analyses were
explicitly partial. These are reference-instance counts, not unique files or
participants. The server run is required to check the missing source artifacts
and sample tables. This audit does not replace professor scientific review.
