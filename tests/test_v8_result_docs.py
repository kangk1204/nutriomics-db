"""Temporary Git integration checks; all metrics here are synthetic fixtures."""
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from scripts.update_v8_result_docs import update, verified_bundle


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


@pytest.fixture
def publication(tmp_path):
    root=tmp_path/'repo';root.mkdir()
    git(root,'init','-b','feature/test')
    git(root,'config','user.name','Synthetic Test')
    git(root,'config','user.email','test@example.invalid')
    status=root/'docs/status';status.mkdir(parents=True)
    baseline={'relation_evaluation':{'gold_sha256':'a'*64,'cohort_pmids_sha256':'b'*64}}
    (status/'literature-2026-10-24.json').write_text(json.dumps(baseline))
    (root/'README.md').write_text('# Synthetic fixture\n\n현재 웹 기본 엔진은 `heuristic-v1`입니다. '
        '`nutriomics-evidence-v8`은 학습 중입니다.\n\n'
        '| chatgpt-repair-v5-r1 | 65 | 63 | 89 | 50.78% | 42.21% | 46.10% |\n'
        '| nutriomics-evidence-v8 | 평가 대기 | — | — | — | — | — |\n\n'
        '개발 목표는 **TP ≥65, 재현율 ≥65/154(42.21%), 정밀도 ≥85%**입니다.\n\n'
        '| v8 run5 | 학습 중 |\n\n'
        '- v8 ID·관계 학습, 모델 동결, 동일 100편 전체 예측·재평가, 기존 구성과 비교.\n',encoding='utf8')
    git(root,'add','.');git(root,'commit','-m','Synthetic base')
    report=tmp_path/'report';report.mkdir()
    payload={'gold_sha256':'a'*64,'cohort_pmids_sha256':'b'*64,
             'observed_at_kst':'2026-10-24T10:00:00+09:00',
             'relation_comparison':[{'model':'v8','papers':100,'gold_relations':154,
                 'metrics':{'tp':65,'fp':10,'fn':89},'predictions':75}]}
    for name,blob in {'report.json':json.dumps(payload).encode(), 'report.html':b'<p>Synthetic</p>',
                      'report.md':b'Synthetic', 'report.csv':b'Synthetic'}.items():
        (report/name).write_bytes(blob)
    manifest={'files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in report.iterdir()}}
    (report/'report-manifest.json').write_text(json.dumps(manifest))
    return root,report,baseline


def test_report_commit_preserves_prior_rows_default_and_is_idempotent(publication):
    root,report,_=publication;before=git(root,'rev-parse','HEAD')
    result=update(report,root=root,branch='feature/test')
    text=(root/'README.md').read_text(encoding='utf8')
    assert '| nutriomics-evidence-v8 | 65 | 10 | 89 | 86.67% | 42.21% | 56.77% |' in text
    assert '| chatgpt-repair-v5-r1 | 65 | 63 | 89 | 50.78% | 42.21% | 46.10% |' in text
    assert '기본 엔진은 `heuristic-v1`' in text
    assert '- v8 ID·관계 학습' not in text
    assert '개발 목표는' not in text and '85%**' not in text
    assert result['commit']!=before and result['pushed'] is False
    assert not git(root,'status','--porcelain')
    repeated=update(report,root=root,branch='feature/test')
    assert repeated['commit']==result['commit']
    assert text.count('<!-- v8-evaluation-report -->')==1


def test_reviewer_readme_accepts_verified_result_without_reintroducing_targets(publication):
    root, report, _ = publication
    (root / 'README.md').write_text('# NutriOmics\n\n[성과표](docs/reviewer/RESULTS.md)\n', encoding='utf8')
    git(root, 'add', 'README.md'); git(root, 'commit', '-m', 'Synthetic reviewer presentation')
    result = update(report, root=root, branch='feature/test')
    text = (root / 'README.md').read_text(encoding='utf8')
    assert '86.67%' in text and 'docs/reviewer/RESULTS.md' in text
    assert '목표' not in text
    assert update(report, root=root, branch='feature/test')['commit'] == result['commit']


@pytest.mark.parametrize('issue',['dirty','branch'])
def test_existing_work_or_another_branch_blocks_all_document_writes(publication,issue):
    root,report,_=publication
    if issue=='dirty':(root/'README.md').write_text('User work',encoding='utf8')
    before=(root/'README.md').read_bytes()
    with pytest.raises(ValueError):
        update(report,root=root,branch='wrong-branch' if issue=='branch' else 'feature/test')
    assert (root/'README.md').read_bytes()==before and not (root/'docs/reports').exists()


def test_tampered_report_and_different_cohort_are_rejected(publication):
    _,report,baseline=publication
    other={'relation_evaluation':{**baseline['relation_evaluation'],'gold_sha256':'c'*64}}
    with pytest.raises(ValueError,match='cohort and gold'):verified_bundle(report,other)
    (report/'report.md').write_bytes(b'Changed')
    with pytest.raises(ValueError,match='hash mismatch'):verified_bundle(report,baseline)


def test_scoped_commit_preserves_unrelated_staged_unstaged_and_untracked_work(publication):
    root,report,_=publication
    source=root/'source.py';source.write_text('base\n')
    git(root,'add','source.py');git(root,'commit','-m','Synthetic source')
    source.write_text('staged change\n');git(root,'add','source.py')
    source.write_text('working change\n')
    untracked=root/'other-work.txt';untracked.write_text('Uncommitted user work')
    staged=git(root,'diff','--cached','--','source.py')
    unstaged=git(root,'diff','--','source.py')
    result=update(report,root=root,branch='feature/test')
    assert result['pushed'] is False
    assert git(root,'show','HEAD:source.py')=='base'
    assert git(root,'diff','--cached','--','source.py')==staged
    assert git(root,'diff','--','source.py')==unstaged
    assert source.read_text()=='working change\n' and untracked.read_text()=='Uncommitted user work'
