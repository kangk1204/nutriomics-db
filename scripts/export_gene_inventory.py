"""Export an exact gene inventory from a verified immutable Explorer release."""
import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from nutrigenomics.gene_names import canonical, digest


def export(release, output):
    release, output = Path(release), Path(output)
    if output.exists():
        raise FileExistsError('Inventory snapshots are immutable')
    manifest = json.loads((release / 'manifest.json').read_text(encoding='utf-8'))
    database = release / ('nutriomics.sqlite3' if (release / 'nutriomics.sqlite3').exists() else 'explorer.sqlite')
    before = database.stat()
    wal = Path(str(database) + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise ValueError('Use a closed immutable snapshot, not a live WAL database')
    actual = digest(database)
    if actual != manifest['database_sha256']:
        raise ValueError('Release database checksum mismatch')
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        release_id = json.loads(db.execute("SELECT value_json FROM meta WHERE key='release_id'").fetchone()[0])
        if release_id != manifest['release_id']:
            raise ValueError('Manifest and database release IDs differ')
        items = []
        for identifier, label, value in db.execute("SELECT id,label,metadata_json FROM node WHERE kind='gene' ORDER BY id"):
            metadata = json.loads(value)
            items.append(dict(id=identifier, label=label, metadata=metadata,
                              metadata_sha256=hashlib.sha256(canonical(metadata).encode()).hexdigest()))
    after = database.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Database changed while creating the inventory')
    value = dict(release_id=release_id, database_sha256=actual, items=items)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        stream.write(canonical(value))
    return dict(release_id=release_id, database_sha256=actual, records=len(items), inventory_sha256=digest(output))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.release, args.output)))
