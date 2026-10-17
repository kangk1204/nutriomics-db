"""Project display selectors onto actually rebuilt contrasts, preserving omissions."""
import argparse
import copy
import json
import hashlib
from pathlib import Path

from nutrigenomics.explorer_build import read_only, sha256
from nutrigenomics.source_updates import atomic_json
from scripts.rebuild_public_omics import SUFFIX


def rebuild(curation, core, output, plan=None):
    body = copy.deepcopy(json.loads(Path(curation).read_text(encoding='utf-8')))
    omitted = []
    versions = {(r['series'], r['historical_version']): r['analysis_version'] for r in (plan or {}).get('outcomes', [])}
    with read_only(Path(core)) as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM ng_contrast')]
    index = {(r['study_id'], r['series_accession'], r['contrast'], r['analysis_version']): r for r in rows}
    for study in body['studies']:
        selected = []
        for old in study.get('contrast_selectors', []):
            version = versions.get((old['series_accession'], old['analysis_version']), old['analysis_version'] + SUFFIX)
            key = study['study_id'], old['series_accession'], old['contrast'], version
            if key in index:
                selected.append({k: index[key][k] for k in ('id', 'series_accession', 'contrast', 'analysis_version')})
            else:
                omitted.append({'study_id': study['study_id'], 'selector': old,
                    'reason': 'No exact newly rebuilt contrast in this snapshot; study context remains available'})
        study['contrast_selectors'] = selected
    identity = hashlib.sha256(json.dumps({'studies': body['studies'], 'omitted': omitted}, sort_keys=True).encode()).hexdigest()[:12]
    body.update(version=body['version'] + '-public-rebuild-' + identity,
                parent_curation_sha256=sha256(Path(curation)), omitted_selectors=omitted,
                selector_policy='Exact study + series + contrast + new analysis version; no name substitution')
    atomic_json(Path(output), body)
    return {'selected': sum(len(s.get('contrast_selectors', [])) for s in body['studies']),
            'omitted': len(omitted), 'expert_review_status': body['expert_review_status']}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('curation', 'core', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(rebuild(a.curation, a.core, a.output)))
