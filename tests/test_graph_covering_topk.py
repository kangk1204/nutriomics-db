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
    for direction in ['subject', 'object']:
        db.execute(f'CREATE INDEX perf_adj_{direction}_cover ON perf_adj({direction},rank_default,edge_id,relation,tier)')
    if partial:
        db.execute("CREATE INDEX perf_adj_object_traversal_cover ON perf_adj(object,rank_default,edge_id,relation,tier) WHERE relation NOT IN ('contains','contains_nutrient','contains_analytical_flavonoid','chemical_gene_influence')")
    monkeypatch.setattr(graph_reads, 'has_index', lambda *_: True)
    reader = graph_reads.GraphReads(db, None, None, 'lipids' if filtered else 'all',
                                    ['curated', 'inferred'] if filtered else None, False)
    reader.load_edges = lambda ids: ids
    clause, args = reader.clause()
    inbound = " AND a.relation NOT IN ('contains','contains_nutrient','contains_analytical_flavonoid','chemical_gene_influence')" if level else ''
    baseline = ("SELECT edge_id FROM (SELECT a.edge_id,a.rank_default FROM perf_adj a WHERE a.subject=?" + clause +
                " UNION SELECT a.edge_id,a.rank_default FROM perf_adj a WHERE a.object=?" + clause + inbound +
                ') ORDER BY rank_default,edge_id LIMIT ?')
    expected = [r[0] for r in db.execute(baseline, ['seed', *args, 'seed', *args, limit])]
    assert reader.adjacent('seed', level, limit) == expected
    selected = reader.adjacent('seed', level, limit, hydrate=False)
    assert [r['id'] for r in selected] == expected
    assert all(set(r) == {'id', 'edge_id', 'subject', 'object'} for r in selected)
    db.close()
