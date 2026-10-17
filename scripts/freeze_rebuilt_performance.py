"""Freeze real rebuilt-release cases; do not silently substitute missing cohorts."""
import argparse
import json
import math
from datetime import UTC, datetime
from pathlib import Path

from nutrigenomics.explorer_build import read_only, sha256
from nutrigenomics.source_updates import atomic_json
from scripts.benchmark_explorer_performance import digest


def freeze(release, output, public=False, allow_missing_omics=False):
    release = Path(release)
    database = release / ('nutriomics.sqlite3' if public else 'explorer.sqlite')
    cases = []

    def add(name, path, params=None, payload=None, target=1):
        cases.append({'id': name, 'path': '/api/v1/explorer/' + path,
                      'params': params or {}, 'payload': payload, 'target_s': target})

    with read_only(database) as db:
        meta = {r[0]: json.loads(r[1]) for r in db.execute('SELECT * FROM meta')}
        food50 = [r[0] for r in db.execute("SELECT id FROM node WHERE kind='food' AND id LIKE 'K-FIND:%' ORDER BY id")]
        if not public and len(food50) != 50:
            raise ValueError('The fixed Korean food panel is incomplete')
        counts = dict(db.execute('SELECT kind,count(*) FROM node GROUP BY kind'))
        high = {kind: db.execute('SELECT id FROM node WHERE kind=? ORDER BY degree DESC,id LIMIT 1', (kind,)).fetchone()[0]
                for kind in ('food', 'gene', 'nutrient') if counts.get(kind)}
        yogurt = db.execute("SELECT id FROM node WHERE kind='food' AND lower(label) LIKE '%yogurt%' ORDER BY id LIMIT 1").fetchone()
        yogurt = yogurt[0] if yogurt else high['food']
        apoe = db.execute("SELECT id FROM node WHERE kind='gene' AND label='APOE' ORDER BY id LIMIT 1").fetchone()
        gene = apoe[0] if apoe else high.get('gene')
        add('food-search', 'entities', {'kind': 'food', 'q': 'yogurt'})
        add('food-phrase-search', 'entities', {'kind': 'food', 'q': 'Yogurt, whole milk, plain', 'page_size': 25})
        add('short-search', 'entities', {'kind': 'component', 'q': 'FA'})
        add('gene-degree', 'entities', {'kind': 'gene', 'sort': 'degree', 'order': 'desc'})
        add('health-coverage', 'entities', {'kind': 'health'})
        add('empty-search', 'entities', {'kind': 'food', 'q': '__absent_rebuild_20261017__'})
        for name, seed in [('yogurt', yogurt), *[(f'high-{kind}', value) for kind, value in high.items()]]:
            add(name + '-graph', 'graph', {'seed_id': seed})
        # This actual browser selection exposed a high-degree inbound scan.
        # Keep it in future frozen sets rather than only the first yogurt ID.
        plain = db.execute("SELECT id FROM node WHERE label='Yogurt, whole milk, plain' ORDER BY id LIMIT 1").fetchone()
        if plain:
            add('browser-plain-yogurt-graph', 'graph', {'seed_id': plain[0]})
            add('browser-plain-yogurt-maximum', 'graph',
                {'seed_id': plain[0], 'max_nodes': 250, 'max_edges': 750}, target=3)
        add('maximum-graph', 'graph', {'seed_id': high['food'], 'max_nodes': 250, 'max_edges': 750}, target=3)
        for kind in ('gene', 'nutrient'):
            if kind in high:
                add(kind + '-maximum-graph', 'graph', {'seed_id': high[kind], 'max_nodes': 250, 'max_edges': 750}, target=3)
        add('filtered-graph', 'graph', {'seed_id': yogurt, 'domain': 'blood_pressure', 'tier': 'curated'})
        add('yogurt-match', 'match', payload={'seed_id': yogurt, 'intake_g': 150})
        add('yogurt-context-match', 'match', payload={'seed_id': yogurt, 'intake_g': 800,
            'reference_context': {'tissue': 'whole blood', 'response_time_h': 6}})
        if gene:
            add('gene-detail', 'nodes/' + gene.replace(':', '%3A'))
            add('gene-evidence', 'evidence', {'seed_id': gene})
            add('yogurt-gene-match', 'match', payload={'seed_id': yogurt, 'intake_g': 150, 'gene_node_id': gene})
        summaries = list(db.execute('SELECT series_accession,analysis_version,contrast,feature_type,total n FROM effect_selector')) if public else list(db.execute('SELECT s.series series_accession,s.version analysis_version,s.contrast,t.feature_type,t.total n FROM perf_effect_selector s JOIN perf_effect_type t ON t.selector=s.selector'))
        if not allow_missing_omics:
            for series, label, main_v4 in [('GSE98645', 'GSE98645', False),
                                         ('GSE107205', 'GSE107205', False),
                                         ('GSE107205', 'GSE107205-main-v4', True)]:
                matches = sorted((dict(r) for r in summaries if r['series_accession'] == series
                                  and (not main_v4 or ('between-arm-v4-rebuild' in r['analysis_version']))),
                                 key=lambda r: (-r['n'], r['analysis_version'], r['contrast']))
                if not matches:
                    raise ValueError('Required reacquired result absent: ' + series)
                selected = matches[0]
                params = {'series': series, 'analysis_version': selected['analysis_version'],
                    'contrast': selected['contrast'], 'feature_type': selected['feature_type'], 'page_size': 25}
                for name, page in [('first', 1), ('middle', max(1, math.ceil(selected['n'] / 25) // 2)),
                                   ('last', math.ceil(selected['n'] / 25))]:
                    add(label + '-' + name, 'omics/results', {**params, 'page': page})
    result = {'frozen_at': datetime.now(UTC).isoformat(), 'baseline_release': meta['release_id'],
              'database_sha256': sha256(database), 'food50': food50, 'cases': cases,
              'case_sha256': digest(cases), 'high_degree_seeds': high,
              'coverage': counts, 'scope': 'Fresh public-source rebuild; source availability is recorded in release manifests, not inferred from this timing set'}
    result['omics_cases_omitted_for_preview'] = allow_missing_omics
    atomic_json(Path(output), result)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--release', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--public', action='store_true')
    p.add_argument('--allow-missing-omics', action='store_true', help='Engineering preview only; never a final performance gate')
    a = p.parse_args()
    print(json.dumps(freeze(a.release, a.output, a.public, a.allow_missing_omics)))
