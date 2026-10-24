"""Measured successful-request durations/tokens and original-budget accounting."""
from collections import defaultdict
from pathlib import Path
import math
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature.chatgpt_repair_io import HOME, AttemptBudget
    from nutri_literature.chatgpt_evaluation import _read
    from nutri_literature.contract_v7 import immutable, sha
    grouped = defaultdict(list)
    for phase in ('reference', 'correction'):
        for path in (HOME / phase).glob('**/responses/*.json'):
            row = _read(path)
            if row.get('test_only') or row.get('status') != 'completed':
                raise ValueError('Only real completed model responses are measured')
            grouped[str(path.parent.parent.relative_to(HOME))].append(row)
    if sum(map(len, grouped.values())) != 2504:
        raise ValueError('Expected1536 reference plus968 independent responses')
    rows = []
    new = reused = 0
    for group, values in sorted(grouped.items()):
        durations = sorted(v['elapsed_seconds'] for v in values)
        tokens = [v.get('usage') or {} for v in values]
        r = sum(bool(v.get('reused_from')) for v in values)
        new += len(values)-r
        reused += r
        rows.append({'group': group, 'completed': len(values), 'reused': r,
                     'actual_models': sorted({v['actual_model'] for v in values}),
                     'mean_request_seconds': statistics.mean(durations),
                     'median_request_seconds': statistics.median(durations),
                     'p95_request_seconds': durations[math.ceil(.95*len(durations))-1],
                     'input_tokens': sum(t.get('input_tokens', 0) for t in tokens),
                     'output_tokens_including_reasoning': sum(t.get('output_tokens', 0) for t in tokens),
                     'reasoning_tokens': sum((t.get('output_tokens_details') or {}).get('reasoning_tokens', 0) for t in tokens)})
    budget = AttemptBudget().status()
    if budget['states'].get('started') or budget['states'].get('completed', 0) != new:
        raise ValueError('Durable completed-attempt ledger and new response counts differ')
    result = {'groups': rows, 'new_completed_requests': new, 'reused_completed_requests': reused,
              'attempts_including_failures': budget, 'normal_new_paper_model_requests': 6,
              'previous_joint_model_requests': 4, 'request_time_is_not_end_to_end_latency': True,
              'currency_cost_and_account_remaining_usage': 'not inferred',
              'source_sha256': sha(Path(__file__).read_bytes())}
    immutable(HOME / 'request-performance.json', result)
    print({'new_completed': new, 'reused_completed': reused, 'budget': budget, 'groups': rows})


if __name__ == '__main__':
    main()
