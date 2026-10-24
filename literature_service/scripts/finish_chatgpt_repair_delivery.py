"""One batch's final offline checks and report packaging, without further inference."""
from pathlib import Path
import os
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def workers_alive():
    import psutil
    for p in psutil.process_iter(['name', 'cmdline']):
        try:
            args = ' '.join(p.info['cmdline'] or [])
            if (p.pid != os.getpid() and (p.info['name'] or '').lower().startswith('python')
                    and any(s in args for s in ('scripts/run_chatgpt_repair_v2.py',
                                               'scripts/continue_chatgpt_repair_v2.py',
                                               'nutri_literature.chatgpt_repair_baseline'))):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def main():
    from nutri_literature.chatgpt_repair_io import HOME, AttemptBudget
    from nutri_literature.chatgpt_evaluation import _read
    from nutri_literature.contract_v7 import immutable, sha
    while not (HOME / 'completion-audit.json').exists():
        if not workers_alive():
            # Avoid declaring a process handoff gap to be an incomplete run.
            time.sleep(15)
            if not workers_alive() and not (HOME / 'completion-audit.json').exists():
                print({'status': 'incomplete', 'budget': AttemptBudget().status(),
                       'reason': 'No live batch worker and no completion audit; do not invent final scores.'}, flush=True)
                return
        time.sleep(10)
    for script in ('audit_chatgpt_repair_validation.py', 'summarize_chatgpt_repair_requests.py'):
        subprocess.run([sys.executable, str(ROOT / 'scripts' / script)], cwd=ROOT, check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    report = _read(HOME / 'comparison.json')
    audit = _read(HOME / 'completion-audit.json')
    runtime = _read(HOME / 'request-performance.json')
    doc = ROOT / 'docs/implementation-chatgpt-repair-v2.md'
    text = doc.read_text(encoding='utf-8')
    marker = '## 최종 실행 결과'
    if marker not in text:
        text = text.replace('상태: 구현·회귀 검증 완료, 실제 학습 자료 처리와 개발 평가 실행 중.',
                            '상태: 실제 학습·개발 평가와 오프라인 완료 감사를 마쳤다.')
        lines = [marker, '', '| 구성 | 정탐 | 오탐 | 누락 | 정밀도 | 재현율 | F1 |',
                 '|---|---:|---:|---:|---:|---:|---:|']
        names = {'baseline': '이전 결합 방식', 'identity_only': 'ID 교정',
                 'identity_and_type': 'ID·유형 교정', 'combined': '독립 검증·CPU 교정 결합'}
        def percent(value):
            return '미정의' if value is None else f'{100*value:.2f}%'
        for name, arm in report['arms'].items():
            m = arm['metrics']
            cells = [names[name], m['tp'], m['fp'], m['fn'], *[percent(m[k]) for k in ('precision', 'recall', 'f1')]]
            lines.append('| '+' | '.join(map(str, cells))+' |')
        transition = report['transitions_vs_previous_joint']['combined']
        lines.extend(['', '개발 기준 판정: '+('충족' if report['goal_met'] else '미달')+'. 기본 엔진은v1으로 유지했다.', '',
                      f"이전 정탐 유지{transition['retained_true_positives']}건, 상실{transition['lost_true_positives']}건, 신규 정탐{transition['new_true_positives']}건이다.", '',
                      f"실제 새 완료 응답{runtime['new_completed_requests']}건, 재사용{runtime['reused_completed_requests']}건, 실패·재시도를 포함한 시도{runtime['attempts_including_failures']['attempts']}회다.", '',
                      f"관련 시험{audit['tests']['tests']}건 통과, 원문 위치 불일치0건, 저장 응답 오프라인 재채점 일치를 확인했다. 구조·출처 검증이 과학적 정확도100%를 뜻하지는 않는다.", '',
                      '최종 구성은 신규 문헌당 원래 네 번의 호출과 독립 교정 두 번, 합계여섯 번을 필요로 한다. 호출별 시간·토큰은 기록했지만 파이프라인 전체 응답 시간과 요금은 추정하지 않았다.', '',
                      '이번100편은 반복 사용한 개발 자료다. 독립 최종 성능·영양학 정확도·검수 시간 절감은 확인하지 않았으며 봉인 자료와 사람 승인 상태는 유지했다.', ''])
        doc.write_text(text+'\n\n'+'\n'.join(lines), encoding='utf-8')
    artifacts = [HOME / n for n in ('comparison.json', 'comparison.md', 'comparison.csv', 'selection.json',
                  'completion-audit.json', 'validation-audit.json', 'request-performance.json', 'verification.xml',
                  'data-audit.json', 'environment.json', 'correction-contract.json', 'reference-contract.json')]
    artifacts += [doc, HOME / 'analysis/previous56-error-ledger.json']
    manifest = {'status': 'complete', 'goal_met': report['goal_met'], 'default_engine': 'heuristic-v1',
                'artifacts': {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in artifacts},
                'packager_source_sha256': sha(Path(__file__).read_bytes()), 'sealed_test_opened': False}
    immutable(HOME / 'delivery-manifest.json', manifest)
    print({'status': 'delivery-complete', 'metrics': report['arms']['combined']['metrics'],
           'goal_met': report['goal_met']}, flush=True)


if __name__ == '__main__':
    main()
