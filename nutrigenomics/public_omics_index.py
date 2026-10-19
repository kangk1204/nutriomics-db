"""Immutable public omics page references, never copies effect/statistic values.

Build: one rowid-ordered source stream, disk-backed reference sorting, restartable
checkpoints. Runtime: bounded ordinal ranges followed by native rowid hydration.
The public study_effect view projection (including permissions) is preserved.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

from .explorer_performance import QueryUnavailable, canonical, fingerprint, read_connection
from .source_replicas import physical_read_path
from .source_updates import atomic_json

SCHEMA = 1
ENV = 'NUTRIGENOMICS_PUBLIC_OMICS_INDEX'
KEYS = ('series_accession', 'feature_type', 'contrast', 'analysis_version')
_VERIFIED = {}


def file_sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _closed_fingerprint(path):
    value = fingerprint(path)
    wal = Path(str(Path(path).resolve()) + '-wal')
    if value[1] is None or value[-1] is not None or (wal.exists() and wal.stat().st_size):
        raise ValueError('A closed immutable database without a nonempty WAL is required')
    return value


def _projection(db):
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name='study_effect'").fetchone()
    sql = row[0] if row else ''
    match = re.fullmatch(r'CREATE\s+VIEW\s+study_effect\s+AS\s+SELECT\s+(.+)\s+FROM\s+effect\s*;?', sql, re.IGNORECASE | re.DOTALL)
    if not match:
        raise ValueError('Unsupported public study_effect view; cannot preserve its projection')
    return match[1], hashlib.sha256(sql.encode()).hexdigest()


def _typed_reference(digest, row):
    # Versioned binary tuple encoding: field count + type + length/value.
    digest.update(struct.pack('>I', len(row)))
    for value in row:
        if value is None:
            digest.update(b'N')
        elif type(value) is int:
            digest.update(b'I' + struct.pack('>q', value))
        elif type(value) is str:
            raw = value.encode('utf-8')
            digest.update(b'S' + struct.pack('>Q', len(raw)) + raw)
        else:
            raise ValueError('Unexpected native reference type')


def _rss_bytes():
    # Linux /proc reports current resident memory, unlike platform-specific ru_maxrss.
    try:
        return int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError, AttributeError):
        return None


def _available_memory():
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return None


def build(source, output, expected_sha256, *, resume=False, batch_size=10000, stop_after_batches=None):
    """Create a new derivative; explicit resume only touches its own .building file.

    Source reading is resumable at committed native rowids. The sorting index is
    atomic; page materialization resumes at its last ordered reference. No matrix
    is held in memory; SQLite caches total 192 MiB and temp sorting uses files.
    stop_after_batches is an operator/testing checkpoint, not a partial release.
    """
    started = time.monotonic()
    source, output = Path(source).resolve(), Path(output).resolve()
    temporary = output.with_name(output.name + '.building')
    if output.exists() or output.with_suffix('.json').exists():
        raise FileExistsError(output)
    if not 1 <= batch_size <= 50000:
        raise ValueError('batch_size must be between 1 and 50000')
    output.parent.mkdir(parents=True, exist_ok=True)
    if temporary.exists() != bool(resume):
        raise ValueError('Existing .building requires explicit resume; resume requires a checkpoint')
    before = _closed_fingerprint(source)
    resources = {'disk_free_bytes': shutil.disk_usage(output.parent).free,
                 'rss_bytes_before': _rss_bytes(), 'memory_budget_bytes': 2 * 1024**3,
                 'memory_available_bytes_before': _available_memory(),
                 'sqlite_cache_bytes': 192 * 1024**2, 'threads': 1}
    if resources['disk_free_bytes'] < max(256 * 1024**2, source.stat().st_size // 2):
        raise ValueError('Insufficient free disk for reference sorting and atomic finalization')
    if resources['rss_bytes_before'] and resources['rss_bytes_before'] > resources['memory_budget_bytes']:
        raise ValueError('Operator process already exceeds memory budget')
    if resources['memory_available_bytes_before'] is not None and resources['memory_available_bytes_before'] < resources['memory_budget_bytes']:
        raise ValueError('Host has less than the 2 GiB build memory budget available')
    source_sha = file_sha(source)
    if source_sha != expected_sha256 or _closed_fingerprint(source) != before:
        raise ValueError('Source checksum/fingerprint mismatch before build')
    checkpoint_path = temporary.with_suffix(temporary.suffix + '.json')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro&immutable=1', uri=True)) as native, closing(sqlite3.connect(temporary)) as target:
        native.execute('PRAGMA cache_size=-65536')
        target.executescript('PRAGMA cache_size=-131072; PRAGMA temp_store=FILE; PRAGMA threads=1; PRAGMA mmap_size=0; PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;')
        _, view_sha = _projection(native)
        release_id = json.loads(native.execute("SELECT value_json FROM meta WHERE key='release_id'").fetchone()[0])
        expected = {tuple(row[:4]): row[4] for row in native.execute('SELECT series_accession,feature_type,contrast,analysis_version,total FROM effect_selector')}
        if any(any(type(v) is not str for v in key) or type(total) is not int or total < 0 for key, total in expected.items()):
            raise ValueError('Invalid native selector/type/total')
        binding = {'schema': SCHEMA, 'release_id': release_id, 'source_sha256': source_sha,
                   'source_fingerprint': before, 'view_sha256': view_sha,
                   'builder_sha256': file_sha(__file__), 'scientific_values_copied': 0,
                   'sort': ['feature_id (SQLite BINARY, NULL first)', 'id (native TEXT)'],
                   'native_ids': 'TEXT; source rowid is internal and never exported',
                   'reference_digest_encoding': 'binary-reference-tuple-v1'}
        if not resume:
            target.executescript('''
                CREATE TABLE header(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE selector(id INTEGER PRIMARY KEY,series_accession TEXT NOT NULL,feature_type TEXT NOT NULL,contrast TEXT NOT NULL,analysis_version TEXT NOT NULL,total INTEGER NOT NULL,UNIQUE(series_accession,feature_type,contrast,analysis_version));
                CREATE TABLE raw_ref(source_rowid INTEGER PRIMARY KEY,effect_id TEXT NOT NULL,selector_id INTEGER NOT NULL,feature_id TEXT);
                CREATE TABLE source_batch(first_rowid INTEGER PRIMARY KEY,last_rowid INTEGER NOT NULL,total INTEGER NOT NULL,sha256 TEXT NOT NULL);
                CREATE TABLE page_ref(selector_id INTEGER NOT NULL,ordinal INTEGER NOT NULL,ordinal_feature INTEGER NOT NULL,source_rowid INTEGER NOT NULL,PRIMARY KEY(selector_id,ordinal)) WITHOUT ROWID;
            ''')
            target.execute('INSERT INTO header VALUES(?,?)', ('binding', canonical(binding)))
            target.executemany('INSERT INTO selector(series_accession,feature_type,contrast,analysis_version,total) VALUES(?,?,?,?,?)', [(*key, total) for key, total in sorted(expected.items())])
            state = {'phase': 'scan', 'last_rowid': 0, 'scanned': 0, 'counts': {}, 'page_count': 0, 'duplicate_five_part_keys': 0}
            target.execute('INSERT INTO header VALUES(?,?)', ('checkpoint', canonical(state)))
            target.commit()
        else:
            old = json.loads(target.execute("SELECT value FROM header WHERE key='binding'").fetchone()[0])
            if canonical(old) != canonical(binding):
                raise ValueError('Resume source/release/schema/builder binding changed')
            state = json.loads(target.execute("SELECT value FROM header WHERE key='checkpoint'").fetchone()[0])
        selectors = {tuple(row[1:5]): row[0] for row in target.execute('SELECT id,series_accession,feature_type,contrast,analysis_version FROM selector')}
        batches = 0

        def checkpoint():
            nonlocal batches
            if _closed_fingerprint(source) != before:
                raise ValueError('Source/WAL changed during index build')
            rss = _rss_bytes()
            resources['rss_bytes_peak_sampled'] = max(resources.get('rss_bytes_peak_sampled', 0), rss or 0)
            if rss and rss > resources['memory_budget_bytes']:
                raise ValueError('Reference builder exceeded 2 GiB memory budget')
            target.execute("UPDATE header SET value=? WHERE key='checkpoint'", (canonical(state),))
            target.commit()
            atomic_json(checkpoint_path, {'binding': binding, 'state': state, 'resources': resources, 'elapsed_seconds': time.monotonic() - started})
            batches += 1
            return stop_after_batches is None or batches < stop_after_batches

        while state['phase'] == 'scan':
            rows = native.execute('SELECT rowid,id,series_accession,feature_type,contrast,analysis_version,feature_id FROM effect NOT INDEXED WHERE rowid>? ORDER BY rowid LIMIT ?', (state['last_rowid'], batch_size)).fetchall()
            if not rows:
                if any(state['counts'].get(str(selectors[key]), 0) != total for key, total in expected.items()):
                    raise ValueError('Native effect_selector totals disagree with the physical source stream')
                state['phase'] = 'sort'
                if not checkpoint():
                    return {'complete': False, **state}
                break
            values = []
            for rid, identifier, series, feature_type, contrast, version, feature in rows:
                if type(identifier) is not str or (feature is not None and type(feature) is not str):
                    raise ValueError('Native id/feature types are not TEXT/NULL')
                key = (series, feature_type, contrast, version)
                if key not in selectors:
                    raise ValueError('Source row lacks its declared selector')
                sid = selectors[key]
                values.append((rid, identifier, sid, feature))
                countkey = str(sid)
                state['counts'][countkey] = state['counts'].get(countkey, 0) + 1
            target.executemany('INSERT INTO raw_ref VALUES(?,?,?,?)', values)
            batch_digest = hashlib.sha256()
            for value in values:
                _typed_reference(batch_digest, value)
            target.execute('INSERT INTO source_batch VALUES(?,?,?,?)', (values[0][0], values[-1][0], len(values), batch_digest.hexdigest()))
            state['last_rowid'], state['scanned'] = rows[-1][0], state['scanned'] + len(rows)
            if not checkpoint():
                return {'complete': False, **state}
        if state['phase'] == 'sort':
            target.execute('CREATE INDEX IF NOT EXISTS raw_order ON raw_ref(selector_id,feature_id,effect_id,source_rowid)')
            state['phase'] = 'pages'
            if not checkpoint():
                return {'complete': False, **state}
        if state['phase'] == 'pages':
            last = state.get('last_ordered')
            where, args = '', []
            if last:
                sid, feature, identifier, rid = last
                feature_later = 'feature_id IS NOT NULL' if feature is None else 'feature_id>?'
                args = [sid, sid] + ([] if feature is None else [feature]) + [feature, identifier, identifier, rid]
                where = 'WHERE selector_id>? OR (selector_id=? AND (' + feature_later + ' OR (feature_id IS ? AND (effect_id>? OR (effect_id=? AND source_rowid>?)))))'
            ordered_cursor = target.execute('SELECT selector_id,feature_id,effect_id,source_rowid FROM raw_ref INDEXED BY raw_order ' + where + ' ORDER BY selector_id,feature_id,effect_id,source_rowid', args)
        while state['phase'] == 'pages':
            rows = ordered_cursor.fetchmany(batch_size)
            if not rows:
                state['phase'] = 'validate'
                checkpoint()
                break
            page_values = []
            for sid, feature, identifier, rid in rows:
                previous = state.get('last_ordered')
                state['ordinal'] = state.get('ordinal', 0) + 1 if previous and previous[0] == sid else 1
                same_feature = previous and previous[:2] == [sid, feature]
                state['ordinal_feature'] = state.get('ordinal_feature', 0) + 1 if same_feature else 1
                if same_feature:
                    state['duplicate_five_part_keys'] += 1
                page_values.append((sid, state['ordinal'], state['ordinal_feature'], rid))
                state['last_ordered'] = [sid, feature, identifier, rid]
                state['page_count'] += 1
            target.executemany('INSERT INTO page_ref VALUES(?,?,?,?)', page_values)
            if not checkpoint():
                return {'complete': False, **state}
        # Every row/reference and multiplicity is checked in sequential sidecar streams.
        reference_digest = hashlib.sha256()
        batches_cursor = iter(target.execute('SELECT first_rowid,last_rowid,total,sha256 FROM source_batch ORDER BY first_rowid'))
        expected_batch = next(batches_cursor, None)
        batch_digest, batch_count = hashlib.sha256(), 0
        for row in target.execute('SELECT source_rowid,effect_id,selector_id,feature_id FROM raw_ref ORDER BY source_rowid'):
            _typed_reference(reference_digest, row)
            _typed_reference(batch_digest, row)
            batch_count += 1
            if expected_batch is None or (batch_count == 1 and row[0] != expected_batch[0]):
                raise ValueError('Reference stream is missing a native batch boundary')
            if row[0] == expected_batch[1]:
                if batch_count != expected_batch[2] or batch_digest.hexdigest() != expected_batch[3]:
                    raise ValueError('Native source reference and sidecar values differ')
                expected_batch = next(batches_cursor, None)
                batch_digest, batch_count = hashlib.sha256(), 0
        if expected_batch is not None or batch_count:
            raise ValueError('Incomplete native reference batch proof')
        ordered_digest = hashlib.sha256()
        actual_counts = Counter()
        previous_order, previous_feature_ordinal = None, 0
        for sid, ordinal, feature_ordinal, rid, identifier, feature in target.execute('SELECT p.selector_id,p.ordinal,p.ordinal_feature,p.source_rowid,r.effect_id,r.feature_id FROM page_ref p JOIN raw_ref r ON r.source_rowid=p.source_rowid ORDER BY p.selector_id,p.ordinal'):
            actual_counts[sid] += 1
            if ordinal != actual_counts[sid]:
                raise ValueError('Noncontiguous page ordinals')
            order = (sid, int(feature is not None), feature or '', identifier, rid)
            expected_feature_ordinal = previous_feature_ordinal + 1 if previous_order and previous_order[:3] == order[:3] else 1
            if (previous_order is not None and order <= previous_order) or feature_ordinal != expected_feature_ordinal:
                raise ValueError('Native feature/id ordering, uniqueness or feature ordinals changed')
            previous_order, previous_feature_ordinal = order, feature_ordinal
            _typed_reference(ordered_digest, (sid, ordinal, feature_ordinal, rid, identifier, feature))
        if state['scanned'] != state['page_count'] or any(actual_counts[sid] != expected[key] for key, sid in selectors.items()):
            raise ValueError('Reference/page/selector totals changed')
        # Feature pagination uses the same reference order, with bounded ordinal lookup.
        target.execute('CREATE INDEX IF NOT EXISTS raw_feature ON raw_ref(selector_id,feature_id,source_rowid)')
        target.execute('CREATE INDEX IF NOT EXISTS page_source ON page_ref(source_rowid)')
        target.execute('CREATE TABLE IF NOT EXISTS feature_page(selector_id INTEGER NOT NULL,feature_id TEXT,ordinal_feature INTEGER NOT NULL,source_rowid INTEGER NOT NULL)')
        if not target.execute('SELECT 1 FROM feature_page LIMIT 1').fetchone():
            target.execute('INSERT INTO feature_page SELECT p.selector_id,r.feature_id,p.ordinal_feature,p.source_rowid FROM page_ref p JOIN raw_ref r ON r.source_rowid=p.source_rowid')
        target.execute('CREATE INDEX IF NOT EXISTS feature_range ON feature_page(selector_id,feature_id,ordinal_feature,source_rowid)')
        binding.update(total=state['scanned'], selectors=len(selectors), duplicate_five_part_keys=state['duplicate_five_part_keys'],
                       native_reference_sha256=reference_digest.hexdigest(), ordered_reference_sha256=ordered_digest.hexdigest(),
                       all_reference_counts_checked=True, build_resources=resources,
                       unique_five_part_key='not assumed; duplicate records and native id tie ordering preserved')
        target.execute("UPDATE header SET value=? WHERE key='binding'", (canonical(binding),))
        state['phase'] = 'complete'
        target.execute("UPDATE header SET value=? WHERE key='checkpoint'", (canonical(state),))
        target.commit()
        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Reference index integrity check failed')
    if _closed_fingerprint(source) != before or file_sha(source) != expected_sha256 or _closed_fingerprint(source) != before:
        raise ValueError('Source checksum/fingerprint changed after build')
    if output.exists():
        raise FileExistsError(output)
    temporary.replace(output)
    result = {**binding, 'complete': True, 'index_sha256': file_sha(output), 'output': str(output),
              'elapsed_seconds': time.monotonic() - started, 'source_sha256_after': expected_sha256,
              'source_physical_streams': 1, 'checksum_streams_per_attempt': 2}
    atomic_json(output.with_suffix('.json'), result)
    atomic_json(checkpoint_path, {**result, 'state': state})
    return result


def verify_index(index_path, source_path, release_id, source_sha256):
    path, source = Path(index_path).resolve(), Path(source_path).resolve()
    before, source_before = _closed_fingerprint(path), _closed_fingerprint(source)
    receipt = json.loads(path.with_suffix('.json').read_text())
    physical = physical_read_path(path).resolve()
    actual = file_sha(physical)
    with closing(sqlite3.connect(physical.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        binding = json.loads(db.execute("SELECT value FROM header WHERE key='binding'").fetchone()[0])
        state = json.loads(db.execute("SELECT value FROM header WHERE key='checkpoint'").fetchone()[0])
    if (state.get('phase') != 'complete' or binding.get('schema') != SCHEMA
            or actual != receipt.get('index_sha256') or before != fingerprint(path)
            or binding.get('source_sha256') != source_sha256 or binding.get('release_id') != release_id
            or canonical(binding.get('source_fingerprint')) != canonical(source_before)
            or _closed_fingerprint(source) != source_before):
        raise ValueError('Public omics sidecar checksum/source/release binding mismatch')
    _VERIFIED[str(path)] = (before, source_before, release_id, binding)
    return {'path': str(path), 'sha256': actual, 'release_id': release_id,
            'references': binding['total'], 'scientific_values_copied': 0}


def page_rows(source_db, source_path, release_id, native_key, offset, page_size, feature_id=None):
    """None means unconfigured. Configured stale/missing artifacts fail explicitly."""
    configured = os.environ.get(ENV)
    if not configured:
        return None
    path = Path(configured).resolve()
    try:
        verified = _VERIFIED.get(str(path))
        if not verified:
            raise QueryUnavailable('source_unavailable')  # readiness must verify full checksum
        index_fp, source_fp, verified_release, binding = verified
        if (_closed_fingerprint(path) != index_fp or _closed_fingerprint(source_path) != source_fp
                or verified_release != release_id):
            raise QueryUnavailable('source_changed')
        projection, view_sha = _projection(source_db)
        if view_sha != binding['view_sha256']:
            raise QueryUnavailable('source_changed')
        with read_connection(path) as index:
            selector = index.execute('SELECT id,total FROM selector WHERE series_accession=? AND feature_type=? AND contrast=? AND analysis_version=?', native_key).fetchone()
            if not selector:
                raise QueryUnavailable('analysis_key_mismatch')
            sid, total = selector
            if feature_id:
                # MAX on covering index reads the terminal ordinal, not every matching row.
                total = index.execute('SELECT COALESCE(MAX(ordinal_feature),0) FROM feature_page WHERE selector_id=? AND feature_id=?', (sid, feature_id)).fetchone()[0]
                refs = list(index.execute('SELECT r.source_rowid,r.effect_id,r.feature_id FROM feature_page p JOIN raw_ref r ON r.source_rowid=p.source_rowid WHERE p.selector_id=? AND p.feature_id=? AND p.ordinal_feature>? AND p.ordinal_feature<=? ORDER BY p.ordinal_feature', (sid, feature_id, offset, offset + page_size)))
            else:
                refs = list(index.execute('SELECT r.source_rowid,r.effect_id,r.feature_id FROM page_ref p JOIN raw_ref r ON r.source_rowid=p.source_rowid WHERE p.selector_id=? AND p.ordinal>? AND p.ordinal<=? ORDER BY p.ordinal', (sid, offset, offset + page_size)))
        if len(refs) != max(0, min(page_size, total - offset)):
            raise QueryUnavailable('analysis_key_mismatch')
        identifiers = sorted(r[0] for r in refs)
        native_rows = source_db.execute('SELECT rowid AS _native_row_reference,' + projection + ' FROM effect WHERE rowid IN (' + ','.join('?' * len(identifiers)) + ')', identifiers).fetchall() if identifiers else []
        by_rowid = {r['_native_row_reference']: dict(r) for r in native_rows}
        rows = []
        for rid, identifier, feature in refs:
            row = by_rowid.get(rid)
            if (not row or type(row['id']) is not str or row['id'] != identifier
                    or tuple(row[k] for k in KEYS) != tuple(native_key)
                    or row['feature_id'] != feature):
                raise QueryUnavailable('analysis_key_mismatch')
            row.pop('_native_row_reference')
            rows.append(row)
        if fingerprint(source_path) != source_fp or fingerprint(path) != index_fp:
            raise QueryUnavailable('source_changed')
        return total, rows
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError) as exc:
        raise QueryUnavailable('source_unavailable') from exc
