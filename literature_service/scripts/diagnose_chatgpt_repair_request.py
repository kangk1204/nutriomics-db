"""One counted retry with error-code-only SSE diagnostics; no headers/bodies logged."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import requests
    from nutri_literature.chatgpt_auth import ChatGPTAuth
    from nutri_literature.chatgpt_client import ChatGPTPlanClient
    from nutri_literature.chatgpt_batch import batch_lock
    from nutri_literature.chatgpt_repair_io import AttemptBudget, HOME
    from nutri_literature.chatgpt_evaluation import _read
    from nutri_literature.contract_v7 import canonical, immutable, sha
    path = Path(sys.argv[1]).resolve()
    request = _read(path)
    target = path.parent.parent / 'responses' / path.name
    if target.exists():
        raise ValueError('A completed response already exists')
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
                                error = event.get('response', {}).get('error') or event.get('error') or {}
                                code = error.get('code') or event.get('code')
                                allowed = {'subscription_sharing_usage_limit_exceeded', 'subscription_sharing_usage_unavailable',
                                           'rate_limit_exceeded', 'server_error', 'model_not_found', 'insufficient_quota',
                                           'context_length_exceeded', 'invalid_request_error'}
                                metadata.append({'event': event['type'], 'code': code if code in allowed else 'other_provider_error'})
                        except (ValueError, TypeError):
                            pass
                    yield line
            response.iter_lines = lines
            return response
    with batch_lock(ROOT / 'runtime/chatgpt-comparison'):
        budget = AttemptBudget()
        fingerprint = sha(canonical(request))
        attempt = budget.reserve(fingerprint, 'reference-proposals', request['model'])
        auth = ChatGPTAuth()
        try:
            result = ChatGPTPlanClient(auth, request['model'], auth.status()['active_registration_id'],
                         reasoning_effort=request['reasoning_effort'], session=SafeSession())(request['instructions'], request['input'])
            if result['status'] != 'completed' or result['actual_model'] != request['model']:
                raise ValueError('Unexpected model completion')
            result.update(request_sha256=fingerprint, attempt_id=attempt)
            immutable(target, result)
            budget.finish(attempt, response=result)
            outcome = 'completed'
        except Exception as error:
            budget.finish(attempt, error=error)
            outcome = type(error).__name__
        receipt = {'attempt': attempt, 'outcome': outcome, 'safe_transport_metadata': metadata,
                   'request_sha256': fingerprint, 'source_sha256': sha(Path(__file__).read_bytes())}
        immutable(HOME / 'diagnostics' / ('attempt-'+str(attempt)+'.json'), receipt)
        print(json.dumps(receipt))


if __name__ == '__main__':
    main()
