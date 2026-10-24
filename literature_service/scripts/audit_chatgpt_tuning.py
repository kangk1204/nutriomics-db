"""Reproduce completed results without network calls and audit every release."""
from pathlib import Path
import json
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature import chatgpt_tuning as tuning
    from nutri_literature.contract_v7 import canonical, immutable, sha
    from nutri_literature.splits_v7 import text_key
    home, prior = tuning.HOME, tuning.PRIOR
    read = tuning._read
    contract = read(home/'contract.json')
    tuning.validate(contract)
    data_audit = read(home/'data-audit.json')
    if sha(canonical(data_audit)) != contract['audit_sha256']:
        raise ValueError('Data audit changed')
    inputs = home/'inputs'
    files = {'relation-train.json':'train_sha256', 'official-bioredirect-train.json':'official_relation_train_sha256',
             'training-audit.json':'prior_audit_sha256'}
    if any(sha((inputs/name).read_bytes()) != data_audit[field] for name,field in files.items()):
        raise ValueError('Official training/audit bytes changed')
    official = read(inputs/'official-bioredirect-train.json')
    from nutri_literature.corpora_v5 import RELEASE_SHA, TRAIN_MEMBER
    if official['release_sha256'] != RELEASE_SHA or official['train_member'] != TRAIN_MEMBER or official['test_labels_read']:
        raise ValueError('Only pinned official train annotations are permitted')
    inherited = read(inputs/'training-audit.json')
    dev = read(inputs/'development-sources.json')['documents']
    excluded_ids = set(inherited['protected_pmids']) | {d['pmid'] for d in dev}
    excluded_text = set(inherited['protected_text_hashes']) | {text_key(d['text']) for d in dev}
    expected = sorted([d for d in official['documents'] if d['pmid'] not in excluded_ids and text_key(d['text']) not in excluded_text],
                      key=lambda d: sha('chatgpt-tuning-v1:'+str(d['pmid'])))[:40]
    actual = read(inputs/'calibration-gold.json')['documents']
    if canonical(actual) != canonical(expected) or [d['pmid'] for d in actual] != data_audit['calibration_pmids']:
        raise ValueError('Training calibration annotations differ from pinned deterministic sampling')
    old_audit = read(prior/'completion-audit.json')
    if any(sha((prior/name).read_bytes()) != value for name,value in old_audit['artifact_sha256'].items()):
        raise ValueError('An original published result was overwritten')
    response_ids, requests = [], 0
    for model, effort in tuning.MODELS:
        for split,total in [('calibration',40),('development',100)]:
            paths = sorted((tuning.directory(model)/split/'responses').glob('*.json'))
            if len(paths) != total:
                raise ValueError('Incomplete real-response cohort')
            for path in paths:
                response = read(path)
                request = read(tuning.directory(model)/split/'requests'/path.name)
                payload = read(home/'payloads'/split/path.name)
                if (response.get('status') != 'completed' or response.get('test_only')
                        or response.get('actual_model') != model or response.get('reasoning_effort') != effort
                        or response['request_sha256'] != sha(canonical(request))
                        or request['instructions'] != tuning.INSTRUCTIONS or request['input'] != canonical(payload)
                        or request['contract_sha256'] != sha(canonical(contract))):
                    raise ValueError('Real source-only inference audit failed')
                if sha(payload['source']) != payload['source_sha256']:
                    raise ValueError('Source bytes changed')
                for span in [*payload['sentences'],*payload['mentions']]:
                    if payload['source'][span['start']:span['end']] != span['text']:
                        raise ValueError('Original Unicode location mismatch')
                response_ids.append(response['response_id'])
                requests += 1
    if len(set(response_ids)) != 280:
        raise ValueError('Expected280 distinct real completed responses')
    # No authentication, client, model request or network function is invoked.
    before = sha((home/'comparison.json').read_bytes())
    catalog, identity, repeated_contract = tuning.prepare()
    if canonical(repeated_contract) != canonical(contract):
        raise ValueError('Reconstructed contract differs')
    tuning.calibrate(catalog, identity, contract)
    tuning.evaluate(catalog, identity, contract)
    if sha((home/'comparison.json').read_bytes()) != before:
        raise ValueError('Cached deterministic rescoring changed')
    suites = ET.parse(home/'verification.xml').getroot().findall('testsuite')
    tests = {k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
    if tests['failures'] or tests['errors'] or tests['skipped']:
        raise ValueError('Verification is not complete')
    result = {'status':'complete','real_completed_requests':requests,'unique_real_response_ids':len(set(response_ids)),
              'papers_development':100,'gold_relations':154,'prior_results_unchanged':True,
              'cached_rescoring_without_network_identical':True,'training_annotations_rederived_from_official_source':True,
              'unicode_source_locations_valid':True,'test_labels_opened':False,'default_engine':'heuristic-v1',
              'nutrition_accuracy':'unmeasured','tests':tests,
              'report_sha256':before,'contract_sha256':sha(canonical(contract)),
              'auditor_source_sha256':sha(Path(__file__).read_bytes()),
              'environment_sha256':sha((home/'environment.json').read_bytes())}
    immutable(home/'completion-audit.json',result)
    print(json.dumps(result,ensure_ascii=False))


if __name__ == '__main__':
    main()
