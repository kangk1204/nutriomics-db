"""Durable engine observations and real, isolated recovery drills.

Health reads do not load NLP. A seven-day receipt contains actual samples only;
operational synthetic papers never enter a scientific evaluation or production DB.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psutil
import requests

from .db import connect, digest, dumps, initialize, now, uid
from .engines import default_engine, engine_rules_version, validate_engine
from .settings import PROJECT, Settings


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(dumps(value), encoding='utf-8')
    temporary.replace(path)


def register_artifact(settings, engine):
    """Explicit worker/CLI initialization; never used by API or health reads."""
    if engine == 'heuristic-v1':
        return 'legacy'
    from .nlp_v2 import _PARSER
    _PARSER.parse({'text': 'Vitamin D decreases VDR expression.', 'start': 0, 'end': 35})
    manifest = _PARSER.manifest
    _atomic(settings.home / 'engine-artifacts' / (engine + '.json'), manifest)
    return digest(dumps(manifest))


def _dictionary_version(settings, engine):
    """Verify frozen bytes without constructing the extraction dictionaries."""
    filename = 'entities.json' if engine == 'heuristic-v1' else 'entities-v2.json'
    entity_path = settings.config.parent / filename
    if not entity_path.exists():
        entity_path = PROJECT / 'config' / filename
    raw_entities = entity_path.read_bytes()
    config = json.loads(raw_entities)
    active = settings.dictionaries / 'hgnc-active.json'
    version = 'fallback-unverified-v1'
    if active.exists():
        manifest = json.loads(active.read_text(encoding='utf-8'))
        snapshot = settings.dictionaries / manifest['path']
        if snapshot.parent.resolve() != settings.dictionaries.resolve():
            raise ValueError('HGNC snapshot path is outside the dictionary directory')
        if digest(snapshot.read_bytes()) != manifest['sha256']:
            raise ValueError('HGNC snapshot hash mismatch')
        version = manifest['version']
    return f"{version}|{config['version']}|entities-sha256:{digest(raw_entities)}"


def provenance(settings, engine):
    validate_engine(engine)
    parser = {}
    path = settings.home / 'engine-artifacts' / (engine + '.json')
    if path.exists():
        parser = json.loads(path.read_text(encoding='utf-8'))
    return {'engine_version': engine, 'rules_version': engine_rules_version(engine),
            'dictionary_version': _dictionary_version(settings, engine),
            'parser_version': '|'.join(str(parser.get(k, '')) for k in
                                       ['name', 'version', 'spacy_version', 'scispacy_version']),
            'model_sha256': parser.get('model_sha256')}


def worker_heartbeat(settings, phase='idle'):
    process = psutil.Process()
    value = {'at': now(), 'pid': process.pid, 'process_created': process.create_time(),
             'phase': phase, 'rss_bytes': process.memory_info().rss}
    with connect(settings) as con:
        value['profiles'] = [dict(row) for row in con.execute('SELECT * FROM engine_profiles WHERE enabled=1')]
    _atomic(settings.home / 'worker-extract-heartbeat.json', value)


def check_engines(settings, request=requests.get, clock=None):
    current = (clock or datetime.now(UTC)).replace(second=0, microsecond=0)
    with connect(settings) as con:
        profiles = [dict(r) for r in con.execute('SELECT * FROM engine_profiles WHERE enabled=1')]
    if not profiles:
        return {'status': 'not_registered', 'engines': []}
    services = {}
    for name, url in [('api', 'http://127.0.0.1:8010/healthz'), ('ui', 'http://127.0.0.1:8510/_stcore/health')]:
        try:
            services[name] = request(url, timeout=5).status_code == 200
        except requests.RequestException:
            services[name] = False
    try:
        heartbeat = json.loads((settings.home / 'worker-extract-heartbeat.json').read_text(encoding='utf-8'))
        process = psutil.Process(heartbeat['pid'])
        age = (current - datetime.fromisoformat(heartbeat['at'])).total_seconds()
        worker_ok = (process.is_running() and abs(process.create_time() - heartbeat['process_created']) < 0.01
                     and -60 <= age <= 900 and heartbeat.get('rss_bytes', 2**40) <= 8 * 1024**3)
    except (OSError, ValueError, KeyError, psutil.Error):
        heartbeat, worker_ok = {}, False
    observations = []
    for profile in profiles:
        engine = profile['engine_version']
        identity = provenance(settings, engine)
        with connect(settings) as con:
            queues = [dict(r) for r in con.execute('SELECT status,count(*) AS count FROM engine_queue '
                                                  'WHERE engine_version=? GROUP BY status', (engine,))]
        artifact_path = settings.home / 'engine-artifacts' / (engine + '.json')
        artifact_ok = engine == 'heuristic-v1' or (artifact_path.exists() and len(identity.get('model_sha256') or '') == 64
            and digest(dumps(json.loads(artifact_path.read_text(encoding='utf-8')))) == profile['artifact_version'])
        version_ok = all(profile[k] == identity[k] for k in ['rules_version', 'dictionary_version'])
        heartbeat_matches = any(p.get('engine_version') == engine and
                                all(p.get(k) == profile[k] for k in ['rules_version', 'dictionary_version', 'artifact_version'])
                                for p in heartbeat.get('profiles', []))
        failed_tasks = sum(q['count'] for q in queues if q['status'] == 'failed')
        ok = all(services.values()) and worker_ok and version_ok and artifact_ok and heartbeat_matches and failed_tasks == 0
        details = {'services': services, 'worker_alive': worker_ok, 'worker_phase': heartbeat.get('phase'),
                   'version_current': version_ok, 'artifact_current': artifact_ok,
                   'heartbeat_profile_current': heartbeat_matches, 'queue': queues,
                   'failed_tasks': failed_tasks, 'default_engine': default_engine(settings)}
        sample = {'at': current.isoformat(), 'engine': engine, 'ok': bool(ok), 'provenance': identity, 'detail': details}
        sha = digest(dumps(sample))
        with connect(settings, durable=True) as con:
            con.execute('INSERT OR IGNORE INTO engine_health VALUES(?,?,?,?,?,?)',
                        (sample['at'], engine, int(ok), dumps(identity), dumps(details), sha))
        observations.append({**sample, 'snapshot_sha256': sha})
    return {'status': 'observing' if all(o['ok'] for o in observations) else 'failed_observation', 'engines': observations}


def soak_receipt(settings, engine='heuristic-v2'):
    from .benchmarks import _receipt
    identity = provenance(settings, engine)
    with connect(settings) as con:
        history = [dict(r) for r in con.execute('SELECT * FROM engine_health WHERE engine_version=? ORDER BY observed_at', (engine,))]
        drills = {r['kind']: json.loads(r['receipt_json']) for r in con.execute('SELECT * FROM engine_drills WHERE engine_version=?', (engine,))}
    # A failed observation, changed artifact or missing interval restarts the
    # contiguous window, while the complete history remains in SQLite.
    window = []
    for row in history:
        same = json.loads(row['provenance']) == identity
        gap = ((datetime.fromisoformat(row['observed_at']) - datetime.fromisoformat(window[-1]['observed_at'])).total_seconds()
               if window else 0)
        if not same or not row['ok'] or gap > 900:
            window = []
        if same and row['ok']:
            window.append(row)
    cutoff = datetime.now(UTC) - timedelta(days=7)
    anchors = [i for i, r in enumerate(window) if datetime.fromisoformat(r['observed_at']) <= cutoff]
    if anchors:
        window = window[anchors[-1]:]
    samples = [{'at': r['observed_at'], 'ok': bool(r['ok']), **identity,
                'source': 'durable_health_snapshot', 'snapshot_sha256': r['snapshot_sha256']} for r in window]
    checks = {name: {'passed': value.get('passed') is True and value.get('provenance') == identity,
                     'receipt_sha256': value.get('receipt_sha256'), 'id': value.get('id')}
              for name, value in drills.items()}
    record = {'schema_version': 'literature-engine-soak-v1', 'id': uid(), 'engine': engine, 'provenance': identity,
              'started_at': samples[0]['at'] if samples else None,
              'completed_at': samples[-1]['at'] if samples else None, 'samples': samples, 'checks': checks,
              'history_samples': len(history), 'historical_failed_samples': sum(not r['ok'] for r in history)}
    record['receipt_sha256'] = digest(dumps(record))
    path = settings.home / 'operations' / ('soak-' + record['id'] + '.json')
    _atomic(path, record)
    _receipt(settings, 'engine_soak_receipt', record['id'], record)
    return {**record, 'output_path': str(path)}


def engine_status(settings, engine='heuristic-v2', clock=None):
    """Read stored engine observations only; never synthesize uptime or run NLP."""
    current=clock or datetime.now(UTC)
    identity=provenance(settings,engine)
    with connect(settings) as con:
        rows=[dict(r) for r in con.execute('SELECT * FROM engine_health WHERE engine_version=? ORDER BY observed_at',(engine,))]
        checks={r['kind']:json.loads(r['receipt_json']) for r in con.execute('SELECT * FROM engine_drills WHERE engine_version=?',(engine,))}
    window=[]
    for row in rows:
        same=json.loads(row['provenance'])==identity
        gap=((datetime.fromisoformat(row['observed_at'])-datetime.fromisoformat(window[-1]['observed_at'])).total_seconds() if window else 0)
        if not same or not row['ok'] or gap>900:
            window=[]
        if same and row['ok']:
            window.append(row)
    elapsed=((datetime.fromisoformat(window[-1]['observed_at'])-datetime.fromisoformat(window[0]['observed_at'])).total_seconds()/3600 if window else 0)
    age=((current-datetime.fromisoformat(rows[-1]['observed_at'])).total_seconds() if rows else None)
    fresh=age is not None and -60<=age<=900
    drills={name:bool(checks.get(name,{}).get('passed') is True and checks[name].get('provenance')==identity)
            for name in ['resume','backup_restore','switch_rollback']}
    state=('pending' if not rows else 'failed' if not window or not fresh else
           'passed' if elapsed>=168 and all(drills.values()) else 'observing')
    return {'engine':engine,'status':state,'samples':len(window),'required_days':7,'elapsed_hours':round(elapsed,2),
            'first_observed_at':window[0]['observed_at'] if window else None,
            'last_observed_at':rows[-1]['observed_at'] if rows else None,'checks':drills,
            'historical_failed_samples':sum(not r['ok'] for r in rows),'provenance':identity,
            'latest_detail':json.loads(rows[-1]['detail']) if rows else {},
            'note':'엔진별 실제 관측입니다. 공개 성능 평가와 영양 전문가 검수는 별도입니다.'}


def _wait(predicate, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise TimeoutError('Operational drill deadline exceeded')


def _resume(settings, engine, directory):
    from .collection import save_article
    from .pipeline import register_engine
    isolated = Settings(directory / 'runtime', settings.config)
    initialize(isolated)
    if settings.dictionaries.exists():
        shutil.copytree(settings.dictionaries, isolated.dictionaries, dirs_exist_ok=True)
    text = 'We enrolled 30 adults. ' + 'DHA reduced blood pressure in adults. ' * 100
    article = {'pmid': 'operational-drill-' + uid(), 'doi': None, 'pmcid': None, 'title': 'Synthetic operational recovery drill',
               'pub_date': '2025', 'abstract': text, 'text': text, 'sections': [], 'publication_types': [], 'mesh': ['Humans'],
               'authors': [], 'retracted': False, 'corrections': [], 'element_xml': b'<operational-synthetic />'}
    with connect(isolated) as con:
        topic = con.execute('SELECT topic_id,query_version FROM topics WHERE enabled=1 LIMIT 1').fetchone()
    paper = save_article(isolated, article, topic['topic_id'], topic['query_version'])
    register_engine(isolated, engine, queue_all=True)
    env = {**os.environ, 'LITERATURE_HOME': str(isolated.home), 'LITERATURE_CONFIG': str(settings.config),
           'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1',
           'LITERATURE_NLP_LOCK': os.environ.get('LITERATURE_NLP_LOCK',str(settings.home/'locks'/'nlp-cpu.lock'))}
    command = [sys.executable, '-m', 'nutri_literature.cli', 'worker', '--stage', 'extract', '--engine', engine]
    def task_state(status):
        with connect(isolated) as con:
            row = con.execute('SELECT * FROM engine_queue WHERE paper_id=? AND engine_version=? AND status=?', (paper, engine, status)).fetchone()
            return dict(row) if row else None
    child = None
    with (directory / 'worker.log').open('w', encoding='utf-8') as log:
        try:
            child = subprocess.Popen(command, cwd=PROJECT, env=env, stdout=log, stderr=subprocess.STDOUT)
            first = _wait(lambda: task_state('running'))
            child.kill()
            child.wait(timeout=10)
            child = subprocess.Popen(command, cwd=PROJECT, env=env, stdout=log, stderr=subprocess.STDOUT)
            recovered = _wait(lambda: task_state('completed'), timeout=180)
            with connect(isolated) as con:
                source_hash = con.execute('SELECT s.text_hash FROM sources s JOIN papers p ON s.source_id=p.current_source_id WHERE p.paper_id=?', (paper,)).fetchone()[0]
                count = con.execute('SELECT count(*) FROM extractions WHERE paper_id=? AND active=1 AND engine_version=?', (paper, engine)).fetchone()[0]
            if recovered['attempts'] <= first['attempts'] or count != 1 or recovered['source_hash'] != source_hash:
                raise ValueError('Recovered task did not preserve version/source or exactly one active extraction')
            return {'isolated_runtime': str(isolated.home), 'killed_after_claim': True,
                    'first_attempts': first['attempts'], 'recovered_attempts': recovered['attempts'],
                    'active_engine_results': count, 'source_hash': source_hash,
                    'scope': 'actual child-process SIGKILL and restart; synthetic operational paper, no accuracy labels'}
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)


def _backup_restore(settings, directory):
    from .reporting import backup
    receipt = backup(settings)
    source = Path(receipt['path'])
    restored = directory / 'restored.sqlite3'
    shutil.copy2(source, restored)
    inventories = []
    for path in [source, restored]:
        con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or con.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('Restored database failed integrity/foreign-key validation')
            tables = ['papers', 'sources', 'extractions', 'assertions', 'reviews', 'engine_queue', 'engine_profiles']
            inventories.append({table: digest(dumps(con.execute('SELECT * FROM ' + table + ' ORDER BY rowid').fetchall())) for table in tables})
        finally:
            con.close()
    if inventories[0] != inventories[1] or digest(restored.read_bytes()) != receipt['sha256']:
        raise ValueError('Restored data/review/queue identity differs from online backup')
    return {'backup_sha256': receipt['sha256'], 'backup_path': str(source), 'restored_path': str(restored),
            'restored_integrity': 'ok', 'table_content_sha256': inventories[1]}


def _switch_rollback(settings, engine):
    with connect(settings) as con:
        row = con.execute("SELECT paper_id FROM extractions WHERE active=1 AND engine_version IN ('heuristic-v1','heuristic-v2') "
                          'GROUP BY paper_id HAVING count(DISTINCT engine_version)=2 ORDER BY paper_id LIMIT 1').fetchone()
    if not row:
        raise ValueError('Need one paper with current v1/v2 results for request-scoped switch/rollback drill')
    before = default_engine(settings)
    sequence = []
    for selected in ['heuristic-v1', engine, 'heuristic-v1']:
        response = requests.get(f'http://127.0.0.1:8010/api/v1/papers/{row[0]}', params={'engine': selected}, timeout=15)
        response.raise_for_status()
        value = response.json()
        extraction = next((e for e in value.get('extractions',[]) if e.get('current_valid')), {})
        if extraction.get('engine_version') != selected:
            raise ValueError('API engine routing failed')
        sequence.append({'engine': selected, 'extraction_id': extraction.get('extraction_id')})
    if sequence[0] != sequence[2] or default_engine(settings) != before:
        raise ValueError('v1 rollback did not preserve extraction/default state')
    from .engines import switch_engine
    blocked = False
    try:
        switch_engine(settings, 'heuristic-v2', {'passed': True})
    except ValueError:
        blocked = True
    if not blocked:
        raise ValueError('Unaudited promotion was accepted')
    return {'paper_id': row[0], 'routing_sequence': sequence, 'default_before': before,
            'default_after': default_engine(settings), 'unaudited_promotion_blocked': blocked,
            'scope': 'actual API engine selection and rollback; default promotion remains subject to public/7-day gates'}


def drill(settings, kind, engine='heuristic-v2'):
    from .benchmarks import _receipt
    validate_engine(engine)
    identifier = uid()
    directory = settings.home / 'operations' / ('drill-' + identifier)
    directory.mkdir(parents=True, exist_ok=False)
    record = {'schema_version': 'literature-engine-drill-v1', 'id': identifier, 'kind': kind,
              'engine': engine, 'provenance': provenance(settings, engine), 'started_at': now()}
    try:
        if kind == 'resume':
            detail = _resume(settings, engine, directory)
        elif kind == 'backup_restore':
            detail = _backup_restore(settings, directory)
        elif kind == 'switch_rollback':
            detail = _switch_rollback(settings, engine)
        else:
            raise ValueError('Unknown operational drill')
        record.update(passed=True, details=detail)
    except Exception as error:
        record.update(passed=False, details={'error_type': type(error).__name__, 'error': str(error)})
    record['completed_at'] = now()
    record['receipt_sha256'] = digest(dumps(record))
    path = directory / 'receipt.json'
    _atomic(path, record)
    with connect(settings, durable=True) as con:
        con.execute('INSERT INTO engine_drills VALUES(?,?,?,?) ON CONFLICT(engine_version,kind) DO UPDATE SET '
                    'receipt_json=excluded.receipt_json,created_at=excluded.created_at', (engine, kind, dumps(record), now()))
    _receipt(settings, 'engine_drill', identifier, record)
    return {**record, 'output_path': str(path)}
