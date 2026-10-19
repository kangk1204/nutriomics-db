"""Verify a permanent immutable release before accepting loopback traffic."""
import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from nutrigenomics.explorer_build import sha256
from nutrigenomics.explorer_context import configured_path
from nutrigenomics.explorer_performance import fingerprint
from nutrigenomics.settings import DATA_DIR
from nutrigenomics.source_replicas import guard as replica_guard
from nutrigenomics.source_replicas import initialize as initialize_replicas
from nutrigenomics.source_replicas import physical_read_path
from nutrigenomics.source_updates import atomic_json


def _memory_available():
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return None


def _closed_source(path):
    wal = Path(str(path) + '-wal')
    value = fingerprint(path)
    if value[1] is None or (wal.exists() and wal.stat().st_size > 0):
        raise ValueError('Internal quantitative source is absent or has a nonempty WAL')
    return value


def _core_sha256(path, deadline):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError('Internal quantitative-source checksum exceeded startup budget; readiness denied')
            block = stream.read(4 * 1024**2)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def verify_internal_core(manifest, *, seconds=600):
    """Hash the actual native source before readiness, independently of the graph.

    This is integrity verification plus disclosed OS file-cache warming. It is
    not response caching, a matrix copy, or evidence of cold-query performance.
    """
    expected = manifest.get('input_hashes', {}).get('core_db')
    declared = manifest.get('source_paths', {}).get('core_db')
    if expected is None and declared is None:
        protected = manifest.get('quantitative_preservation', {}).get('protected_table_counts_after', {})
        if protected.get('perf_effect_ref', 0) > 0:
            raise ValueError('Internal quantitative references require a manifest-bound SHA-256')
        return {'availability': 'not_declared_in_manifest', 'verified': False,
                'scope': 'No quantitative-source identity declared; no source-integrity claim'}
    if not isinstance(expected, str) or len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
        raise ValueError('Internal quantitative source requires a manifest-bound SHA-256')
    if not 0 < seconds <= 600:
        raise ValueError('Invalid quantitative-source verification time bound')
    path = configured_path('NUTRIGENOMICS_EXPLORER_CORE_DATABASE', Path(declared) if declared else DATA_DIR / 'db/nutrigenomics.sqlite3').resolve()
    before = _closed_source(path)
    started = time.monotonic()
    memory_before = _memory_available()
    physical = physical_read_path(path).resolve()
    actual = _core_sha256(physical, started + seconds)
    elapsed = time.monotonic() - started
    after = _closed_source(path)
    if elapsed > seconds:
        raise TimeoutError('Internal quantitative-source checksum exceeded startup budget; readiness denied')
    replica_guard()
    if actual != expected or after != before:
        raise ValueError('Internal quantitative-source SHA-256/fingerprint mismatch')
    return {'availability': 'verified', 'verified': True, 'source': str(path),
            'source_sha256': actual, 'physical_read_path': str(physical), 'expected_sha256_from': 'manifest.input_hashes.core_db',
            'source_fingerprint_before': before, 'source_fingerprint_after': after,
            'bytes': path.stat().st_size, 'checksum_seconds': elapsed,
            'startup_budget_seconds': seconds, 'nonempty_wal_rejected': True,
            'budget_scope': 'Checked between 4MiB reads; kernel disk waits can delay cancellation; readiness is denied after expiry',
            'memory_available_bytes_before': memory_before,
            'memory_available_bytes_after': _memory_available(),
            'cache_condition': ('Hash-identical whole-file tmpfs read replica; original identity guarded; no cold-source claim'
                                if physical != path else 'Full sequential checksum reads warm OS source pages; no cache flush or matrix copy'),
            'http_slo_evidence': False, 'cold_query_performance_validated': False}


def verify(release, application, receipt):
    release = Path(release).resolve()
    manifest = json.loads((release / 'manifest.json').read_text())
    database = release / ('nutriomics.sqlite3' if application == 'public' else 'explorer.sqlite')
    replicas = initialize_replicas(application, manifest['release_id'])
    started = time.monotonic()
    before = database.stat()
    actual = sha256(physical_read_path(database))
    after = database.stat()
    replica_guard()
    if actual != manifest.get('database_sha256') or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Release database changed or its SHA-256 does not match the manifest')
    value = {'release_id': manifest['release_id'], 'database': str(database),
             'database_sha256': actual, 'bytes': after.st_size,
             'startup_verification_seconds': time.monotonic() - started,
             'cache_condition': ('Verified whole-file tmpfs replicas; original-source guards remain active; startup costs are separate'
                                 if replicas['enabled'] else 'Sequential checksum reads warm the OS file cache; caches are not dropped'),
             'application': application, 'source_replicas': replicas}
    if application == 'internal':
        value['quantitative_source_verification'] = verify_internal_core(manifest)
    if os.environ.get('NUTRIGENOMICS_EXPLORER_EVIDENCE_INDEX'):
        from nutrigenomics.evidence_paging import verify_index
        value['evidence_query_index'] = verify_index(os.environ['NUTRIGENOMICS_EXPLORER_EVIDENCE_INDEX'], database, manifest['release_id'], actual)
    if application == 'public' and os.environ.get('NUTRIGENOMICS_PUBLIC_OMICS_INDEX'):
        from nutrigenomics.public_omics_index import verify_index
        value['omics_query_index'] = verify_index(os.environ['NUTRIGENOMICS_PUBLIC_OMICS_INDEX'], database, manifest['release_id'], actual)
    atomic_json(Path(receipt), value)
    return value


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--release', type=Path, required=True)
    p.add_argument('--application', choices=('internal', 'public'), required=True)
    p.add_argument('--receipt', type=Path, required=True)
    p.add_argument('--port', type=int, required=True)
    a = p.parse_args()
    result = verify(a.release, a.application, a.receipt)
    if a.application == 'public':
        os.environ['NUTRIOMICS_PUBLIC_RELEASE'] = str(a.release.resolve())
    else:
        os.environ['NUTRIGENOMICS_EXPLORER_DATABASE'] = result['database']
    from nutrigenomics.preview_startup import prepare_effect_preview_cache
    previews = prepare_effect_preview_cache(a.release, a.application,
        Path(a.receipt).with_name(Path(a.receipt).stem + '-previews.json'))
    result['effect_preview_preparation'] = previews
    atomic_json(Path(a.receipt), result)
    print(json.dumps(result), flush=True)
    import uvicorn
    uvicorn.run('nutrigenomics.public_service:app_factory' if a.application == 'public' else 'nutrigenomics.api:app',
                factory=a.application == 'public', host='127.0.0.1', port=a.port)
