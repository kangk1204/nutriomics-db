from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest

from nutri_literature.chatgpt_repair_io import AttemptBudget, BudgetExceeded, inference
from nutri_literature.contract_v7 import immutable, canonical, sha


def test_attempt_budget_is_atomic_and_crashed_reservations_count(tmp_path):
    budget = AttemptBudget(tmp_path / 'attempts.sqlite3', limit=9)
    def reserve(i):
        try:
            return budget.reserve(str(i), 'test', 'model')
        except BudgetExceeded:
            return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        values = list(pool.map(reserve, range(25)))
    assert len([v for v in values if v]) == 9
    assert budget.status() == {'attempts': 9, 'limit': 9, 'states': {'started': 9}}
    budget.finish(next(v for v in values if v), error=ValueError())
    with pytest.raises(BudgetExceeded):
        budget.reserve('next', 'test', 'model')


def test_reuse_requires_exact_prompt_input_model_and_real_completed_response(tmp_path, monkeypatch):
    import nutri_literature.chatgpt_client as client
    def forbidden(*args, **kwargs):
        raise AssertionError('No HTTP should occur when an identical completed response exists')
    monkeypatch.setattr(client, 'ChatGPTPlanClient', forbidden)
    auth = SimpleNamespace(status=lambda: {'active_registration_id': 'authorized'})
    request = {'model': 'model', 'instructions': 'prompt', 'input': 'source'}
    previous = tmp_path / 'previous.json'
    previous_request = tmp_path / 'previous-request.json'
    immutable(previous_request, request)
    immutable(previous, {'text': '{}', 'status': 'completed', 'actual_model': 'model',
                         'reasoning_effort': 'medium', 'response_id': 'real-response',
                         'request_sha256': sha(canonical(request)), 'account_profile_sha256': sha('authorized')})
    monkeypatch.setattr('nutri_literature.chatgpt_repair_io.ROOT', tmp_path)
    target = tmp_path / 'responses/paper-1.json'
    budget = AttemptBudget(tmp_path / 'attempts.sqlite3')
    inference([(target, 'model', 'medium', 'prompt', 'source', {}, [(previous, previous_request)])],
              'test', {}, auth, budget)
    assert target.exists() and budget.status()['attempts'] == 0
    with pytest.raises(ValueError):
        inference([(target, 'model', 'medium', 'changed', 'source', {}, [])], 'test', {}, auth, budget)


def test_failure_is_counted_and_no_error_body_is_persisted(tmp_path, monkeypatch):
    import nutri_literature.chatgpt_client as client
    def factory(*args, **kwargs):
        def fail(*args):
            raise RuntimeError('private response body')
        return fail
    monkeypatch.setattr(client, 'ChatGPTPlanClient', factory)
    auth = SimpleNamespace(status=lambda: {'active_registration_id': 'authorized'})
    budget = AttemptBudget(tmp_path / 'attempts.sqlite3')
    target = tmp_path / 'responses/paper-1.json'
    with pytest.raises(RuntimeError):
        inference([(target, 'model', 'medium', 'prompt', 'source', {}, [])], 'test', {}, auth, budget)
    assert not target.exists()
    assert budget.status()['states'] == {'failed': 1}
    with budget.connect() as db:
        assert db.execute('SELECT error_class FROM attempts').fetchone()[0] == 'RuntimeError'
