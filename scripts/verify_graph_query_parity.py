"""Compare complete real-data graph responses with and without read indexes."""
import argparse
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from nutrigenomics.source_updates import atomic_json


def verify(database, cases, output, cache_mib=64):
    os.environ['NUTRIGENOMICS_EXPLORER_DATABASE'] = str(Path(database).resolve())
    from nutrigenomics import explorer_graph_reads, explorer
    from nutrigenomics.explorer import graph
    from nutrigenomics.explorer_build import sha256
    from nutrigenomics.explorer_performance import Budget, CURRENT

    spec = json.loads(Path(cases).read_text())
    results = []
    original_has_index = explorer_graph_reads.has_index
    original_database = explorer.database

    @contextmanager
    def verification_database():
        with original_database() as db:
            db.execute("PRAGMA cache_size=" + str(-1024 * cache_mib))
            yield db

    # A larger read cache is optional for semantic parity on rotational media.
    # This tool is not a latency benchmark; both modes receive the same cache.
    for case in (c for c in spec['cases'] if c['path'].endswith('/graph')):
        values = []
        for mode in ('indexed', 'native_sql'):
            atomic_json(Path(output), {'status': 'in_progress', 'case': case['id'], 'mode': mode, 'cases': results})
            explorer_graph_reads.has_index = original_has_index if mode == 'indexed' else lambda *args: False
            explorer.database = verification_database
            budget = Budget(300)
            token = CURRENT.set(budget)
            timer = threading.Timer(300, budget.cancel)
            timer.start()
            try:
                params = {'domain': 'all', 'tier': 'all', 'depth': 4, 'max_nodes': 80,
                          'max_edges': 200, 'include_extracted': False, **case['params']}
                value = graph(**params)
                values.append(value)
            finally:
                timer.cancel()
                CURRENT.reset(token)
                explorer_graph_reads.has_index = original_has_index
                explorer.database = original_database
        left, right = [json.dumps(v, sort_keys=True, ensure_ascii=False).encode() for v in values]
        results.append({'case': case['id'], 'equal': left == right,
            'indexed_sha256': hashlib.sha256(left).hexdigest(),
            'native_sql_sha256': hashlib.sha256(right).hexdigest(),
            'nodes': len(values[0]['nodes']), 'edges': len(values[0]['edges'])})
        atomic_json(Path(output), {'status': 'in_progress', 'cases': results})
        if left != right:
            raise ValueError('Real graph selection, values, context or omission counts changed')
    report = {'status': 'passed', 'database_sha256': sha256(database), 'case_sha256': spec['case_sha256'], 'read_cache_mib_each_mode': cache_mib,
              'scope': 'Full graph JSON including selected IDs, values, provenance, paths and omission counts', 'cases': results}
    atomic_json(Path(output), report)
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database', type=Path, required=True)
    p.add_argument('--cases', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--cache-mib', type=int, choices=(64, 128, 512, 4096), default=64)
    a = p.parse_args()
    print(json.dumps(verify(a.database, a.cases, a.output, a.cache_mib)))
