"""Build an auditable local and rights-filtered public release from 14 fresh outcomes."""
import argparse
import json
import os
import sqlite3
from pathlib import Path

from nutrigenomics.explorer_build import build_release, sha256, verify_closed_sqlite
from nutrigenomics.explorer_scientific_build import upgrade_scientific_release
from nutrigenomics.explorer_performance_build import build_performance_release
from nutrigenomics.publication import build_candidate
from nutrigenomics.source_updates import atomic_json
from scripts.audit_rebuilt_outcomes import audit
from scripts.build_fresh_food_graph import build as build_graph
from scripts.import_analysis_results import import_analysis_results
from scripts.import_human_bridge import import_bridge
from scripts.prepare_public_service_release import prepare
from scripts.rebuild_curations import rebuild
from scripts.rebuild_public_omics import rebuilt_policy


def build(work, source):
    work, source = Path(work).resolve(), Path(source).resolve()
    status_path = work / 'release-rebuild-status.json'
    state = json.loads(status_path.read_text()) if status_path.exists() else {'completed': {}}

    def checkpoint(phase, result=None):
        state['phase'] = phase
        if result is not None:
            state['completed'][phase] = result
        atomic_json(status_path, state)
        print(json.dumps({'phase': phase, 'completed': result is not None}), flush=True)

    checkpoint('audit-executed-outcomes')
    result = audit(work, source)
    if result['status'] != 'completed':
        raise ValueError('Fourteen actually executed and frozen outcomes are required')
    checkpoint('audit-executed-outcomes', result)
    plan = json.loads((work / 'analysis-rebuild-plan.json').read_text())
    database = work / 'data/db/nutrigenomics.sqlite3'
    url = 'sqlite:///' + str(database)
    os.environ.setdefault('NUTRIGENOMICS_IMPORT_CACHE_MIB', '2048')
    for outcome in plan['outcomes']:
        key = 'import:' + outcome['analysis_version']
        if key in state['completed']:
            continue
        checkpoint(key)
        result = import_analysis_results(work / 'outputs' / outcome['folder'], url,
                                        outcome['series'], require_completion=True)
        checkpoint(key, result)
    checkpoint('bridge-context', import_bridge(source / 'config/human_exposures.v1.json', url))
    snapshot = work / 'snapshots/fresh-full-core-v2.sqlite3'
    snapshot.parent.mkdir(exist_ok=True)
    if not snapshot.exists():
        checkpoint('consistent-core-snapshot')
        temp = snapshot.with_suffix('.building')
        if temp.exists():
            raise ValueError('Unfinished snapshot exists; audit it before retrying')
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as src, sqlite3.connect(temp) as dest:
            src.backup(dest, pages=2048)
        verify_closed_sqlite(temp)
        temp.rename(snapshot)
        checkpoint('consistent-core-snapshot', {'sha256': sha256(snapshot)})
    graph = work / 'fresh-full-graph-v2.sqlite3'
    if not graph.exists():
        checkpoint('fresh-source-graph', build_graph(snapshot, graph, work / 'raw/hgnc/hgnc_complete_set.txt'))
    curation = work / 'fresh-curation-v2.json'
    if not curation.exists():
        checkpoint('exact-contrast-curation', rebuild(source / 'config/explorer_evidence_curations.json', snapshot, curation, plan))
    releases = work / 'releases'
    releases.mkdir(exist_ok=True)
    base = releases / 'reacquired-base-v2'
    science = releases / 'reacquired-scientific-v2'
    local_service = releases / 'reacquired-local-performance-v2'
    public = releases / 'reacquired-public-v2'
    service = releases / 'reacquired-public-service-v2'
    ctd = work / 'data/raw/ctd'
    ctd_status = json.loads((ctd / 'manifest.json').read_text()) if (ctd / 'manifest.json').exists() else {}
    if not base.exists():
        checkpoint('local-base-release')
        checkpoint('local-base-release', build_release(graph, snapshot, base, base.name,
            source / 'config/explorer_scope.json', ctd if ctd_status.get('status') == 'completed' else None))
    if not science.exists():
        checkpoint('scientific-context-release')
        checkpoint('scientific-context-release', upgrade_scientific_release(base, snapshot, science, science.name, curation))
    if not local_service.exists():
        checkpoint('local-performance-indexes')
        checkpoint('local-performance-indexes', build_performance_release(science, snapshot, local_service, local_service.name))
    policy = rebuilt_policy(source, work, plan)
    if not public.exists():
        checkpoint('rights-filtered-public-release')
        checkpoint('rights-filtered-public-release', build_candidate(science, snapshot, policy, public, public.name))
    if not service.exists():
        checkpoint('public-service-release')
        checkpoint('public-service-release', prepare(public, None, service, service.name))
    verify_closed_sqlite(service / 'nutriomics.sqlite3')
    checkpoint('release-build-complete', {'public': str(service), 'local': str(local_service),
        'core_snapshot': str(snapshot), 'scientific_expert_review': 'pending',
        'performance_accuracy_and_browser_gates': 'not_yet_executed'})
    return state


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('work', 'source'):
        p.add_argument('--' + name, type=Path, required=True)
    a = p.parse_args()
    build(a.work, a.source)
