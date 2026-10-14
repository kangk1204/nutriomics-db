"""Index a rights-filtered data package for local UI inspection, never production.

The public data package intentionally omits internal aliases, study indexes and
effect references. Missing fields remain unknown. This creates only a separate
presentation index over its actual records; it does not reconstruct omitted data.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from nutrigenomics.explorer_build import SCHEMA, canonical, sha256


def prepare(source: Path, output: Path):
    source = source.resolve()
    before = sha256(source)
    output.mkdir(parents=True, exist_ok=False)
    incoming = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    incoming.row_factory = sqlite3.Row
    incoming.execute('PRAGMA query_only=ON')
    db = sqlite3.connect(output / 'explorer.sqlite')
    db.executescript(SCHEMA)
    original = {r['key']: json.loads(r['value_json']) for r in incoming.execute('SELECT * FROM meta')}
    assert isinstance(original.get('scope'), str) and 'rights-filtered' in original['scope']
    db.executemany('INSERT INTO node VALUES (?,?,?,?,?,?,?,?)', (
        (r['id'], r['kind'], r['label'], None, '[]', '{}', '[]', 0)
        for r in incoming.execute('SELECT * FROM node')
    ))
    db.executemany('INSERT INTO source VALUES (?,?)', (
        (r['id'], r['metadata_json']) for r in incoming.execute('SELECT * FROM source')
    ))
    db.executemany('INSERT INTO edge VALUES (?,?,?,?,?,?,?,?,?,?,?)', (
        (r['id'], r['subject'], r['object'], r['relation'], r['tier'], r['review_status'] or 'unknown',
         r['taxon'], r['context_json'] or '{}', r['source_ids_json'] or '[]', r['pmids_json'] or '[]', '[]')
        for r in incoming.execute('SELECT * FROM edge')
    ))
    db.executemany('INSERT INTO measurement VALUES (?,?,?,?,?,?,?,?,?)', (
        (r['id'], r['food'], r['component'], r['amount'], r['unit'], r['basis'], r['status'], r['source_id'], '{}')
        for r in incoming.execute('SELECT * FROM measurement')
    ))
    db.execute('UPDATE node SET degree=(SELECT count(*) FROM edge WHERE subject=node.id)+(SELECT count(*) FROM edge WHERE object=node.id)')
    counts = {t: db.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in ('node', 'edge', 'measurement', 'source')}
    assert counts == {t: incoming.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in counts}
    metadata = {
        'release_id': 'permitted-local-preview-' + datetime.now(UTC).strftime('%Y%m%d'),
        'parent_release_id': original.get('release_id'),
        'created_at': datetime.now(UTC).isoformat(),
        'scope': original['scope'], 'counts': counts, 'expert_review': original.get('expert_review', 'unknown'),
        'source_package_sha256': before,
        'gaps': ['허용된 조성·관계 부분집합입니다. 원본의 연구·오믹스 색인과 별칭·분야 주석은 포함되지 않습니다.'],
    }
    db.executemany('INSERT INTO meta VALUES (?,?)', ((k, canonical(v)) for k, v in metadata.items()))
    db.commit()
    assert db.execute('PRAGMA foreign_key_check').fetchone() is None
    assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    db.close()
    incoming.close()
    assert sha256(source) == before, 'source package modified'
    manifest = {**metadata, 'database_sha256': sha256(output / 'explorer.sqlite'),
                'production_activation': False,
                'normalization': 'Missing JSON arrays/objects remain empty, missing review status is unknown; no biological relationship added.'}
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = prepare(args.source, args.output)
    print(json.dumps({'release_id': result['release_id'], 'counts': result['counts'], 'production_activation': False}))
