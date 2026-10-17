"""Create an immutable public derivative with restricted dictionary payloads removed.

Quantitative tables are byte-copied before mutation and protected by a SQLite
authorizer. This operation changes citation/node metadata, never omics values.
"""
import argparse
import json
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from nutrigenomics.explorer_build import canonical, decode, read_only, sha256, verify_closed_sqlite
from nutrigenomics.publication import _csv, _write_review, reference_node_metadata, source_reference
from nutrigenomics.source_updates import atomic_json


def metadata_only_authorizer(action, first, second, database, trigger):
    if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
        if first not in {'node', 'source', 'meta'}:
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def sanitize(parent, source, output, release_id):
    parent, source, output = map(lambda p: Path(p).resolve(), (parent, source, output))
    manifest = json.loads((parent / 'manifest.json').read_text())
    original_hash = sha256(parent / 'nutriomics.sqlite3')
    if original_hash != manifest['database_sha256']:
        raise ValueError('Public parent changed')
    staging = output.with_name(output.name + '.building')
    if output.exists() or staging.exists():
        raise FileExistsError('Use a new immutable derivative directory')
    staging.mkdir(parents=True)
    print(json.dumps({'phase': 'copy_public_parent', 'release': release_id}), flush=True)
    for p in parent.iterdir():
        if p.name == 'manifest.json':
            continue
        if p.is_dir():
            shutil.copytree(p, staging / p.name)
        else:
            shutil.copyfile(p, staging / p.name)
    database = staging / 'nutriomics.sqlite3'
    if sha256(database) != original_hash:
        raise ValueError('Public database copy is not byte-identical before metadata edits')
    changes = []
    with closing(sqlite3.connect(database)) as db, closing(read_only(source)) as upstream:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA secure_delete=ON')
        db.execute('PRAGMA cache_size=-262144')
        db.set_authorizer(metadata_only_authorizer)
        references = {}
        for row in db.execute('SELECT * FROM source').fetchall():
            full = upstream.execute('SELECT * FROM source WHERE id=?', (row['id'],)).fetchone()
            if full is None:
                raise ValueError('Citation source absent from original release')
            new, old = source_reference(upstream, full), decode(row['metadata_json'])
            if new['export_allowed'] != old['export_allowed'] or new['sha256'] != old['sha256']:
                raise ValueError('Metadata repair may not widen permissions or change source identity')
            references[row['id']] = new
            db.execute('UPDATE source SET metadata_json=? WHERE id=?', (canonical(new), row['id']))
        for row in db.execute('SELECT id,metadata_json FROM node').fetchall():
            original = decode(row['metadata_json'])
            safe = reference_node_metadata(original, references)
            if safe != original:
                db.execute('UPDATE node SET metadata_json=? WHERE id=?', (canonical(safe), row['id']))
                changes.append({'node_id': row['id'], 'source_id': original.get('source_id'),
                                'removed_fields': ['dictionary']})
        metadata = json.loads((staging / 'dataset-metadata.json').read_text())
        metadata.update(release_id=release_id, dataset_release_id=release_id, service_build_id=release_id,
            created_at=datetime.now(UTC).isoformat(),
            public_service_parent=manifest['release_id'], public_service_parent_database_sha256=original_hash,
            public_reference_policy='restricted-source-dictionaries-withheld-v1')
        metadata['graph_performance']['indexed_for_release'] = release_id
        report = {'status': 'passed', 'parent_release': manifest['release_id'],
            'parent_database_sha256': original_hash, 'release_id': release_id, 'redacted_nodes': changes,
            'source_references': len(references), 'permissions_widened': 0,
            'effect_mutation_policy': 'SQLite authorizer denies all DML outside node/source/meta',
            'original_copy_hash_verified': True, 'secure_delete': True}
        metadata['reference_payload_audit'] = report
        for key, value in metadata.items():
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, canonical(value)))
        db.commit()
        remaining = [row['id'] for row in db.execute('SELECT id,metadata_json FROM node')
                     if reference_node_metadata(decode(row['metadata_json']), references) != decode(row['metadata_json'])]
        if remaining:
            raise ValueError('Restricted dictionaries remain queryable')
        atomic_json(staging / 'source-catalog.json', list(references.values()))
        atomic_json(staging / 'dataset-metadata.json', metadata)
        atomic_json(staging / 'reference-payload-audit.json', report)
        for table in ('node', 'source', 'meta'):
            columns = [r[1] for r in db.execute('PRAGMA table_info(' + table + ')')]
            _csv(staging / 'tables' / (table + '.csv.gz'), columns, db.execute('SELECT * FROM ' + table))
        _write_review(staging, [dict(r) for r in db.execute('SELECT * FROM science_record ORDER BY kind,id')],
                      list(references.values()), json.loads((staging / 'review/analysis-audit.json').read_text()))
        quality_path = staging / 'quality-report.json'
        if quality_path.exists():
            quality = json.loads(quality_path.read_text())
            quality.update(release_id=release_id, reference_payload_audit=report)
            atomic_json(quality_path, quality)
    print(json.dumps({'phase': 'verify_closed_sanitized_derivative', 'redacted_nodes': len(changes)}), flush=True)
    verify_closed_sqlite(database)
    files = [{'path': p.relative_to(staging).as_posix(), 'bytes': p.stat().st_size,
              'sha256': sha256(p)} for p in sorted(staging.rglob('*')) if p.is_file()]
    atomic_json(staging / 'manifest.json', {**manifest, 'release_id': release_id,
        'database_sha256': sha256(database), 'files': files,
        'derived_from': {'release_id': manifest['release_id'], 'database_sha256': original_hash},
        'reference_payload_audit': report})
    staging.rename(output)
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--parent', type=Path, required=True)
    p.add_argument('--source', type=Path, required=True, help='Original scientific projection; used only for citation links and versions')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--release-id', required=True)
    a = p.parse_args()
    print(json.dumps(sanitize(a.parent, a.source, a.output, a.release_id)))
