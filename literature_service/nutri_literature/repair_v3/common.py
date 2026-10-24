"""Immutable experiment artifacts and unchanged native evaluation."""
import json
from pathlib import Path
import sqlite3

from ..contract_v7 import canonical, immutable, sha, TYPES, C_GRID, THRESHOLDS
from ..chatgpt_evaluation import _read

ROOT = Path(__file__).resolve().parents[2]
PRIOR = ROOT / 'runtime/chatgpt-repair-v2'
HOME = ROOT / 'runtime/chatgpt-repair-v3'
FLOOR = 64 / 154
SEED = 20261017
ARMS = ('baseline', 'identity_only', 'events_only', 'combined')


def write(relative, value):
    return immutable(HOME / relative, value)


def log(stage, **fields):
    print(json.dumps({'stage': stage, **fields}, ensure_ascii=False), flush=True)


def budget_status():
    path = PRIOR / 'attempts.sqlite3'
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        states = dict(db.execute('SELECT state,count(*) FROM attempts GROUP BY state'))
    return {'attempts': sum(states.values()), 'limit': 3000,
            'remaining': max(0, 3000-sum(states.values())), 'states': states}


def data(split):
    name = 'train.json' if split == 'train' else 'development-sources.json'
    documents = _read(PRIOR / 'inputs' / name)['documents']
    if split != 'train' and any('entities' in d or 'relations' in d for d in documents):
        raise ValueError('개발 예측 입력에 정답이 포함됨')
    return documents


def old_inputs(document, split):
    # Existing provenance validation, without the v2 prepare routine (which writes).
    from ..chatgpt_repair_runner import inputs_for
    return inputs_for(document, split)


def raw_reviews(pmid, split):
    result = {}
    for model, effort in (('gpt-6.1-sol', 'medium'), ('gpt-6-luna', 'max')):
        path = PRIOR / 'correction' / split / (model+'-'+effort) / 'responses' / ('paper-'+pmid+'.json')
        value = _read(path)
        try:
            raw = json.loads(value['text'])
            if not isinstance(raw, dict) or not isinstance(raw.get('mentions'), list) or not isinstance(raw.get('events'), list):
                raise ValueError('schema')
            failed = False
        except (ValueError, TypeError):
            raw, failed = {'mentions': [], 'events': []}, True
        result[model] = {'raw': raw, 'response_sha256': sha(path.read_bytes()), 'parse_failure': failed}
    return result


def baseline(pmid, split):
    if split == 'development':
        return _read(PRIOR / 'predictions/combined' / ('paper-'+pmid+'.json'))
    # Frozen v2 corrector is a comparator, never a new training target.
    from ..chatgpt_repair_learning import make_rows, project_rows, priority
    d = {'pmid': pmid}
    payload, parsed, previous = old_inputs(d, split)
    selection = _read(PRIOR / 'selection.json')
    rows = make_rows(payload, parsed, previous)
    return project_rows(rows, [priority(r, selection['model']) for r in rows],
                        selection['selected']['threshold'])


def source_manifest():
    paths = [*sorted(Path(__file__).parent.glob('*.py')),
             ROOT/'scripts/run_chatgpt_repair_v3.py', ROOT/'tests/test_repair_v3.py']
    return {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in paths}


def verify_source_freeze():
    frozen = _read(HOME / 'freeze.json')
    if frozen['implementation_sha256'] != source_manifest():
        raise ValueError('동결 후 v3 소스 변경: 새 실험 버전 필요')
    from .. import benchmarks
    if frozen['evaluator_sha256'] != benchmarks.evaluator_source_hashes():
        raise ValueError('동결 평가기 변경')
    for name, digest in frozen['artifacts_sha256'].items():
        if sha((HOME/name).read_bytes()) != digest:
            raise ValueError('동결 산출물 변경: '+name)
    return frozen


def native_metrics(documents, predictions):
    from ..chatgpt_repair_io import score
    from ..chatgpt_evaluation import _metric
    per_paper = score(documents, predictions)
    return {'metrics': _metric([v['relation'] for v in per_paper.values()]),
            'direction': _metric([v['relation_direction'] for v in per_paper.values()]),
            'per_paper': per_paper}
