"""Isolated repair experiment, immutable receipts and a durable attempt budget."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import time

from .contract_v7 import canonical, immutable, sha
from . import chatgpt_tuning as old, benchmarks, evaluation_v6
from .chatgpt_evaluation import _read
from .splits_v7 import text_key

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / 'runtime/chatgpt-repair-v2'
MAX_ATTEMPTS = 3000
MODELS = old.MODELS


def log(stage, **values):
    print(json.dumps({'stage': stage, **values}, ensure_ascii=False), flush=True)


def prepare_data():
    """Only train annotations are read here; dev inputs are source-only."""
    src = old.HOME / 'inputs'
    audit = _read(src / 'training-audit.json')
    official = _read(src / 'official-bioredirect-train.json')
    training = _read(src / 'relation-train.json')
    sources = _read(src / 'development-sources.json')['documents']
    if not audit['passed'] or audit['protected_overlap'] or official['split'] != 'train':
        raise ValueError('Official training provenance failed')
    if sha((src / 'relation-train.json').read_bytes()) != audit['audited_training_sha256']:
        raise ValueError('Audited entity training bytes changed')
    protected = set(audit['protected_pmids']) | {d['pmid'] for d in sources}
    texts = set(audit['protected_text_hashes']) | {text_key(d['text']) for d in sources}
    pmcs = set(audit['protected_pmcids'])
    keep, excluded = [], []
    for d in official['documents']:
        reasons = [name for name, hit in [('pmid', d['pmid'] in protected),
                   ('normalized_text', text_key(d['text']) in texts),
                   ('pmcid', bool(d.get('pmcid') and d['pmcid'] in pmcs))] if hit]
        if reasons:
            excluded.append({'pmid': d['pmid'], 'reasons': reasons})
        else:
            keep.append(d)
    n = sum(len(evaluation_v6.relation_sets(d, {'relations': []})[0]) for d in keep)
    if len(keep) != 384 or n != 549 or len(sources) != 100:
        raise ValueError('Expected384 training papers/549 relations and100 dev sources')
    immutable(HOME / 'inputs/train.json', {'documents': keep, 'split': 'train'})
    immutable(HOME / 'inputs/development-sources.json', {'documents': sources, 'labels_read': False})
    immutable(HOME / 'data-audit.json', {
        'training_papers': 384, 'training_relations': n, 'excluded': excluded,
        'entity_training_papers': len(training['documents']), 'protected_overlap': 0,
        'pmcid_missing_training': sum(not d.get('pmcid') for d in keep),
        'pmcid_check': 'available only; missing unknown', 'pretraining_overlap': 'unknown',
        'fixed_upstream_identity_training_exposure': 'prior model used these training papers; conditional evaluation, not independent',
        'official_train_sha256': sha((src / 'official-bioredirect-train.json').read_bytes()),
        'prior_audit_sha256': sha((src / 'training-audit.json').read_bytes()),
        'original_development_report_sha256': sha((old.HOME / 'joint-verifier/comparison.json').read_bytes()),
        'sealed_test_opened': False, 'nutrition_accuracy': 'unmeasured'})
    return keep, sources


class BudgetExceeded(RuntimeError):
    pass


class AttemptBudget:
    """An attempt is reserved durably BEFORE an inference HTTP call; crashes count."""
    def __init__(self, path=None, limit=MAX_ATTEMPTS):
        self.path = Path(path or HOME / 'attempts.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.limit = limit
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, request_hash TEXT NOT NULL, '
                       'phase TEXT NOT NULL, model TEXT NOT NULL, state TEXT NOT NULL, '
                       'started TEXT NOT NULL, finished TEXT, response_id TEXT, error_class TEXT)')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute('PRAGMA journal_mode=WAL')
        return db

    def reserve(self, fingerprint, phase, model):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM attempts').fetchone()[0] >= self.limit:
                raise BudgetExceeded('The3000-attempt inference budget is exhausted')
            cursor = db.execute('INSERT INTO attempts(request_hash,phase,model,state,started) VALUES(?,?,?,?,?)',
                                (fingerprint, phase, model, 'started', datetime.now(timezone.utc).isoformat()))
            return cursor.lastrowid

    def finish(self, identifier, response=None, error=None):
        with self.connect() as db:
            db.execute('UPDATE attempts SET state=?,finished=?,response_id=?,error_class=? WHERE id=?',
                       ('completed' if response else 'failed', datetime.now(timezone.utc).isoformat(),
                        response.get('response_id') if response else None,
                        type(error).__name__ if error else None, identifier))

    def status(self):
        with self.connect() as db:
            rows = dict(db.execute('SELECT state,count(*) FROM attempts GROUP BY state'))
        return {'attempts': sum(rows.values()), 'limit': self.limit, 'states': rows}


def inference(jobs, phase, contract, auth, budget=None):
    """Four network slots total. No automatic retries, no silent model switch."""
    from .chatgpt_client import ChatGPTPlanClient
    budget = budget or AttemptBudget()
    profile = auth.status()['active_registration_id']
    if not profile:
        raise ValueError('The authorized account is unavailable')
    profile_sha = sha(profile)
    stop = threading.Event()
    def execute(job):
        if stop.is_set():
            return
        path, model, effort, prompt, source, extra, reuse = job
        request = {'model': model, 'reasoning_effort': effort, 'instructions': prompt, 'input': source,
                   'contract_sha256': sha(canonical(contract)), **extra}
        fingerprint = sha(canonical(request))
        immutable(path.parent.parent / 'requests' / path.name, request)
        if path.exists():
            saved = _read(path)
            if (saved['request_sha256'] != fingerprint or saved.get('status') != 'completed'
                    or saved.get('actual_model') != model or saved.get('test_only')
                    or saved.get('account_profile_sha256') != profile_sha):
                raise ValueError('Immutable response or selected account differs')
            return
        for prior_path, prior_request in reuse:
            if not prior_path.exists() or not prior_request.exists():
                continue
            value, previous = _read(prior_path), _read(prior_request)
            if (previous.get('instructions') == prompt and previous.get('input') == source
                    and value.get('actual_model') == model and value.get('reasoning_effort') == effort
                    and value.get('account_profile_sha256') == profile_sha
                    and value.get('status') == 'completed' and not value.get('test_only')
                    and value.get('request_sha256') == sha(canonical(previous))):
                immutable(path, {**value, 'request_sha256': fingerprint,
                                'reused_from': str(prior_path.relative_to(ROOT)),
                                'reused_response_sha256': sha(prior_path.read_bytes())})
                return
        attempt = budget.reserve(fingerprint, phase, model)
        try:
            client = ChatGPTPlanClient(auth, model, profile, reasoning_effort=effort)
            response = client(prompt, source)
            if (response.get('status') != 'completed' or response.get('actual_model') != model
                    or not isinstance(response.get('response_id'), str)):
                raise ValueError('Actual completed model response required')
            response.update(request_sha256=fingerprint, attempt_id=attempt)
            immutable(path, response)
            budget.finish(attempt, response=response)
        except Exception as error:
            budget.finish(attempt, error=error)
            stop.set()
            raise
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(execute, job) for job in jobs]
        for i, future in enumerate(as_completed(futures), 1):
            try:
                future.result()
            except Exception:
                stop.set()
                for pending in futures:
                    pending.cancel()
                raise
            if i % 20 == 0 or i == len(jobs):
                log(phase, completed=i, total=len(jobs), elapsed_seconds=round(time.monotonic()-start), **budget.status())


def score(documents, predictions):
    return old.score_rows(documents, predictions)


def phase_contract(name, source_files, extra=None):
    contract = {'schema': 'chatgpt-repair-v2-' + name, 'models': MODELS,
                'implementation': {p.name: sha(p.read_bytes()) for p in source_files},
                'scorer_sha256': benchmarks.evaluator_source_hashes(),
                'data_audit_sha256': sha((HOME / 'data-audit.json').read_bytes()),
                'attempt_limit': MAX_ATTEMPTS, 'goals': {'precision': .85, 'minimum_tp': 56, 'recall': 56/154},
                'default_engine': 'heuristic-v1', 'sealed_test_opened': False, **(extra or {})}
    immutable(HOME / (name + '-contract.json'), contract)
    return contract
