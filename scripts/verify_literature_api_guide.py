"""Exercise the documented API against isolated synthetic data; never starts workers."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'literature_service')]

from fastapi import FastAPI
from fastapi.testclient import TestClient
from nutri_literature.auth import create_account
from nutri_literature.db import connect, digest, initialize
from nutri_literature.settings import Settings
from nutri_literature import platform_control
from nutrigenomics.literature_api import attach_literature
from nutrigenomics.literature_management import create_app
from nutrigenomics.literature_snapshot import build_snapshot


def verify(output):
    results = []
    def call(client, method, path, expected=200, **kwargs):
        response = client.request(method, path, **kwargs)
        if response.status_code != expected:
            raise AssertionError(f'{method} {path}: {response.status_code} != {expected}; {response.text[:200]}')
        results.append({'method': method, 'path': path, 'expected_status': expected, 'actual_status': response.status_code})
        return response

    with tempfile.TemporaryDirectory(prefix='nutriomics-api-guide-') as temporary:
        home = Path(temporary)
        config = home / 'topics.json'
        config.write_text(json.dumps({'topics': [{'id': 'HTN', 'name': 'HTN', 'query': 'hypertension',
                                                 'start_date': '2010-01-01', 'enabled': True}]}), encoding='utf8')
        settings = Settings(home / 'runtime', config)
        initialize(settings); platform_control.initialize(settings)
        create_account(settings, 'operator', 'synthetic-fixture-password')
        platform_control.grant(settings, 'operator', 'operator')
        create_account(settings, 'reviewer', 'synthetic-fixture-password')
        text = 'β 😀 Vitamin D reduced ACE expression.'
        text_hash = digest(text)
        with connect(settings) as con:
            for i, license_value in [(1, 'CC BY 4.0'), (2, None)]:
                stamp = '2026-10-24T00:00:00+00:00'
                con.execute('INSERT INTO papers(paper_id,pmid,title,pub_date,abstract,organism,oa_status,license,created_at,updated_at,current_source_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                            (i, str(i), 'Synthetic Vitamin D study', '2020-01-01', text, 'human', 'available', license_value, stamp, stamp, 's' + str(i)))
                con.execute('INSERT INTO sources(source_id,paper_id,kind,sha256,text_hash,raw_path,text_content,url,license,retrieved_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                            ('s' + str(i), i, 'fulltext', text_hash, text_hash, 'synthetic.xml', text, 'https://example.invalid', license_value, stamp))
                con.execute('INSERT INTO extractions(extraction_id,paper_id,source_id,source_hash,engine_version,dictionary_version,result_json,created_at) VALUES(?,?,?,?,?,?,?,?)',
                            ('e' + str(i), i, 's' + str(i), text_hash, 'heuristic-v1', 'synthetic', json.dumps({'summary': {'results': [text]}}), stamp))
                con.execute('INSERT INTO assertions(assertion_id,extraction_id,paper_id,fingerprint,subject,predicate,object,quote,start,end,rule_id,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                            ('a' + str(i), 'e' + str(i), i, 'f' + str(i), 'MESH:D000001', 'decreases', 'HGNC:2707', text, 0, len(text), 'synthetic-rule', '{}'))
        public = home / 'public'; internal = home / 'internal'
        build_snapshot(settings.database, public, release_id='synthetic-api-guide')
        build_snapshot(settings.database, internal, release_id='synthetic-api-guide-internal', public=False)
        # Emulate an older immutable snapshot that still stores development targets.
        import sqlite3
        from nutrigenomics.literature_snapshot import filehash
        with sqlite3.connect(public / 'literature.sqlite3') as db:
            payload = {'arm': 'synthetic', 'papers': 100, 'gold_relations': 154, 'predicted_relations': 0,
                       'metrics': {'tp': 0, 'fp': 0, 'fn': 154, 'precision': None, 'recall': 0, 'f1': 0},
                       'targets': {'precision': 0.85}}
            db.execute('INSERT INTO benchmarks VALUES(?,?)', ('synthetic', json.dumps(payload)))
        db.close()
        manifest_path = public / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf8'))
        manifest['database_sha256'] = filehash(public / 'literature.sqlite3')
        manifest_path.write_text(json.dumps(manifest), encoding='utf8')
        app = FastAPI(); attach_literature(app, database_path=public)
        client = TestClient(app)
        base = '/api/v1/explorer/literature'
        call(client, 'GET', '/docs'); schema = call(client, 'GET', '/openapi.json').json()
        stats = call(client, 'GET', base + '/stats').json()
        assert stats['status'] == 'available' and stats['counts']['unique_papers'] == 2
        assert all('targets' not in row for row in stats['benchmarks'])
        batch = call(client, 'GET', base + '/papers?page=1&page_size=5').json()
        assert batch['total'] == 2 and len(batch['items']) == 2
        found = call(client, 'GET', base + '/papers', params={'q': 'Vitamin D', 'year_from': 2020, 'fulltext': True, 'species': 'human'}).json()
        assert found['total'] == 2
        paper = call(client, 'GET', base + '/papers/1?engine=heuristic-v1').json()
        assert paper['summary'] is not None
        hidden = call(client, 'GET', base + '/papers/2').json()
        assert hidden['source_text'] is None and hidden['summary'] is None
        assert call(client, 'GET', base + '/papers/1?engine=unknown').json()['summary'] is None
        events = call(client, 'GET', base + '/events?pmid=1&engine=heuristic-v1&page_size=200').json()
        event = events['items'][0]
        assert paper['source_text'][event['start']:event['end']] == event['quote'] == text
        assert paper['source_sha256'] == event['source_sha256']
        assert call(client, 'GET', base + '/papers', params={'gene': 'HGNC:2707'}).json()['total'] == 1
        assert all('targets' not in row for row in call(client, 'GET', base + '/benchmarks').json()['items'])
        call(client, 'GET', base + '/reports')
        for form in ('html', 'md', 'csv', 'json'):
            response = call(client, 'GET', base + '/reports/current?format=' + form)
            assert response.content and 'Content-Disposition' in response.headers
            if form == 'json':
                assert all('targets' not in row for row in response.json()['benchmarks'])
        call(client, 'GET', base + '/reports/current?format=csv&table=inventory')
        for suffix in ('/papers?page_size=201', '/papers?year_from=2026&year_to=2020',
                       '/papers?processing_status=invalid', '/reports/current?format=pdf'):
            call(client, 'GET', base + suffix, expected=422)
        call(client, 'GET', base + '/papers/999', expected=404)
        call(client, 'POST', '/api/v1/literature-admin/jobs', expected=404, json={'kind': 'collect'})
        # Every public route and documented search parameter is in the API guide.
        guide = (ROOT / 'docs/LITERATURE_API_ko.md').read_text(encoding='utf8')
        for path, methods in schema['paths'].items():
            assert path.removeprefix(base) in guide
        params = [p['name'] for p in schema['paths'][base + '/papers']['get']['parameters']]
        assert all('`' + name + '`' in guide for name in params)
        internal_client = TestClient(create_app(settings, snapshot=internal))
        admin = '/api/v1/literature-admin'
        call(internal_client, 'GET', '/healthz')
        call(internal_client, 'GET', base + '/papers', expected=401)
        for username in ('reviewer', 'operator'):
            login = call(internal_client, 'POST', admin + '/session', json={'username': username, 'password': 'synthetic-fixture-password'})
            assert 'HttpOnly' in login.headers['set-cookie'] and 'access_token' not in login.json()
            headers = {'X-CSRF-Token': login.json()['csrf'], 'Idempotency-Key': 'api-guide-1'}
            call(internal_client, 'GET', admin + '/session')
            if username == 'reviewer':
                call(internal_client, 'POST', admin + '/jobs', expected=403, headers=headers, json={'kind': 'build-report'})
        call(internal_client, 'GET', base + '/papers/1')
        private_event = call(internal_client, 'GET', base + '/events?pmid=1').json()['items'][0]
        review = {'event_id': private_event['event_id'], 'source_sha256': private_event['source_sha256'],
                  'decision': 'unconfirmed', 'comment': 'Synthetic API contract check', 'human_confirmation': False}
        call(internal_client, 'POST', admin + '/reviews', expected=422, headers=headers, json=review)
        review['human_confirmation'] = True
        call(internal_client, 'POST', admin + '/reviews', headers=headers, json=review)
        call(internal_client, 'POST', admin + '/jobs', expected=403, json={'kind': 'build-report'})
        job = call(internal_client, 'POST', admin + '/jobs', expected=202, headers=headers, json={'kind': 'build-report', 'params': {}}).json()
        repeated = call(internal_client, 'POST', admin + '/jobs', expected=202, headers=headers, json={'kind': 'build-report', 'params': {}}).json()
        assert job['job_id'] == repeated['job_id'] and job['status'] == 'queued'
        call(internal_client, 'GET', admin + '/jobs')
        call(internal_client, 'POST', admin + '/jobs/' + job['job_id'] + '/cancel', headers=headers)
        call(internal_client, 'POST', admin + '/jobs/' + job['job_id'] + '/resume', headers=headers)
        call(internal_client, 'DELETE', admin + '/session', headers=headers)
        call(internal_client, 'GET', admin + '/jobs', expected=401)
        result = {'status': 'passed', 'scope': 'isolated synthetic API examples; not production data or model accuracy',
                  'workers_started': 0, 'external_model_calls': 0, 'checks': len(results), 'requests': results,
                  'unicode_quote_and_hash_match': True, 'restricted_text_withheld': True,
                  'search_parameters_documented': params,
                  'sources': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
                              ('docs/LITERATURE_API_ko.md', 'nutrigenomics/literature_api.py', 'nutrigenomics/literature_management.py')}}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    return {'status': result['status'], 'checks': result['checks'], 'output': str(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/reviewer/api-validation.json')
    print(json.dumps(verify(parser.parse_args().output), ensure_ascii=False))


if __name__ == '__main__':
    main()
