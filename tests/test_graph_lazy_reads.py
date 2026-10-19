"""Cold graph work follows visited nodes instead of speculative frontier size."""
import sqlite3

import nutrigenomics.explorer_graph_reads as graph_reads


def test_kind_projection_avoids_loading_large_node_payload_and_retains_legacy_fallback(monkeypatch):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE node(id TEXT PRIMARY KEY,kind TEXT,metadata_json TEXT)')
    db.executemany('INSERT INTO node VALUES(?,?,?)', [('gene','gene','x'*50000),('legacy','outcome','')])
    db.execute('CREATE TABLE node_kind(id TEXT PRIMARY KEY,kind TEXT NOT NULL)')
    db.execute("INSERT INTO node_kind VALUES('gene','gene')")
    monkeypatch.setattr(graph_reads, 'has_index', lambda *_: False)
    reader = graph_reads.GraphReads(db, None, None, 'all', None, False)
    statements = []
    db.set_trace_callback(statements.append)
    assert reader.node_kind('gene') == 'gene'
    assert not any(sql.startswith('SELECT kind FROM node WHERE') for sql in statements)
    assert reader.node_kind('legacy') == 'outcome'
    assert reader.node_kind('absent') is None
    plan = ' '.join(str(row[3]) for row in db.execute('EXPLAIN QUERY PLAN SELECT kind FROM node_kind WHERE id=?', ('gene',)))
    assert 'SEARCH' in plan and 'id=?' in plan
    db.close()


def test_kind_lookahead_is_primary_key_bounded_and_request_local(monkeypatch):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE node(id TEXT PRIMARY KEY,kind TEXT,metadata_json TEXT)')
    db.executemany('INSERT INTO node VALUES(?,?,?)', (
        (f'node:{index}', 'gene' if index == 18 else 'regulatory_region', '{"source":"unchanged"}')
        for index in range(10000)
    ))
    monkeypatch.setattr(graph_reads, 'has_index', lambda *_: True)
    reader = graph_reads.GraphReads(db, None, None, 'all', None, False)
    statements = []
    db.set_trace_callback(statements.append)
    assert reader.node_kind('node:18') == 'gene'
    assert reader.node_kind('node:18') == 'gene'
    assert reader.node_kind('absent') is None
    assert reader.node_kind('absent') is None
    reader.nodes['already-loaded'] = {'kind': 'outcome'}
    assert reader.node_kind('already-loaded') == 'outcome'
    kind_reads = [sql for sql in statements if sql.startswith('SELECT kind FROM node')]
    assert len(kind_reads) == 2
    assert all(' WHERE id=' in sql for sql in kind_reads)
    assert not any('SELECT id,kind FROM node' in sql for sql in statements)
    assert set(reader.kinds) == {'node:18', 'absent'}
    plan = ' '.join(str(row[3]) for row in db.execute('EXPLAIN QUERY PLAN SELECT kind FROM node WHERE id=?', ('node:18',)))
    assert 'SEARCH' in plan and 'id=?' in plan
    # A subsequent request does not inherit a previous source's kind cache.
    db.execute("UPDATE node SET kind='outcome' WHERE id='node:18'")
    fresh = graph_reads.GraphReads(db, None, None, 'all', None, False)
    assert fresh.node_kind('node:18') == 'outcome'
    db.close()


def test_prefetch_defers_all_endpoint_reads_until_a_node_is_popped(monkeypatch):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE perf_adj(edge_id TEXT PRIMARY KEY,subject TEXT,object TEXT,rank_default INTEGER,relation TEXT,tier TEXT)')
    db.executemany('INSERT INTO perf_adj VALUES(?,?,?,?,?,?)', (
        (f'edge:{origin:02}:{index:04}', f'origin:{origin}', f'neighbor:{origin}:{index}', index % 3, 'association', 'curated')
        for origin in range(32) for index in range(60)
    ))
    for direction in ('subject', 'object'):
        db.execute(f'CREATE INDEX perf_adj_{direction}_cover ON perf_adj({direction},rank_default,edge_id,relation,tier)')
    monkeypatch.setattr(graph_reads, 'has_index', lambda *_: True)
    reader = graph_reads.GraphReads(db, None, None, 'all', None, False)
    statements = []
    db.set_trace_callback(statements.append)
    reader.prefetch_adjacent([f'origin:{index}' for index in range(32)], 0, 11)
    assert len(reader.pending) == 32
    assert all(len(ids) == 11 and all(isinstance(identifier, str) for identifier in ids) for ids in reader.pending.values())
    assert not any(sql.startswith('SELECT edge_id id,edge_id,subject,object') for sql in statements)
    expected = [dict(row) for row in db.execute(
        'SELECT edge_id AS id,edge_id,subject,object FROM perf_adj WHERE subject=? OR object=? ORDER BY rank_default,edge_id LIMIT 11',
        ('origin:0', 'origin:0'),
    )]
    assert reader.adjacent('origin:0', 0, 11, hydrate=False) == expected
    endpoint_reads = [sql for sql in statements if sql.startswith('SELECT edge_id id,edge_id,subject,object')]
    assert len(endpoint_reads) == 1
    assert "edge:00:" in endpoint_reads[0] and "edge:01:" not in endpoint_reads[0]
    assert len(reader.pending) == 31
    db.close()


def test_empty_prefetched_node_needs_no_endpoint_lookup(monkeypatch):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE perf_adj(edge_id TEXT,subject TEXT,object TEXT,rank_default INTEGER,relation TEXT,tier TEXT)')
    monkeypatch.setattr(graph_reads, 'has_index', lambda *_: True)
    reader = graph_reads.GraphReads(db, None, None, 'all', None, False)
    reader.prefetch_adjacent(['absent-one', 'absent-two'], 0, 11)
    statements = []
    db.set_trace_callback(statements.append)
    assert reader.adjacent('absent-one', 0, 11, hydrate=False) == []
    assert statements == []
    db.close()
