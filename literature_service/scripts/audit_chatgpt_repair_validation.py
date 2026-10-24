"""Preserve every parser/identity/evidence rejection after real inference completes."""
from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature.chatgpt_repair_io import HOME, MODELS
    from nutri_literature.chatgpt_repair_runner import paths
    from nutri_literature.chatgpt_repair_semantics import parse
    from nutri_literature.chatgpt_evaluation import _read
    from nutri_literature.contract_v7 import canonical, immutable, sha
    summary = {}
    for split, expected in [('train', 384), ('development', 100)]:
        for model, effort in MODELS:
            directory = paths(split, model)
            responses = sorted((directory / 'responses').glob('*.json'))
            if len(responses) != expected:
                raise ValueError('All independent responses must finish before validation audit')
            aggregate = Counter()
            for path in responses:
                payload = _read(HOME / 'payloads' / split / path.name)
                request, response = _read(directory / 'requests' / path.name), _read(path)
                if request['input'] != canonical(payload) or response['request_sha256'] != sha(canonical(request)):
                    raise ValueError('Saved source-only request does not match response')
                parsed = parse(payload, response['text'])
                tally = Counter(reason for d in parsed['decisions'] for reason in d['reasons'])
                tally['parse_failures'] += parsed['parse_failure']
                tally['validated_events'] += len(parsed['events'])
                tally['resolved_mentions'] += sum(m['id_valid'] for m in parsed['mentions'].values())
                tally['unresolved_mentions'] += sum(not m['id_valid'] for m in parsed['mentions'].values())
                tally['invalid_identity_quotes'] += sum(not m['id_quote_valid'] for m in parsed['mentions'].values())
                aggregate.update(tally)
                immutable(HOME / 'validation' / split / (model+'-'+effort) / path.name, {
                    'response_sha256': sha(path.read_bytes()), 'payload_sha256': sha(canonical(payload)),
                    'counts': dict(tally), 'decisions': parsed['decisions'],
                    'parse_failure': parsed['parse_failure']})
            summary[split+':'+model] = {'papers': expected, 'counts': dict(aggregate)}
    result = {'status': 'complete', 'partitions': summary,
              'all_rejections_reconstructable_from_real_responses': True,
              'annotation_labels_read': False, 'source_sha256': sha(Path(__file__).read_bytes()),
              'interpretation': 'Source/parser validation counts, not scientific accuracy or independent annotation.'}
    immutable(HOME / 'validation-audit.json', result)
    print(result)


if __name__ == '__main__':
    main()
