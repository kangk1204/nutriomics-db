"""Verify annotation provenance and prevent incomplete reviews being promoted."""
import hashlib
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / 'research/2026-09-30/expansion'


def validate_review(record):
    if record['review_actor'] != 'assistant' or record['human_approved']:
        raise ValueError('Assistant checks cannot carry human approval')
    if record['primary_relation_verified']:
        raise ValueError('These incomplete primary checks cannot be verified relations')
    if record['primary_text']['redistribution_approved']:
        raise ValueError('Unresolved article licenses must remain quarantined')
    if record['context']['health_outcome'] is not None:
        raise ValueError('Molecular source checks cannot create clinical outcomes')
    if record['context']['nutrient_dose'] is not None:
        raise ValueError('No primary nutrient dose was verified')
    if record['access']['gate_bypassed']:
        raise ValueError('Access challenges must remain explicit')


def main():
    master = BASE / 'nutrition_evidence.sqlite3'
    before = hashlib.file_digest(master.open('rb'), 'sha256').hexdigest()
    records = json.loads((BASE / 'mechanisms/source_checks_20261002.json').read_text())['records']
    assert len(records) == 2
    for record in records:
        validate_review(record)
        annotation = record['existing_annotation']
        path = BASE / annotation['capture']
        assert hashlib.file_digest(path.open('rb'), 'sha256').hexdigest() == annotation['sha256']
        protein = json.loads(path.read_text())
        assert 'UniProt:' + protein['primaryAccession'] == record['object']
        if record['predicate'] == 'cofactor_of':
            match = next(c['cofactors'][0] for c in protein['comments'] if c['commentType'] == 'COFACTOR' and c['cofactors'][0]['cofactorCrossReference']['id'] == record['subject'])
        else:
            match = next(c['reaction'] for c in protein['comments'] if c['commentType'] == 'CATALYTIC ACTIVITY' and any(x['id'] == annotation['rhea'] for x in c['reaction']['reactionCrossReferences']))
            assert any(x['id'] == record['subject'] for x in match['reactionCrossReferences'])
        assert any(e.get('id') == annotation['pmid'] and e['evidenceCode'] == annotation['evidence_code'] for e in match['evidences'])
    assert hashlib.file_digest(master.open('rb'), 'sha256').hexdigest() == before
    assert before == '724038e0527baea4114bd3d26c53541f1673ef906906b3f3659698de4bb5b0e1'
    print(json.dumps({'source_checks': 2, 'curated_captures_verified': 2, 'primary_relations_verified': 0, 'human_approved': 0, 'master_unchanged': True, 'master_sha256': before}))


if __name__ == '__main__':
    main()
