"""Release-bound workbench page references; no scientific values are duplicated."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from .explorer_performance import QueryUnavailable, canonical, fingerprint, read_connection

_VERIFIED_INDEXES = {}
_IMPLEMENTATION_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def build(source: Path, output: Path, expected_sha256: str):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError(output)
    before = fingerprint(source)
    if before[-1] is not None:
        raise ValueError('Build from a closed immutable release without a WAL')
    with source.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != expected_sha256:
        raise ValueError('Source SHA-256 mismatch')
    memory = sqlite3.connect(':memory:')
    memory.execute('PRAGMA temp_store=MEMORY')
    memory.executescript('''
        CREATE TABLE header (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE raw_ref (id TEXT,layer TEXT,study_id TEXT,tissue TEXT,subject_id TEXT,object_id TEXT);
        CREATE TABLE page_ref (ordinal INTEGER PRIMARY KEY,layer TEXT NOT NULL,ordinal_layer INTEGER NOT NULL,id TEXT NOT NULL,subject_id TEXT,object_id TEXT);
        CREATE TABLE layer_count (layer TEXT PRIMARY KEY,total INTEGER NOT NULL);
    ''')
    counts = Counter()
    source_order = hashlib.sha256()
    source_rows = hashlib.sha256()
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as db:
        release_id = json.loads(db.execute("SELECT value_json FROM meta WHERE key='release_id'").fetchone()[0])
        def values():
            # Scan the native table sequentially. Reading source payload pages
            # in a million random identifier positions is expensive on HDDs.
            for row in db.execute('SELECT id,layer,study_id,tissue,subject_id,object_id FROM workbench_evidence NOT INDEXED'):
                source_rows.update(canonical(list(row)).encode())
                yield row
        memory.executemany('INSERT INTO raw_ref VALUES(?,?,?,?,?,?)', values())
    copied_rows = hashlib.sha256()
    for row in memory.execute('SELECT * FROM raw_ref ORDER BY rowid'):
        copied_rows.update(canonical(list(row)).encode())
    if copied_rows.digest() != source_rows.digest():
        raise ValueError('Source identifier/endpoints changed in projection')
    def ordered_values():
        for rank, (identifier, layer, subject, target) in enumerate(memory.execute('SELECT id,layer,subject_id,object_id FROM raw_ref ORDER BY layer,study_id,tissue,id'), 1):
            counts[layer] += 1
            source_order.update(canonical([rank, layer, counts[layer], identifier, subject, target]).encode())
            yield rank, layer, counts[layer], identifier, subject, target
    memory.executemany('INSERT INTO page_ref VALUES(?,?,?,?,?,?)', ordered_values())
    memory.executescript('CREATE UNIQUE INDEX page_layer ON page_ref(layer,ordinal_layer); CREATE UNIQUE INDEX page_id ON page_ref(id); CREATE INDEX page_subject ON page_ref(subject_id,ordinal,id,layer); CREATE INDEX page_object ON page_ref(object_id,ordinal,id,layer);')
    memory.executemany('INSERT INTO layer_count VALUES(?,?)', sorted(counts.items()))
    projected_order = hashlib.sha256()
    for row in memory.execute('SELECT ordinal,layer,ordinal_layer,id,subject_id,object_id FROM page_ref ORDER BY ordinal'):
        projected_order.update(canonical(list(row)).encode())
    if projected_order.digest() != source_order.digest():
        raise ValueError('Native and indexed record order disagree')
    metadata = {'schema': 2, 'release_id': release_id, 'source_fingerprint': before,
                'source_sha256': actual, 'total': sum(counts.values()),
                'sort': ['layer', 'study_id', 'tissue', 'id'], 'scientific_values_copied': 0,
                'builder_module_sha256': _IMPLEMENTATION_SHA256,
                'ordered_references_sha256': source_order.hexdigest(), 'all_reference_order_checked': True,
                'all_identifier_endpoints_checked': True, 'native_identifier_endpoints_sha256': source_rows.hexdigest()}
    memory.execute('DROP TABLE raw_ref')
    memory.execute('INSERT INTO header VALUES(?,?)', ('binding', canonical(metadata)))
    memory.commit()
    memory.execute('VACUUM')
    if memory.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or fingerprint(source) != before:
        raise ValueError('Source changed or page index integrity failed')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + '.building')
    if temporary.exists():
        raise FileExistsError(temporary)
    try:
        with closing(sqlite3.connect(temporary)) as target:
            memory.backup(target)
        if fingerprint(source) != before:
            raise ValueError('Source changed while materializing index')
        temporary.replace(output)
    finally:
        memory.close()
    return {**metadata, 'output': str(output), 'index_sha256': hashlib.sha256(output.read_bytes()).hexdigest()}


def verify_index(index_path, source_path, release_id, source_sha256):
    """Verify the auxiliary artifact before accepting requests, just like its parent."""
    path = Path(index_path).resolve()
    before = fingerprint(path)
    receipt = json.loads(path.with_suffix('.json').read_text())
    with path.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        binding = json.loads(db.execute("SELECT value FROM header WHERE key='binding'").fetchone()[0])
    if (before != fingerprint(path) or actual != receipt.get('index_sha256')
            or binding.get('release_id') != release_id or binding.get('source_sha256') != source_sha256
            or canonical(binding.get('source_fingerprint')) != canonical(fingerprint(source_path))):
        raise ValueError('Auxiliary evidence index checksum or source binding mismatch')
    _VERIFIED_INDEXES[str(path)] = before
    return {'path': str(path), 'sha256': actual, 'release_id': release_id, 'references': binding['total']}


def page_rows(source_db, source_path, release_id, layer, page, page_size, seed_id=None):
    """Return None if not configured; a stale/missing configured index is an error."""
    configured = os.environ.get('NUTRIGENOMICS_EXPLORER_EVIDENCE_INDEX')
    if not configured:
        return None
    path = Path(configured)
    try:
        current_index = fingerprint(path)
        verified_index = _VERIFIED_INDEXES.get(str(path.resolve()))
        if verified_index is None:
            receipt = json.loads(path.with_suffix('.json').read_text())
            verify_index(path, source_path, release_id, receipt['source_sha256'])
            verified_index = _VERIFIED_INDEXES[str(path.resolve())]
        if current_index != verified_index:
            raise QueryUnavailable('source_mismatch')
        with read_connection(path) as index:
            row = index.execute("SELECT value FROM header WHERE key='binding'").fetchone()
            binding = json.loads(row[0]) if row else {}
            if (binding.get('schema') not in (1, 2) or binding.get('release_id') != release_id
                    or canonical(binding.get('source_fingerprint')) != canonical(fingerprint(source_path))):
                raise QueryUnavailable('source_mismatch')
            offset = (page - 1) * page_size
            if seed_id is not None:
                if binding['schema'] < 2:
                    return None
                layer_clause = ' AND layer=?' if layer != 'all' else ''
                args = [seed_id, layer, seed_id, layer] if layer != 'all' else [seed_id, seed_id]
                references = ('SELECT ordinal,id FROM page_ref WHERE subject_id=?' + layer_clause +
                              ' UNION SELECT ordinal,id FROM page_ref WHERE object_id=?' + layer_clause)
                total = index.execute('SELECT count(*) FROM (' + references + ')', args).fetchone()[0]
                identifiers = [r[0] for r in index.execute('SELECT id FROM (' + references + ') ORDER BY ordinal LIMIT ? OFFSET ?', [*args, page_size, offset])]
            elif layer == 'all':
                total = binding['total']
                identifiers = [r[0] for r in index.execute('SELECT id FROM page_ref WHERE ordinal>? AND ordinal<=? ORDER BY ordinal', (offset, offset + page_size))]
            else:
                count = index.execute('SELECT total FROM layer_count WHERE layer=?', (layer,)).fetchone()
                total = count[0] if count else 0
                identifiers = [r[0] for r in index.execute('SELECT id FROM page_ref WHERE layer=? AND ordinal_layer>? AND ordinal_layer<=? ORDER BY ordinal_layer', (layer, offset, offset + page_size))]
        expected = max(0, min(page_size, total - offset))
        if len(identifiers) != expected:
            raise QueryUnavailable('source_mismatch')
        rows = {row['id']: row for row in source_db.execute('SELECT * FROM workbench_evidence WHERE id IN (' + ','.join('?' * len(identifiers)) + ')', identifiers)} if identifiers else {}
        if len(rows) != len(identifiers):
            raise QueryUnavailable('source_mismatch')
        return total, [rows[identifier] for identifier in identifiers]
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError) as exc:
        raise QueryUnavailable('source_mismatch') from exc
