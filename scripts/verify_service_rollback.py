"""Rehearse code and release restoration on a separate loopback probe service."""
import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from nutrigenomics.explorer_build import canonical, sha256
from nutrigenomics.source_updates import atomic_json


def verify(current_code, previous_code, current_release, previous_release, output, port=8050):
    output = Path(output)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', port))
    results = []
    for label, code, release in [('current_before', current_code, current_release),
                                 ('previous_code_and_release', previous_code, previous_release),
                                 ('current_restored', current_code, current_release)]:
        code, release = Path(code).resolve(), Path(release).resolve()
        manifest = json.loads((release / 'manifest.json').read_text())
        database = release / 'explorer.sqlite'
        if sha256(database) != manifest['database_sha256']:
            raise ValueError('Rollback input checksum mismatch')
        env = os.environ.copy()
        env.update(PYTHONPATH=str(code), NUTRIGENOMICS_EXPLORER_DATABASE=str(database))
        env['NUTRIGENOMICS_EXPLORER_PROFILE'] = str(output.with_name(label + '.profile.jsonl'))
        child = ("from fastapi import FastAPI;from nutrigenomics.explorer import attach_explorer;"
                 "import uvicorn;app=FastAPI();attach_explorer(app);"
                 f"uvicorn.run(app,host='127.0.0.1',port={port})")
        with output.with_name(label + '.http.log').open('ab') as log:
            job = subprocess.Popen([sys.executable, '-c', child], cwd=code, env=env,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            for attempt in range(120):
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/v1/explorer/meta', timeout=2) as response:
                        metadata = json.load(response)
                    break
                except OSError:
                    if job.poll() is not None:
                        raise RuntimeError('Probe service failed; inspect its own log')
                    time.sleep(.25)
            else:
                raise RuntimeError('Rollback probe was not ready')
            assert metadata['release_id'] == manifest['release_id']
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/v1/explorer/graph?seed_id=NCBIGene%3A348', timeout=15) as response:
                graph = json.load(response)
            results.append({'step': label, 'code_root': str(code), 'release_id': metadata['release_id'],
                            'database_sha256': manifest['database_sha256'], 'http_status': 200,
                            'graph_sha256': hashlib.sha256(canonical(graph).encode()).hexdigest(),
                            'nodes': len(graph['nodes']), 'edges': len(graph['edges']),
                            'runtime_code_sha256': {name: sha256(code / name) for name in
                                ['nutrigenomics/explorer.py', 'nutrigenomics/explorer_graph_reads.py']}})
        finally:
            job.terminate()
            try:
                job.wait(timeout=10)
            except subprocess.TimeoutExpired:
                job.kill(); job.wait(timeout=10)
        atomic_json(output, {'status': 'in_progress', 'steps': results})
    passed = results[0]['graph_sha256'] == results[2]['graph_sha256'] and results[0]['database_sha256'] == results[2]['database_sha256']
    report = {'status': 'passed' if passed else 'failed', 'passed': passed, 'steps': results,
              'scope': 'Actual code plus release replacement and restore on an isolated loopback HTTP process; production listeners unchanged'}
    atomic_json(output, report)
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ['current-code', 'previous-code', 'current-release', 'previous-release', 'output']:
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--port', type=int, default=8050)
    a = p.parse_args()
    result = verify(a.current_code, a.previous_code, a.current_release, a.previous_release, a.output, a.port)
    print(json.dumps({'status': result['status'], 'steps': len(result['steps'])}))
    raise SystemExit(0 if result['passed'] else 1)
