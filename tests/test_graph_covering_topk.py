"""The bounded two-branch query must preserve the exhaustive union selection."""
import sqlite3

import pytest

import nutrigenomics.explorer_graph_reads as graph_reads


@pytest.mark.parametrize('level', [0, 1])
@pytest.mark.parametrize('limit', [1, 80, 750])
@pytest.mark.parametrize('filtered', [False, True])
@pytest.mark.parametrize('partial', [False, True])
def test_high_degree_topk_ties_selfloops_filters(monkeypatch, level, limit, filtered, partial):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE perf_adj(edge_id TEXT,subject TEXT,object TEXT,rank_default INTEGER,relation TEXT,tier TEXT)')
    db.execute('CREATE TABLE edge_domain(edge_id TEXT,domain TEXT)')
    relations = ['contains', 'contains_nutrient', 'chemical_gene_influence', 'association']
    tiers = ['curated', 'inferred', 'extracted']
    rows = [(f'e{i:05}', 'seed' if i % 2 or i % 17 == 0 else f'n{i}',
             'seed' if i % 2 == 0 or i % 17 == 0 else f'n{i}',
             i % 11, relations[i % 4], tiers[i % 3]) for i in range(10000)]
    db.executemany('INSERT INTO perf_adj VALUES(?,?,?,?,?,?)', rows)
    db.executemany('INSERT INTO edge_domain VALUES(?,?)', [(r[0], 'lipids') for r in rows if int(r[0][1:]) % 5])
    db.execute('CREATE INDEX domain_edge ON edge_domain(domain,edge_id)')
    for direction in ['subject', 'object']:
        db.execute(f'CREATE INDEX perf_adj_{direction}_cover ON perf_adj({direction},rank_default,edge_id,relation,tier)')
    if partial:
        db.execute("CREATE INDEX perf_adj_object_traversal_cover ON perf_adj(object,rank_default,edge_id,relation,tier) WHERE relation NOT IN ('contains','contains_nutrient','contains_analytical_flavonoid','chemical_gene_influence')")
    monkeypatch.setattr(graph_reads, 'has_index', lambda *_: True)
    db.execute('PRAGMA query_only=ON')
    reader = graph_reads.GraphReads(db, None, None, 'lipids' if filtered else 'all',
                                    ['curated', 'inferred'] if filtered else None, False)
    reader.load_edges = lambda ids: ids
    clause = " AND EXISTS(SELECT 1 FROM edge_domain ed WHERE ed.edge_id=a.edge_id AND ed.domain=?) AND a.tier IN (?,?) AND a.tier!='extracted'" if filtered else " AND a.tier!='extracted'"
    args = ['lipids','curated','inferred'] if filtered else []
    inbound = " AND a.relation NOT IN ('contains','contains_nutrient','contains_analytical_flavonoid','chemical_gene_influence')" if level else ''
    baseline = ("SELECT edge_id FROM (SELECT a.edge_id,a.rank_default FROM perf_adj a WHERE a.subject=?" + clause +
                " UNION SELECT a.edge_id,a.rank_default FROM perf_adj a WHERE a.object=?" + clause + inbound +
                ') ORDER BY rank_default,edge_id LIMIT ?')
    expected = [r[0] for r in db.execute(baseline, ['seed', *args, 'seed', *args, limit])]
    assert reader.adjacent('seed', level, limit) == expected
    selected = reader.adjacent('seed', level, limit, hydrate=False)
    assert [r['id'] for r in selected] == expected
    assert all(set(r) == {'id', 'edge_id', 'subject', 'object'} for r in selected)
    reader.prefetch_adjacent(['seed', 'n1', 'n2', 'not-present'], level, limit)
    batched = reader.adjacent('seed', level, limit, hydrate=False)
    assert batched == selected
    for identifier in ['n1', 'n2', 'not-present']:
        exact = [r[0] for r in db.execute(baseline, [identifier, *args, identifier, *args, limit])]
        assert reader.adjacent(identifier, level, limit) == exact
    db.close()


def test_domain_projections_are_isolated_without_database_writes(monkeypatch):
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
    db.executescript('CREATE TABLE perf_adj(edge_id TEXT);CREATE TABLE edge_domain(edge_id TEXT,domain TEXT);')
    db.executemany('INSERT INTO edge_domain VALUES(?,?)',[('a','lipids'),('b','obesity'),('a','lipids')])
    db.execute('PRAGMA query_only=ON')
    monkeypatch.setattr(graph_reads,'has_index',lambda *_:True)
    lipid=graph_reads.GraphReads(db,None,None,'lipids',None,False)
    obesity=graph_reads.GraphReads(db,None,None,'obesity',None,False)
    missing=graph_reads.GraphReads(db,None,None,'absent',None,False)
    for reader, expected in [(lipid,[1,0,0]),(obesity,[0,1,0]),(missing,[0,0,0])]:
        assert [db.execute('SELECT '+reader.domain_function+'(?)',(v,)).fetchone()[0] for v in ['a','b',None]]==expected
    assert db.execute('PRAGMA query_only').fetchone()[0]==1
    with pytest.raises(sqlite3.OperationalError):db.execute("INSERT INTO edge_domain VALUES('c','lipids')")
    db.close()


def test_prefetch_limits_covering_branches_before_endpoint_hydration(monkeypatch):
    """K bounds execution work as well as output on a high-degree frontier."""
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('CREATE TABLE perf_adj(edge_id TEXT PRIMARY KEY,subject TEXT,object TEXT,rank_default INTEGER,relation TEXT,tier TEXT);'
                     'CREATE TABLE edge_domain(edge_id TEXT,domain TEXT);')
    db.executemany('INSERT INTO perf_adj VALUES(?,?,?,?,?,?)', (
        (f'e{i:06}', 'seed', f'n{i}', i % 5, 'association', 'curated') for i in range(30000)))
    db.execute("INSERT INTO perf_adj VALUES('small-edge','small','seed',2,'association','curated')")
    for direction in ('subject', 'object'):
        db.execute(f'CREATE INDEX perf_adj_{direction}_cover ON perf_adj({direction},rank_default,edge_id,relation,tier)')
    monkeypatch.setattr(graph_reads, 'has_index', lambda *_: True)
    reader = graph_reads.GraphReads(db, None, None, 'all', None, False)
    counts = [0]
    def step():
        counts[0] += 1
        return 0
    db.set_progress_handler(step, 1)
    old_sql = ("WITH ranked AS (SELECT a.subject AS origin,a.edge_id,a.subject,a.object,a.rank_default,"
               "row_number() OVER (PARTITION BY a.subject ORDER BY a.rank_default,a.edge_id) AS position "
               "FROM perf_adj a INDEXED BY perf_adj_subject_cover WHERE a.subject IN (?,?) AND a.tier!='extracted') "
               "SELECT origin,edge_id,subject,object,rank_default FROM ranked WHERE position<=?")
    list(db.execute(old_sql, ['seed', 'small', 11]))
    old_steps = counts[0]
    counts[0] = 0
    reader.prefetch_adjacent(['seed', 'small'], 0, 11)
    new_steps = counts[0]
    db.set_progress_handler(None, 0)
    assert new_steps < old_steps / 10, (old_steps, new_steps)
    for identifier in ('seed', 'small'):
        expected = [dict(row) for row in db.execute(
            "SELECT edge_id AS id,edge_id,subject,object FROM perf_adj WHERE subject=? OR object=? "
            "ORDER BY rank_default,edge_id LIMIT 11", (identifier, identifier))]
        assert reader.adjacent(identifier, 0, 11, hydrate=False) == expected
    db.close()
