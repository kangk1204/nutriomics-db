"""Finish this one batch, recovering exact failed requests within its original budget.

No scheduler, account switch, prompt rewrite or scientific-source mutation.
"""
from pathlib import Path
import json
import os
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def active_other_workers():
    import psutil
    result = []
    for p in psutil.process_iter(['name', 'cmdline']):
        try:
            args = ' '.join(p.info['cmdline'] or [])
            if (p.pid != os.getpid() and (p.info['name'] or '').lower().startswith('python')
                    and any(s in args for s in ('scripts/run_chatgpt_repair_v2.py', 'nutri_literature.chatgpt_repair_baseline'))):
                result.append(p.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return result


def retry_failed_request(path):
    import requests
    from nutri_literature.chatgpt_auth import ChatGPTAuth
    from nutri_literature.chatgpt_client import ChatGPTPlanClient
    from nutri_literature.chatgpt_batch import batch_lock
    from nutri_literature.chatgpt_repair_io import AttemptBudget, HOME
    from nutri_literature.chatgpt_evaluation import _read
    from nutri_literature.contract_v7 import immutable, canonical, sha
    request = _read(path)
    target = path.parent.parent / 'responses' / path.name
    metadata = []
    class SafeSession(requests.Session):
        def post(self, *args, **kwargs):
            response = super().post(*args, **kwargs)
            metadata.append({'http_status': response.status_code})
            previous = response.iter_lines
            def lines(*args, **kwargs):
                for line in previous(*args, **kwargs):
                    value = line.decode() if isinstance(line, bytes) else line
                    if value and value.startswith('data:'):
                        try:
                            event = json.loads(value[5:].lstrip())
                            if event.get('type') in {'error', 'response.failed', 'response.incomplete'}:
                                e = event.get('response', {}).get('error') or event.get('error') or {}
                                code = e.get('code') or event.get('code')
                                allowed = ('subscription_sharing_usage_limit_exceeded', 'subscription_sharing_usage_unavailable',
                                           'rate_limit_exceeded', 'server_error', 'model_not_found', 'insufficient_quota',
                                           'context_length_exceeded', 'invalid_request_error')
                                metadata.append({'event': event['type'], 'code': code if code in allowed else 'other_provider_error'})
                        except (ValueError, TypeError):
                            pass
                    yield line
            response.iter_lines = lines
            return response
    phase = ('independent-train' if 'correction/train' in path.as_posix() else
             'independent-development' if 'correction/development' in path.as_posix() else
             'reference-review' if '/review/' in path.as_posix() else 'reference-proposals')
    with batch_lock(ROOT / 'runtime/chatgpt-comparison'):
        budget = AttemptBudget()
        attempt = budget.reserve(sha(canonical(request)), phase, request['model'])
        auth = ChatGPTAuth()
        try:
            value = ChatGPTPlanClient(auth, request['model'], auth.status()['active_registration_id'],
                     reasoning_effort=request['reasoning_effort'], session=SafeSession())(request['instructions'], request['input'])
            if value['status'] != 'completed' or value['actual_model'] != request['model']:
                raise ValueError('A real matching model completion is required')
            value.update(request_sha256=sha(canonical(request)), attempt_id=attempt)
            immutable(target, value)
            budget.finish(attempt, response=value)
            success = True
        except Exception as error:
            budget.finish(attempt, error=error)
            success = False
        receipt = {'attempt': attempt, 'completed': success, 'safe_metadata': metadata,
                   'request_sha256': sha(canonical(request)), 'source_sha256': sha(Path(__file__).read_bytes())}
        immutable(HOME / 'diagnostics' / ('recovery-'+str(attempt)+'.json'), receipt)
        print(json.dumps({'stage': 'counted-recovery', **receipt}), flush=True)
        return success


def main():
    from nutri_literature.chatgpt_repair_io import HOME, AttemptBudget
    from nutri_literature.chatgpt_evaluation import _read
    from nutri_literature.contract_v7 import sha, canonical
    while active_other_workers():
        time.sleep(10)
    budget = AttemptBudget()
    while not (HOME / 'completion-audit.json').exists():
        if budget.status()['attempts'] >= 3000:
            print('HALT: original3000-attempt budget reached', flush=True)
            return
        # Only once every prior worker is gone may crashed reservations be closed.
        with budget.connect() as db:
            db.execute("UPDATE attempts SET state='failed',error_class='AbandonedProcess' WHERE state='started'")
            failed = {r[0] for r in db.execute("SELECT request_hash FROM attempts WHERE state='failed'")}
        pending = []
        for phase in ('reference', 'correction'):
            for path in (HOME / phase).glob('**/requests/*.json'):
                target = path.parent.parent / 'responses' / path.name
                if not target.exists() and sha(canonical(_read(path))) in failed:
                    pending.append(path)
        for path in pending:
            if not retry_failed_request(path):
                print('HALT: counted diagnostic retry did not complete; do not bypass account/model/budget', flush=True)
                return
        env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'OMP_NUM_THREADS': '1',
               'MKL_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1'}
        process = subprocess.run([sys.executable, str(ROOT/'scripts/run_chatgpt_repair_v2.py'), 'resume'],
                     cwd=ROOT, env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if process.returncode and not pending:
            with budget.connect() as db:
                active_failures = {r[0] for r in db.execute("SELECT request_hash FROM attempts WHERE state='failed'")}
            if active_failures == failed:
                print('HALT: non-provider failure requires inspection', flush=True)
                return
    print('COMPLETE: isolated batch and offline audit finished', flush=True)


if __name__ == '__main__':
    main()
