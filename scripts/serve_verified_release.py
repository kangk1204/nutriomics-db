"""Verify a permanent immutable release before accepting loopback traffic."""
import argparse
import json
import os
import time
from pathlib import Path

from nutrigenomics.explorer_build import sha256
from nutrigenomics.source_updates import atomic_json


def verify(release, application, receipt):
    release = Path(release).resolve()
    manifest = json.loads((release / 'manifest.json').read_text())
    database = release / ('nutriomics.sqlite3' if application == 'public' else 'explorer.sqlite')
    started = time.monotonic()
    before = database.stat()
    actual = sha256(database)
    after = database.stat()
    if actual != manifest.get('database_sha256') or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Release database changed or its SHA-256 does not match the manifest')
    value = {'release_id': manifest['release_id'], 'database': str(database),
             'database_sha256': actual, 'bytes': after.st_size,
             'startup_verification_seconds': time.monotonic() - started,
             'cache_condition': 'Sequential checksum reads warm the OS file cache; caches are not dropped',
             'application': application}
    if os.environ.get('NUTRIGENOMICS_EXPLORER_EVIDENCE_INDEX'):
        from nutrigenomics.evidence_paging import verify_index
        value['evidence_query_index'] = verify_index(os.environ['NUTRIGENOMICS_EXPLORER_EVIDENCE_INDEX'], database, manifest['release_id'], actual)
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
    print(json.dumps(result), flush=True)
    import uvicorn
    uvicorn.run('nutrigenomics.public_service:app_factory' if a.application == 'public' else 'nutrigenomics.api:app',
                factory=a.application == 'public', host='127.0.0.1', port=a.port)
