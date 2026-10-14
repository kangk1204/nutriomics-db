"""Bounded offline reconciliation; source databases are always opened read-only."""
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'research/2026-09-30/expansion'
CAPTURE = BASE / 'repository-reconciliation/additional-packages'


def sha(path):
    return hashlib.file_digest(path.open('rb'), 'sha256').hexdigest()


def read_db(path):
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    connection.execute('PRAGMA query_only=ON')
    return connection


def main():
    master = BASE / 'nutrition_evidence.sqlite3'
    before = sha(master)
    assert before == '724038e0527baea4114bd3d26c53541f1673ef906906b3f3659698de4bb5b0e1'
    receipt = json.loads((CAPTURE / 'nutriomics-evidence-engine--literature_service_integration.json').read_text())
    manifest = json.loads((CAPTURE / 'nutriomics-evidence-engine--pubmed_source_manifest.json').read_text())
    snapshot = ROOT / 'final_2026/repos/nutriomics-evidence-engine/artifacts/literature_snapshot_v3.sqlite'
    assert sha(snapshot) == receipt['snapshot_database_sha256']
    source = read_db(snapshot)
    assert source.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    papers = {pmid: json.loads(payload) for pmid, payload in source.execute('SELECT pmid,payload_json FROM paper')}
    statuses = dict(source.execute('SELECT status,count(*) FROM assertion GROUP BY status'))
    original = {pmid for query in manifest['searches'].values() for pmid in query['selected_pmids']}
    prototype = set(papers)
    overlap = {'original_collection': len(original), 'prototype_collection': len(prototype), 'shared_pmids': len(original & prototype), 'prototype_only_pmids': len(prototype - original), 'union_unique_pmids': len(original | prototype)}
    assert overlap == receipt['overlap']
    assert len(papers) == 597 and statuses == {'candidate': 1465}
    current = read_db(master)
    study_pmids = {p for (p,) in current.execute('SELECT DISTINCT pubmed_id FROM ng_study WHERE pubmed_id IS NOT NULL')}
    paper_licenses = Counter(str(p.get('license')) for p in papers.values())
    source_licenses = Counter(str(p.get('provider_provenance', {}).get('source_license')) for p in papers.values())
    source_kinds = Counter(str(p.get('provider_provenance', {}).get('source_kind')) for p in papers.values())
    study_overlap = {'unique_existing_study_pmids': len(study_pmids), 'in_original_collection': sorted(study_pmids & original), 'in_prototype_collection': sorted(study_pmids & prototype)}
    graph = json.loads((CAPTURE / 'nutriomics-evidence-engine--validated_snapshot.json').read_text())['graph']
    assert sum(graph['measurement_status'].values()) == graph['tables']['measurement']
    current.close()
    source.close()
    assert sha(master) == before
    result = {'audit_date_utc': '2026-10-02', 'master_sha256_before_and_after': before, 'master_unchanged': True, 'literature_snapshot_sha256': sha(snapshot), 'snapshot_integrity': 'ok', 'independently_checked_papers': len(papers), 'assertion_status': statuses, 'collection_overlap': overlap, 'bibliographic_study_overlap_only': study_overlap, 'paper_license_labels_not_release_approval': dict(paper_licenses), 'provider_license_labels_not_release_approval': dict(source_licenses), 'provider_source_kinds': dict(source_kinds), 'upstream_reported_measurement_status_not_reproduced': graph['measurement_status'], 'new_independent_cohorts': 0, 'new_approved_health_assertions': 0, 'clinical_benefit_inferred': False, 'model_or_api_started': False, 'software_semantic_accuracy': 'not_evaluated', 'redistribution': 'No new release; per-document rights review remains required'}
    destination = CAPTURE / 'overlap_audit_20261002.json'
    assert not destination.exists(), 'Refuse to overwrite another worker checkpoint'
    destination.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
