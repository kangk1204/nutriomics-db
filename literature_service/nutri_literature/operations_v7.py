"""Real resource gates and observations; no artificial seven-day receipt."""
import json
from pathlib import Path
import shutil
import os
import tempfile
from datetime import datetime, timezone
from .contract_v7 import POLICY, sha, immutable


def resource_check(path):
    import psutil
    p = Path(path)
    while not p.exists():
        p = p.parent
    record = {'available_memory': psutil.virtual_memory().available, 'free_disk': shutil.disk_usage(p).free}
    if record['available_memory'] < POLICY['minimum_available_gib'] * 1024**3:
        raise MemoryError('V7 resource wait: available memory below 16 GiB')
    if record['free_disk'] < POLICY['minimum_free_gib'] * 1024**3:
        raise OSError('V7 resource wait: free disk below 50 GiB')
    # A free-space reading can look healthy on an emergency read-only mount.
    with tempfile.TemporaryFile(dir=p) as stream:
        stream.write(b'v7-storage-probe');stream.flush();os.fsync(stream.fileno())
    record['durable_write_probe']=True
    return record


def observe(home):
    record = {'at': datetime.now(timezone.utc).isoformat(), 'resources': resource_check(home),
              'kind': 'resource_observation_only', 'soak_completed': False}
    path = Path(home) / 'v7/observations' / (sha(record['at']) + '.json')
    immutable(path, record)
    return record


def reports(home):
    result = []
    for p in sorted((Path(home) / 'v7').rglob('report.json')):
        r = json.loads(p.read_bytes())
        result.append({k: r.get(k) for k in ('id', 'engine', 'mode', 'metrics', 'papers', 'scientific_targets_met')})
    states={}
    for name in ('baseline','identity','normalization-r2','normalization-r3','joint'):
        path=Path(home)/'v7'/(name+'-state.json')
        if path.exists():states[name]=json.loads(path.read_bytes())
    dependency=Path(home)/'v7/joint-prerequisite.json'
    if states.get('joint',{}).get('stage')=='nested' and states['joint'].get('status')=='running' and dependency.exists():
        required=Path(home)/json.loads(dependency.read_bytes())['state']
        status=json.loads(required.read_bytes()) if required.exists() else {}
        if status.get('status')!='complete':
            states['joint']={**states['joint'],'effective_status':'waiting_dependency',
                             'dependency':str(required),'dependency_stage':status.get('stage')}
    diagnostics=[]
    for path in sorted((Path(home)/'v7/validation').glob('*-diagnostics.json')):
        value=json.loads(path.read_bytes())
        diagnostics.append({k:value.get(k) for k in ('mode','papers','first_failure_counts','false_positive_counts','candidate_coverage')})
    return {'engine': 'heuristic-v7', 'items': result, 'training_states':states,'diagnostics':diagnostics,
            'default_engine': 'heuristic-v1', 'nutrition_accuracy': 'unmeasured'}


def operation_status(settings):
    return {'engine':'heuristic-v7','soak_completed':False,'seven_day_observation':'not_started',
            'default_engine':'heuristic-v1','training_states':reports(settings.home)['training_states']}


def process(settings,head,upstream,limit=10,identity_path=None):
    import os
    from .db import initialize,connect,now
    from .operations_v6 import components,processing_slot,identity
    from .resources_v6 import memory_budget
    from .dictionaries import load_dictionaries
    from .extraction_v7 import load_head
    from .registry_v7 import extract_engine
    from .pipeline import store_extraction
    from .contract_v7 import canonical
    resource_check(settings.home)
    initialize(settings)
    os.environ['LITERATURE_V6_MODEL_HOME']=str(Path(upstream).resolve())
    with processing_slot(settings),memory_budget():
        values=components(settings,'union')
        if identity_path is not None:
            from .identity_artifact_v7 import load_identity
            values['_v7_identity']=load_identity(identity_path,values)
        dictionaries=load_dictionaries(settings,'heuristic-v2')
        base=identity(settings,values,dictionaries)
        values.update(_v7_model_path=Path(head),_v7_loaded_head=load_head(head),_artifact_identity=base['artifact_identity'])
        with connect(settings) as con:
            rows=[dict(r) for r in con.execute('SELECT p.paper_id,s.* FROM papers p JOIN sources s ON s.source_id=p.current_source_id '
                                              'WHERE p.retracted=0 ORDER BY p.paper_id LIMIT ?',(limit,))]
        result={'engine':'heuristic-v7','processed':0,'failed':0,'default_changed':False,'errors':[]}
        for source in rows:
            try:
                extraction=extract_engine(source['text_content'],json.loads(source['sections']),dictionaries,values,'heuristic-v7')
                artifact=settings.home/'engine-artifacts/heuristic-v7.json'
                artifact.parent.mkdir(exist_ok=True)
                temporary=artifact.with_suffix('.tmp');temporary.write_text(canonical(extraction['manifest']));temporary.replace(artifact)
                with connect(settings) as con:
                    con.execute('INSERT INTO engine_profiles(engine_version,rules_version,dictionary_version,enabled,updated_at,artifact_version) '
                       'VALUES(?,?,?,1,?,?) ON CONFLICT(engine_version) DO UPDATE SET rules_version=excluded.rules_version,'
                       'dictionary_version=excluded.dictionary_version,artifact_version=excluded.artifact_version,updated_at=excluded.updated_at',
                       ('heuristic-v7',extraction['rules_version'],extraction['dictionary_version'],now(),extraction['manifest']['artifact_identity']))
                store_extraction(settings,source['paper_id'],source,extraction)
                result['processed']+=1
            except Exception as error:
                result['failed']+=1;result['errors'].append({'paper_id':source['paper_id'],'error':str(error)})
        return result
