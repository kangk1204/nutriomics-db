"""Publish a verified 100-paper report to documentation on an explicit Git branch."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FILES = ('report.html', 'report.md', 'report.csv', 'report.json')


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True, encoding='utf8').strip()


def verified_bundle(directory, baseline):
    directory = Path(directory)
    manifest = json.loads((directory / 'report-manifest.json').read_text(encoding='utf8'))
    if set(manifest['files']) != set(FILES):
        raise ValueError('Unexpected report bundle files')
    blobs = {name:(directory / name).read_bytes() for name in FILES}
    for name, blob in blobs.items():
        if hashlib.sha256(blob).hexdigest() != manifest['files'][name]:
            raise ValueError('Report bundle hash mismatch: ' + name)
    payload = json.loads(blobs['report.json'])
    if (payload.get('gold_sha256') != baseline['relation_evaluation']['gold_sha256']
            or payload.get('cohort_pmids_sha256') != baseline['relation_evaluation']['cohort_pmids_sha256']):
        raise ValueError('Documentation requires the same cohort and gold hashes')
    row = next(r for r in payload['relation_comparison'] if r['model'] == 'v8')
    if row.get('papers') != 100 or row.get('gold_relations') != 154 or not row.get('metrics'):
        raise ValueError('The complete 100-paper v8 evaluation is required')
    from scripts.build_v8_submission_report import metrics
    checked = metrics(row['metrics'], 154)
    if row.get('predictions') != checked['tp'] + checked['fp']:
        raise ValueError('Prediction count differs from the evaluated relations')
    return payload, checked, blobs, manifest


def readme_result(text, payload, measured, relative):
    line = '| nutriomics-evidence-v8 | ' + ' | '.join([
        str(measured[k]) for k in ('tp', 'fp', 'fn')]) + ' | ' + ' | '.join([
        f'{100 * measured[k]:.2f}%' if measured[k] is not None else '미정의'
        for k in ('precision', 'recall', 'f1')]) + ' |'
    text, n = re.subn(r'^\| nutriomics-evidence-v8 \|[^\n]*', lambda _:line, text, flags=re.M)
    if n != 1:
        raise ValueError('README must contain one v8 benchmark row')
    r2 = next((r for r in payload['relation_comparison'] if r['model'] == 'r2' and r.get('metrics')), None)
    if r2:
        from scripts.build_v8_submission_report import metrics
        checked = metrics(r2['metrics'], 154)
        line = '| relation-repair-v6-gpu-r2 | ' + ' | '.join(str(checked[k]) for k in ('tp','fp','fn'))
        line += ' | ' + ' | '.join(f'{100*checked[k]:.2f}%' if checked[k] is not None else '미정의'
                                  for k in ('precision','recall','f1')) + ' |'
        text = re.sub(r'^\| relation-repair-v6-gpu-r2 \|[^\n]*', lambda _:line, text, flags=re.M)
        text = re.sub(r'^\| 기존 r2 \|[^\n]*', '| 기존 r2 | 학습·100편 예측·재평가 완료 |', text, flags=re.M)
    text = re.sub(r'기준일은 \*\*\d{4}-\d{2}-\d{2}\*\*',
                  '기준일은 **' + payload['observed_at_kst'][:10] + '**', text)
    text = text.replace('`nutriomics-evidence-v8`은 학습 중입니다.',
                        '`nutriomics-evidence-v8`의 동일 개발 100편 평가를 완료했습니다.')
    # The scientific default and the previous model rows are preserved.
    text = re.sub(r'^\| v8 run5 \|[^\n]*',
                  '| v8 run5 | NER·ID·관계 학습, 동결, 100편 예측·재평가 완료 |', text, flags=re.M)
    text = re.sub(r'^- v8 (?:NER·)?ID·관계 학습, 모델 동결, 동일 100편 전체 예측·재평가, 기존 구성과 비교\.\n',
                  '', text, flags=re.M)
    target = (measured['tp'] >= 65 and measured['recall'] >= 65 / 154
              and measured['precision'] is not None and measured['precision'] >= .85)
    statement = ('개발 목표는 **TP ≥65, 재현율 ≥65/154(42.21%), 정밀도 ≥85%**입니다. '
                 f"v8 실측은 TP {measured['tp']}·FP {measured['fp']}이며 목표 {'충족' if target else '미달'}입니다. "
                 'v5-r1 기준은 TP 65·FP 63입니다. 평가 구성과 웹 기본 엔진 `heuristic-v1`은 별도로 관리합니다.')
    text = re.sub(r'^개발 목표는 \*\*TP ≥65[^\n]*', lambda _:statement, text, flags=re.M)
    marker = '<!-- v8-evaluation-report -->'
    section = (marker + '\n## 최신 v8 재평가\n\n'
               + '관측 기준: ' + payload['observed_at_kst'] + '\n\n'
               + ' · '.join(f'[{label}]({relative}/{name})' for label, name in
                            [('HTML','report.html'),('Markdown','report.md'),('CSV','report.csv'),('JSON','report.json')])
               + '\n<!-- /v8-evaluation-report -->')
    if marker in text:
        text = re.sub(re.escape(marker) + r'.*?<!-- /v8-evaluation-report -->', lambda _:section, text, flags=re.S)
    else:
        text = text.rstrip() + '\n\n' + section + '\n'
    return text


def update(directory, *, root=ROOT, branch, push=False):
    root = Path(root).resolve()
    if git(root, 'branch', '--show-current') != branch:
        raise ValueError('Documentation branch changed; no files written')
    if git(root, 'status', '--porcelain'):
        raise ValueError('Documentation requires a clean worktree; no files written')
    if push and git(root, 'remote', 'get-url', 'origin') != 'https://github.com/kangk1204/nutriomics-db.git':
        raise ValueError('Documentation push destination changed; no files written')
    baseline = json.loads((root / 'docs/status/literature-2026-10-24.json').read_text(encoding='utf8'))
    payload, measured, blobs, manifest = verified_bundle(directory, baseline)
    digest = manifest['files']['report.json']
    if not re.fullmatch('[a-f0-9]{64}', digest):
        raise ValueError('Invalid report identity')
    relative = 'docs/reports/v8-evaluation-' + digest[:16]
    destination = root / relative
    readme = root / 'README.md'
    replacement = readme_result(readme.read_text(encoding='utf8'), payload, measured, relative)
    files = {**blobs, 'report-manifest.json':(Path(directory) / 'report-manifest.json').read_bytes()}
    for name, blob in files.items():
        existing = destination / name
        if existing.exists() and existing.read_bytes() != blob:
            raise ValueError('Immutable documentation report changed')
    destination.mkdir(parents=True, exist_ok=True)
    for name, blob in files.items():
        (destination / name).write_bytes(blob)
    readme.write_text(replacement, encoding='utf8')
    paths = ['README.md', *[relative + '/' + name for name in files]]
    git(root, 'add', '--', *paths)
    staged = git(root, 'diff', '--cached', '--name-only').splitlines()
    if set(staged) - set(paths) or git(root, 'branch', '--show-current') != branch:
        raise ValueError('Concurrent Git changes require manual documentation review')
    if staged:
        author = git(root, 'log', '-1', '--format=%an')
        email = git(root, 'log', '-1', '--format=%ae')
        git(root, '-c', 'user.name=' + author, '-c', 'user.email=' + email,
            'commit', '-m', 'Report verified v8 development evaluation')
    head = git(root, 'rev-parse', 'HEAD')
    if push:
        git(root, 'push', 'origin', 'HEAD:refs/heads/' + branch)
        actual = git(root, 'ls-remote', 'origin', 'refs/heads/' + branch).split()[0]
        if actual != head:
            raise ValueError('Remote documentation commit differs')
    return {'commit':head, 'branch':branch, 'pushed':push, 'report':relative,
            'report_sha256':digest, 'metrics':measured}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-directory', type=Path, required=True)
    parser.add_argument('--branch', required=True)
    parser.add_argument('--push', action='store_true')
    args = parser.parse_args()
    print(json.dumps(update(args.report_directory, branch=args.branch, push=args.push), ensure_ascii=False))


if __name__ == '__main__':
    main()
