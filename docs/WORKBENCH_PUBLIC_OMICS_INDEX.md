# Public omics page references

The public SQLite release remains immutable. An optional derivative accelerates
deep omics pages by storing native identifiers, source row references and ordered
positions. It contains no effect estimates, uncertainty, P/FDR values, participant
counts or inferred permissions. Each response reads those values through the
original `study_effect` view, including its original metadata and rights projection.

Build with the same Python environment as the application:

```sh
python -m scripts.build_public_omics_index \
  --source /data/release/nutriomics.sqlite3 \
  --output /data/derived/public-omics-v1.sqlite3 \
  --source-sha256 <database_sha256_from_release_manifest>
```

Use a new output path for each derivative. Existing final artifacts and source
databases are never overwritten. The `.building` database and `.building.json`
checkpoint are incomplete operator artifacts. After interruption, repeat the same
command with `--resume`. Source path, file fingerprint, checksum, release, view
definition and builder bytes must still agree. The native source is read once in
physical row order; completed batches are retained when resuming. Sorting and
page materialization use disk-backed SQLite. Full checksums before and after
construction are separate sequential reads. These reads can warm operating-system
caches and are not HTTP performance evidence.

The builder uses one thread, 192 MiB of SQLite cache and bounded Python batches.
It checks disk and available memory before work, samples resident memory against a
2 GiB limit and records checksums, selector totals, ordering proofs and resource
measurements. Native TEXT effect identifiers are preserved. Multiple records with
the same five-part scientific key are retained in original `feature_id,id` order;
five-part uniqueness is not assumed.

For the public verified-release startup process only:

```sh
export NUTRIGENOMICS_PUBLIC_OMICS_INDEX=/data/derived/public-omics-v1.sqlite3
python -m scripts.serve_verified_release \
  --release /data/release --application public \
  --receipt /data/receipts/public-startup.json --port 8000
```

Startup verifies the derivative checksum, source checksum binding and release
before accepting requests. A configured but unverified, missing, changed or
mismatched derivative produces an explicit unavailable result, rather than
falling back to a slow query or reporting zero results. A different source path,
replacement database or nonempty WAL requires a newly built derivative. Source
row references are internal and never appear in API results or exports. Existing
`page` and source-bound `cursor` inputs retain their original meaning.

Keep startup preparation time, first request time, noncached query time and warm
HTTP distributions separate in evaluation. This index preserves numerical values
and permissions; it does not constitute scientific review or validate health
interpretation.
