"""Verify executed rebuild producers and attach a computational gate, not human review."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path

from nutrigenomics.explorer_build import sha256
from nutrigenomics.source_updates import atomic_json
from scripts.freeze_omics_bundle import freeze


def audit(work, source):
    work, source = Path(work), Path(source)
    state = json.loads((work / 'analysis-rebuild-status.json').read_text())
    plan = json.loads((work / 'analysis-rebuild-plan.json').read_text())
    if any(r.get('status') == 'running' for r in state['jobs'].values()):
        raise RuntimeError('Wait for the producer DAG; never approve a running output')
    verified_jobs, findings, verified_inputs = [], [], {}
    for job in plan['jobs']:
        record = state['jobs'].get(job['id'], {})
        if record.get('returncode') != 0 or record.get('status') not in {'completed', 'completion_failed'}:
            findings.append({'job': job['id'], 'reason': 'producer_not_successful'})
            continue
        script = source / 'scripts' / job['script']
        if sha256(script) != job['source_sha256'] or record.get('source_sha256') != job['source_sha256']:
            raise ValueError('Executed source hash mismatch: ' + job['id'])
        if record['arguments'] != job['args'] or not record.get('finished_at'):
            raise ValueError('Producer command/exit evidence mismatch')
        for folder in job['outputs']:
            bundle = work / 'outputs' / folder
            path = bundle / 'analysis_metadata.json'
            metadata = json.loads(path.read_text())
            inputs = metadata.get('input_manifest', [])
            if isinstance(inputs, dict):
                inputs = [inputs]
            if metadata.get('input_file') and metadata.get('input_file_sha256'):
                inputs = [*inputs, {'path': str(work / 'data/raw/geo' / metadata['accession'] / metadata['input_file']),
                                    'sha256': metadata['input_file_sha256']}]
            for entry in inputs:
                input_path, digest = Path(entry['path']), entry['sha256']
                previous = verified_inputs.get(str(input_path))
                if previous is not None and previous != digest:
                    raise ValueError('One input path has conflicting executed hashes')
                if previous is None and (not input_path.is_file() or sha256(input_path) != digest):
                    raise ValueError('Executed input is missing or has changed: ' + str(input_path))
                verified_inputs[str(input_path)] = digest
            if not (bundle / 'DONE.json').exists() and not metadata.get('script_sha256'):
                # Preserve the exact R-emitted JSON before adding wrapper provenance.
                original = bundle / 'analysis_metadata.producer-original.json'
                if original.exists() and original.read_bytes() != path.read_bytes():
                    raise ValueError('Unfrozen metadata already changed')
                shutil.copyfile(path, original)
                metadata.update(script_path=str(script), script_sha256=job['source_sha256'],
                    provenance_enrichment={'actor': 'executed Python producer wrapper',
                        'original_metadata_sha256': sha256(original),
                        'basis': 'source hashes checked before and after the observed zero-exit R process',
                        'expert_review': 'pending'})
                atomic_json(path, metadata)
            if metadata.get('script_sha256') != job['source_sha256']:
                raise ValueError('R metadata does not identify its executed script: ' + folder)
            freeze(bundle)
        verified_jobs.append({'name': job['id'], 'script': job['script'],
            'script_sha256': job['source_sha256'], 'status': 'completed', 'returncode': 0,
            'started_at': record['started_at'], 'finished_at': record['finished_at'],
            'arguments': job['args'], 'outputs': [str(work / 'outputs' / f) for f in job['outputs']],
            'source_controller_receipt_sha256': sha256(work / 'analysis-rebuild-status.json')})
    receipt = {'jobs': verified_jobs, 'expert_review_status': 'pending',
               'scope': 'Observed computational producer execution; no independent scientific review'}
    digest = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
    producer = work / 'producer_status_snapshots' / (digest + '.json')
    producer.parent.mkdir(exist_ok=True)
    if not producer.exists():
        atomic_json(producer, receipt)
    completed = []
    addon_path = work / 'toolchain/r-addons-20261017.json'
    addons = json.loads(addon_path.read_text()) if addon_path.is_file() else None
    if addons is not None and (addons.get('status') != 'passed' or addons.get('previous_packages_changed') != 0):
        raise ValueError('Unverified R runtime extension')
    for outcome in plan['outcomes']:
        producer_job = next((j for j in verified_jobs if str(work / 'outputs' / outcome['folder']) in j['outputs']), None)
        if producer_job is None:
            continue
        bundle = work / 'outputs' / outcome['folder']
        ready = freeze(bundle)
        metadata = json.loads((bundle / 'analysis_metadata.json').read_text())
        if producer_job['name'] in {'fat-crossover', 'fish-oil-crossover'}:
            lock = Path(metadata.get('environment_lock_path') or work / 'toolchain/r-environment-linux-64.lock.txt')
            base_text = lock.read_text()
            pinned_in_base = all(name in base_text for name in ('bioconductor-makecdfenv', 'bioconductor-hgu133plus2cdf'))
            if not pinned_in_base and addons is None:
                raise ValueError('CDF readers must be in the base lock or a verified additive runtime manifest')
        if ready['analysis_version'] != outcome['analysis_version'] or int(metadata.get('independent_people', 0)) < 2:
            raise ValueError('Outcome identity/person count mismatch')
        identity = ':'.join(metadata[k] for k in ('accession', 'analysis_version', 'run_utc', 'script_sha256'))
        approval = {'completion_sha256': sha256(bundle / 'DONE.json'), 'producer_exit_code': 0,
            'scientific_qc_status': 'passed_with_declared_limitations',
            'review_type': 'automated computational integrity audit', 'expert_review_status': 'pending',
            'run_id': hashlib.sha256(identity.encode()).hexdigest(),
            'producer_status_path': str(producer), 'producer_status_sha256': sha256(producer),
            'producer_job': producer_job['name'],
            'actual_execution_times': {k: producer_job[k] for k in ('started_at', 'finished_at')},
            'additive_runtime_manifest': ({'filename': addon_path.name, 'sha256': sha256(addon_path),
                'previous_packages_changed': 0, 'introduced_packages': addons['introduced_packages']} if addons else None),
            'checks': ['Observed zero process exit and exact command/source identity',
                       'Result files and source snapshots match immutable hashes',
                       'P and FDR are finite in [0,1] when reported; missing values preserved',
                       'Reported independent people count is at least two'],
            'limits': ['Computational checks do not constitute expert approval of study design or biological interpretation',
                       'New analysis versions use reacquired public originals and a newly locked environment',
                       'Historical exact numerical identity is not claimed; source server is unavailable']}
        approval_path = bundle / 'RELEASE_APPROVAL.json'
        if approval_path.exists():
            old = json.loads(approval_path.read_text())
            if old['completion_sha256'] != approval['completion_sha256']:
                raise ValueError('Completed output changed')
        else:
            atomic_json(approval_path, approval)
        completed.append({**outcome, 'executed_and_frozen': True,
                          'completion_sha256': sha256(bundle / 'DONE.json')})
    result = {'status': 'completed' if len(completed) == 14 else 'incomplete',
              'executed_public_outcomes': len(completed), 'outcomes': completed,
              'findings': findings, 'verified_input_files': len(verified_inputs),
              'expert_review_status': 'pending'}
    atomic_json(work / 'rebuild-computational-audit.json', result)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('work', 'source'):
        p.add_argument('--' + name, type=Path, required=True)
    a = p.parse_args()
    result = audit(a.work, a.source)
    print(json.dumps(result))
    raise SystemExit(0 if result['status'] == 'completed' else 1)
