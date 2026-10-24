"""Render completed, frozen v4 results; no fitting, selection or model requests."""
import os
import sys
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
for variable in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[variable]='1'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nutri_literature.repair_v4.common import HOME,PRIOR,ROOT,read,canonical,sha,immutable
from nutri_literature.repair_v4.pipeline import verify_freeze

def main():
    verify_freeze()
    assert read(HOME/'completion-audit.json')['offline_rescore_identical']
    out=ROOT/'runtime/chatgpt-repair-v4-postscore'
    comparison=read(HOME/'comparison.json');diagnostics=read(out/'diagnostics.json')
    identities=read(HOME/'analysis/id-report.json')['counts']
    fates=read(HOME/'analysis/error-fates.json');selection=read(HOME/'selection.json')
    connection=sqlite3.connect('file:'+str(PRIOR/'attempts.sqlite3')+'?mode=ro',uri=True)
    connection.row_factory=sqlite3.Row
    rows=[dict(row) for row in connection.execute(
        'select phase,model,state,started,finished,error_class from attempts where id>?',
        (read(HOME/'audit.json')['budget_at_start']['attempts'],))]
    connection.close()
    assert len(rows)==comparison['additional_calls'] and len(rows)<=653
    started=min(row['started'] for row in rows)
    finished=max(row['finished'] for row in rows if row['finished'])
    elapsed=(datetime.fromisoformat(finished)-datetime.fromisoformat(started)).total_seconds()
    percent=lambda value:'정의 불가' if value is None else f'{value:.2%}'
    labels={'v2':'v2','v3':'v3','sol_only':'새 Sol 단독','luna_only':'새 Luna 단독',
            'identity_only':'ID만 교정','events_locked_ids':'이벤트 개선·기존 ID 고정','combined':'v4 결합'}
    lines=['# 관계 추출 v4 실행 결과와 실패 원인','',
        '**구현·추가 추론·동일 기준 재평가는 완료했지만 성능 목표는 달성하지 못했다.**',
        '개발 100편·정답 154건의 원시 ID와 관계 유형 일치를 그대로 사용했다. 개발 점수를 본 뒤 구성이나 임계값을 바꾸지 않았다.','',
        '| 구성 | 정탐 | 오탐 | 누락 | 정밀도 | 재현율 | F1 |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for arm,label in labels.items():
        m=comparison['arms'][arm]['metrics']
        lines.append(f"| {label} | {m['tp']} | {m['fp']} | {m['fn']} | {percent(m['precision'])} | {percent(m['recall'])} | {percent(m['f1'])} |")
    transition=comparison['transitions']['combined']
    lines+=['','## 판정과 정탐 보존','',
        f"v4 결합은 v2의 정탐 중 {transition['retained_v2_tp']}건을 유지하고 {transition['lost_v2_tp']}건을 잃었으며 {transition['new_tp']}건을 새로 복원했다.",
        'ID 전용 비교군은 v2의 정탐 64건을 모두 유지하고 1건을 추가했다. 정밀도 85%에는 미달하며, 이 개발 점수를 근거로 기본 구성을 다시 선택하지 않았다.',
        '기본 엔진은 heuristic-v1이다. 운영 API·웹 화면·기존 서버 작업과 봉인 시험은 변경하지 않았다.','',
        '## Association 오탐 41건의 처리','',
        f"기존 v3의 41건 중 유형 교정 {fates['association41'].get('type_corrected',0)}건, 기존 오류 유지 {fates['association41'].get('retained_error',0)}건, 제거 또는 ID 변경 {fates['association41'].get('removed_or_identity_changed',0)}건이다."]
    old=comparison['per_type']['v3']['Association'];new=comparison['per_type']['combined']['Association']
    lines += [f"전체 Association 오탐은 {old['fp']}→{new['fp']}건으로 줄었지만 정탐도 {old['tp']}→{new['tp']}건으로 줄었다. 해당 유형 정밀도는 {percent(old['precision'])}→{percent(new['precision'])}다.",
        '오탐 감소를 정탐·재현율을 유지한 개선으로 해석할 수 없다. 주석에 없는 개체·관계는 생물학적으로 잘못됐다는 판정과 구분했다.','',
        '## ID 교정과 남은 손실','',
        f"기존 물질 ID {identities['chemical'].get('changed_existing',0)}건을 변경했고, 두 건 모두 정답 주석과 일치했다. 유전자 기존 오연결 교정은 {identities['gene'].get('wrong_old_corrected',0)}건이다.",
        f"정확했던 기존 유전자 ID {identities['gene'].get('correct_old_damaged',0)}건은 NIL로 보류돼 손실됐다. 신규·변경 연결의 엄격한 정밀도는 물질 {percent(identities['chemical']['new_or_changed_strict_precision'])}, 유전자 {percent(identities['gene']['new_or_changed_strict_precision'])}다.",
        '물질 ID 교정이 0건에 머무른 상태는 해소했지만, 변경 표본 2건만으로 일반 정확도 90%를 입증할 수 없다. 물질의 신규·변경 연결 정밀도도 개발 목표 90%에 미달했다.',
        '최초 관측 실패 단계는 아래와 같다. 이 단계별 집계는 인과적 복원 효과의 추정치가 아니다.','',
        '| 최초 관측 단계 | 건수 |','|---|---:|']
    names={'chemical_exact_boundary_not_detected':'물질 경계 누락','gene_exact_boundary_not_detected':'유전자 경계 누락',
        'chemical_id_not_retrieved':'물질 ID 후보 검색 실패','gene_id_not_retrieved':'유전자 ID 후보 검색 실패',
        'chemical_id_not_selected':'물질 ID 선택 실패','gene_id_not_selected':'유전자 ID 선택 실패',
        'no_resolved_mention_pair_bag':'대응하는 언급 묶음 부재','no_matching_native_type_witness':'대응하는 관계 유형 근거 부재',
        'type_competition_or_training_threshold':'유형 경쟁 또는 학습 임계값','matched':'정탐'}
    lines.extend(f'| {names[k]} | {v} |' for k,v in diagnostics['first_gap_counts'].items())
    lines+=['','학습 외부 5-fold 합산 정밀도 '+percent(selection['outer_metrics']['precision'])+'·재현율 '+percent(selection['outer_metrics']['recall'])+'로 학습 진입 기준도 미달했다. 이번 동결 구성은 진단용이며 봉인 최종 시험이나 운영 전환의 자격을 얻지 못했다.','',
        '## 논문 단위 paired bootstrap 2,000회','',
        '| 비교 | 정밀도 차이 95% 구간 | 재현율 차이 95% 구간 | F1 차이 95% 구간 |',
        '|---|---:|---:|---:|']
    for base in ('v2','v3'):
        ci=transition['paired_bootstrap2000_ci95_vs_'+base]
        cells=[f"{100*ci[k]['lower']:+.2f}～{100*ci[k]['upper']:+.2f}%p" for k in ('precision','recall','f1')]
        lines.append('| v4−'+base+' | '+' | '.join(cells)+' |')
    lines+=['','## 실제 추가 요청과 재현','',
        f"추가 요청 {len(rows)}/653회. Sol {sum(r['model']=='gpt-6.1-sol' for r in rows)}회, Luna {sum(r['model']=='gpt-6-luna' for r in rows)}회. 학습 검증 400개와 개발 검증 200개의 유효 응답을 보존했다.",
        f"요청 장부상 완료 {sum(r['state']=='completed' for r in rows)}회·실패 {sum(r['state']=='failed' for r in rows)}회이며, 완료 요청 수에는 초기 기술 입력과 파싱 재시도도 포함된다. 동일 정규 입력의 유효 응답 600개와 구분한다.",
        f"첫 추가 요청부터 마지막 응답까지 {elapsed/3600:.2f}시간이 경과했다. 대기·재개·중간 CPU 처리를 포함한 경과 시간이며, 학습 내부 선택 기록의 처리 시간은 {selection['elapsed_seconds']:.1f}초다.",
        f"잔여 예산은 {comparison['budget']['remaining']}회다. 계정·모델 자동 전환이나 장부 초기화는 하지 않았다.",
        '저장 예측의 오프라인 재채점은 동일했고 원문 위치와 이전 버전 보존 검사를 통과했다. 학습 후 동결 소스·모델·규칙의 해시가 동일함을 재확인했다.',
        '사후 진단과 본 보고서 작성에는 모델 요청·재학습·정책 변경이 없었다.',
        '이 100편은 반복 노출된 개발 자료다. 독립 최종 성능, 영양 문헌 정확도, 전문가 검수 시간 절감은 입증하지 않았다.','']
    report='\n'.join(lines)
    immutable(out/'execution-statistics.json',{'calls':len(rows),'models':dict(Counter(r['model'] for r in rows)),
        'phases':dict(Counter(r['phase'] for r in rows)),'states':dict(Counter(r['state'] for r in rows)),
        'first_started_utc':started,'last_finished_utc':finished,'wall_elapsed_seconds':elapsed,
        'report_added_model_calls':0,'policy_changed':False})
    path=out/'interpretation.md'
    if path.exists() and path.read_text(encoding='utf8')!=report:raise ValueError('immutable report differs')
    path.write_text(report,encoding='utf8')
    immutable(out/'interpretation-manifest.json',{'script_sha256':sha(Path(__file__).read_bytes()),
        'comparison_sha256':sha((HOME/'comparison.json').read_bytes()),
        'diagnostics_sha256':sha((out/'diagnostics.json').read_bytes()),
        'report_sha256':sha(path.read_bytes()),'new_model_calls':0,'policy_changed':False})
    print(canonical({'report':str(path),'calls':len(rows),'goal_met':comparison['goal_met']}))

if __name__=='__main__':main()
