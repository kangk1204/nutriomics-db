"""Compare real read-only research APIs before/after integration, including pathway rows."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
from pathlib import Path
import time
import urllib.parse
import urllib.request


def sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def request(url, method, path, body=None, expected=200):
    payload = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url.rstrip('/') + path, data=payload, method=method,
                                 headers={'Content-Type': 'application/json'} if payload else {})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=50) as response:
            blob = response.read(); status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code; blob = exc.read()
    if status != expected:
        raise AssertionError(f'{method} {path}: {status} != {expected}; {blob[:180]!r}')
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    return data, {'method': method, 'path': path, 'body': body, 'status': status,
                  'sha256': sha(data) if data is not None else hashlib.sha256(blob).hexdigest(),
                  'bytes': len(blob), 'seconds': time.monotonic() - started}


def capture(url, spec=None, baseline=None, literature=False):
    cases = []
    if baseline:
        cases = [(r['method'], r['path'], r.get('body')) for r in baseline['research_requests']]
    else:
        cases = [('GET', p, None) for p in ('/healthz', '/api/v1/explorer/meta', '/api/v1/interventions')]
        for kind, queries in [('food_record', ('', '김치찌개', '된장찌개', '요구르트')), ('gene', ('APOE', 'HGNC:29')), ('component', ('vitamin D',))]:
            for q in queries:
                cases.append(('GET', '/api/v1/explorer/entities?' + urllib.parse.urlencode(dict(kind=kind, q=q, page=1, page_size=8)), None))
        foods = ['K-FIND:15100070:D306-266000000-0001:e6e7a11ea966', 'K-FIND:15100070:D106-275000000-0001:fa09ba8b4950', 'USDA-FDC:2259793']
        for identifier in [*foods, 'Ensembl:ENSG00000130203']:
            cases.append(('GET', '/api/v1/explorer/nodes/' + urllib.parse.quote(identifier, safe=''), None))
            cases.append(('GET', '/api/v1/explorer/graph?' + urllib.parse.urlencode(dict(seed_id=identifier, domain='all', tier='observed', max_nodes=80, max_edges=200, include_extracted='false')), None))
            body = {'seed_id': identifier, 'reference_context': {'tissue': None, 'response_time_h': None, 'regimen_duration_days': None, 'frequency': None}}
            if identifier in foods:
                body['intake_g'] = 150
                cases.append(('POST', '/api/v1/meals/analyze', {'items': [{'food_id': identifier, 'amount_g': 150}]}))
            cases.append(('POST', '/api/v1/explorer/match', body))
        if spec:
            for run in spec['runs']:
                expected_per_contrast = run['rows'] // len(run['contrasts'])
                assert expected_per_contrast * len(run['contrasts']) == run['rows']
                for contrast in run['contrasts']:
                    for page in range(1, math.ceil(expected_per_contrast / 200) + 1):
                        params = dict(series=run['series_accession'], feature_type='pathway', contrast=contrast,
                                      analysis_version=run['parent_analysis_version'], pathway_selection='corrected_if_available', page=page, page_size=200)
                        cases.append(('GET', '/api/v1/explorer/omics/results?' + urllib.parse.urlencode(params), None))
    records = []; mismatches = []; pathway_rows = 0; meta = None
    for index, (method, path, body) in enumerate(cases):
        data, record = request(url, method, path, body)
        if path == '/api/v1/explorer/meta':
            meta = data
        if path.startswith('/api/v1/explorer/omics/results?'):
            assert data['status'] == 'available', path
            pathway_rows += len(data['items'])
        if baseline and record['sha256'] != baseline['research_requests'][index]['sha256']:
            mismatches.append({'index': index, 'path': path, 'before': baseline['research_requests'][index]['sha256'], 'after': record['sha256']})
        records.append(record)
        if (index + 1) % 50 == 0:
            print(json.dumps({'research_requests_checked': index + 1, 'total': len(cases)}), flush=True)
    assert meta is not None
    assert not mismatches, json.dumps(mismatches[:5])
    if spec:
        assert pathway_rows == sum(run['rows'] for run in spec['runs']) == 50550
    if baseline:
        assert meta['release_id'] == baseline['science_release_id']
        assert meta['counts'] == baseline['science_counts']
        assert pathway_rows == baseline['pathway_rows_checked']
    literature_checks = []; snapshot = None
    if literature:
        base = '/api/v1/explorer/literature'
        snapshot, r = request(url, 'GET', base + '/stats'); literature_checks.append(r)
        assert snapshot['status'] == 'available' and snapshot['scope'] == 'public'
        papers, r = request(url, 'GET', base + '/papers?year_from=1500&page_size=5'); literature_checks.append(r)
        assert papers['total'] == snapshot['counts']['unique_papers'] and papers['items']
        assert papers['release_id'] == snapshot['release_id']
        _, r = request(url, 'GET', base + '/papers?year_from=2010&fulltext=true&page_size=5'); literature_checks.append(r)
        events, r = request(url, 'GET', base + '/events?engine=heuristic-v1&page_size=10'); literature_checks.append(r)
        for event in events['items']:
            paper, r = request(url, 'GET', base + '/papers/' + event['pmid'] + '?engine=heuristic-v1'); literature_checks.append(r)
            assert paper['source_sha256'] == event['source_sha256']
            assert paper['source_text'][event['start']:event['end']] == event['quote']
        metrics, r = request(url, 'GET', base + '/benchmarks'); literature_checks.append(r)
        assert all('targets' not in row for row in metrics['items'])
        assert any(row['metrics']['tp'] == 65 and row['metrics']['fp'] == 63 for row in metrics['items'])
        for form in ('html', 'md', 'csv', 'json'):
            _, r = request(url, 'GET', base + '/reports/current?format=' + form); literature_checks.append(r)
        _, r = request(url, 'POST', '/api/v1/literature-admin/jobs', {'kind': 'collect'}, expected=404); literature_checks.append(r)
        def concurrent(index):
            route = '/healthz' if index % 2 else base + '/stats'
            data, r = request(url, 'GET', route)
            assert data['release_id'] == (meta['release_id'] if index % 2 else snapshot['release_id'])
            return r
        with ThreadPoolExecutor(max_workers=4) as pool:
            literature_checks.extend(pool.map(concurrent, range(20)))
    return {'status': 'pass', 'url': url, 'science_release_id': meta['release_id'], 'science_counts': meta['counts'],
            'research_requests': records, 'pathway_rows_checked': pathway_rows, 'exact_json_mismatches': mismatches,
            'literature_snapshot_id': snapshot['release_id'] if snapshot else None,
            'literature_counts': snapshot['counts'] if snapshot else None, 'literature_requests': literature_checks,
            'scope': 'Operational API and unchanged values; not new model accuracy or expert approval',
            'new_model_calls': 0, 'scientific_approvals_created': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pathway-spec', type=Path)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--literature', action='store_true')
    args = parser.parse_args()
    spec = json.loads(args.pathway_spec.read_text(encoding='utf8')) if args.pathway_spec else None
    baseline = json.loads(args.baseline.read_text(encoding='utf8')) if args.baseline else None
    result = capture(args.url, spec, baseline, args.literature)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    print(json.dumps({'status': result['status'], 'research_requests': len(result['research_requests']),
                      'pathway_rows_checked': result['pathway_rows_checked'], 'literature_requests': len(result['literature_requests']),
                      'exact_json_mismatches': len(result['exact_json_mismatches']), 'output': str(args.output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
