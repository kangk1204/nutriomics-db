"""Read-only preservation audit and deterministic reporting; no model calls or fitting."""
import os,sys,json,sqlite3
from pathlib import Path
from collections import Counter
for k in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):os.environ[k]='1'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nutri_literature.repair_v5.common import *

def baseline_manifest():
    path=HOME/'baseline-preservation.json'
    if path.exists():
        for n,h in read(path)['files'].items():assert filehash(ROOT/n)==h,n
        return
    files=[*sorted((V4/'predictions').glob('*/*.json')),V4/'comparison.json',V4/'completion-audit.json',V4/'freeze.json']
    write('baseline-preservation.json',{'files':{str(p.relative_to(ROOT)):filehash(p) for p in files},
        'scope':'pre-score v4 predictions and evaluation; original mutable attempt ledger excluded',
        'new_model_calls':0})
    print('baseline prediction hashes recorded',flush=True)

def main():
    baseline_manifest()
    if len(sys.argv)>1 and sys.argv[1]=='audit':return
    from nutri_literature.repair_v5.pipeline import verify_freeze
    verify_freeze();assert (HOME/'completion-audit.json').exists()
    c=read(HOME/'comparison.json');selection=read(HOME/'selection.json')
    labels={'v2':'v2','v3':'v3','v4':'v4 결합','identity_baseline':'ID 전용 기준선','retrieval_only':'v5 ID 검색·교정',
        'events_only':'v5 이벤트·기존 ID 고정','combined':'v5 최종'}
    pct=lambda v:'정의 불가' if v is None else format(v,'.2%')
    lines=['# 관계 추출 v5 실제 실행 결과','',
        '**'+('성능 목표를 충족했다.' if c['goal_met'] else '성능 목표에 미달했다.')+'** 같은 개발 100편·정답 154건, 원시 표준 ID·관계 유형 평가를 사용했다.','',
        '| 구성 | 정탐 | 오탐 | 누락 | 정밀도 | 재현율 | F1 |','|---|---:|---:|---:|---:|---:|---:|']
    for arm,label in labels.items():
        m=c['arms'][arm]['metrics'];lines.append(f"| {label} | {m['tp']} | {m['fp']} | {m['fn']} | {pct(m['precision'])} | {pct(m['recall'])} | {pct(m['f1'])} |")
    t=c['transitions']['combined'];lines+=['',f"기준선 정탐 65건 중 유지 {t['retained65']}건·상실 {t['lost65']}건·신규 {t['new']}건이다.",
        '합격 기준은 정탐 65건 이상, 재현율 65/154 이상, 정밀도 85% 이상이다. 65건 정탐이면 오탐은 최대 11건이어야 한다.','',
        '## 동결 구성과 학습 검증','',
        '선택 모드: `'+selection['selected']['mode']+'`. 학습 내부에서 선택하고 개발 점수로 다시 선택하지 않았다.']
    if selection['selected']['mode']=='baseline':lines.append('최종 구성은 학습 검증에서 선택된 기존 ID 전용 경로다. 신규 관계 편집기를 채택한 성능 향상으로 해석하지 않는다.')
    metric=selection['outer_actual'];lines.append('외부 5-fold 실제 검토 200편의 정밀도 '+pct(metric['precision'])+'·재현율 '+pct(metric['recall'])+'·F1 '+pct(metric['f1'])+'다. 음성 층화 가중 추정치는 selection.json에 별도로 기록했다.')
    lines+=['','## 기존 ID 검색 실패 45건','',
        '| 단계 | 관계 수 |','|---|---:|']
    stages=Counter(r['final_stage'] for r in c['previous45_records'])
    names={'matched':'최종 관계 정탐','in50_not_final':'상위 50 후보에 있으나 최종 미검출','rank51_to200_not_ranked':'51～200위 후보에만 존재','absent200':'상위 200 후보에도 없음'}
    for k,v in stages.items():lines.append('| '+names[k]+' | '+str(v)+' |')
    assert sum(stages.values())==45
    hits=sum(r['now_in200'] for r in c['previous45_records']);lines+=['',f"기존 실패 45건 중 {hits}건에서 필요한 ID가 새 상위 200 후보에 포함됐다. 이는 관계 정탐 복원 수와 다르다.",
        '검색 내부 200개, 순위 학습 입력 50개, 모델 메뉴 최대 20개를 구분했다. 표기·어순 변형은 검색 가설이며 자동 개체 동일성으로 취급하지 않았다.','',
        '## 전체 정확 경계 대응 언급의 후보 검색','',
        '| 종류 | 대응 언급 수 | 검색 Recall@10 | 검색 Recall@50 | 검색 Recall@200 | 정규화 순위 Recall@10 |',
        '|---|---:|---:|---:|---:|---:|']
    cov=c['candidate_coverage']
    for kind,label in [('chemical','물질'),('gene','유전자')]:
        n=cov.get(kind+'_exact_mentions',0)
        values=[cov.get(kind+'_recall'+str(k)+'_hits',0)/n if n else None for k in (10,50,200)]
        values.append(cov.get(kind+'_normalization_rank10_hits',0)/n if n else None)
        lines.append('| '+label+' | '+str(n)+' | '+' | '.join(pct(v) for v in values)+' |')
    lines+=['','이 표는 정확 경계가 대응한 언급에 조건부인 검색 지표다. 자동 개체 탐지의 전체 재현율이나 ID 자동 선택 정밀도가 아니다.','',
        '## 엄격한 ID 자동 선택','',
        '| 종류 | 신규·변경 선택 | 엄격 일치 정답 | 정밀도 | 기존 오연결 교정 | 기존 정답 손상 |','|---|---:|---:|---:|---:|---:|']
    ids=c['id_decision_counts']
    for kind,label in [('chemical','물질'),('gene','유전자')]:
        n=ids.get(kind+'_new_or_changed',0);ok=ids.get(kind+'_new_or_changed_strict_correct',0)
        lines.append(f"| {label} | {n} | {ok} | {pct(ok/n if n else None)} | {ids.get(kind+'_wrong_existing_corrected',0)} | {ids.get(kind+'_correct_existing_damaged',0)} |")
    lines+=['','동결된 v4의 언급별 선택과 비교한 수치다. 관계 후보에서는 NIL이 기존 ID를 보존하므로 언급 단위 손상과 관계 손상을 구분한다.','',
        '## 관계 오탐의 주석 대조','',
        '| 구성 | 유형 불일치 | 물질 ID 미주석 | 유전자 ID 미주석 | 관계쌍 미주석 |','|---|---:|---:|---:|---:|']
    for arm in ('identity_baseline','combined'):
        e=c['error_classes'][arm];lines.append('| '+labels[arm]+' | '+' | '.join(str(e.get(k,0)) for k in ('relation_type_mismatch','chemical_id_not_annotated','gene_id_not_annotated','annotated_endpoints_pair_not_annotated'))+' |')
    lines+=['','주석 부재와 생물학적 오류 확정을 구분한다. 미주석 관계를 개발 정답에 추가하거나 제외하지 않았다.','',
        '## 논문 단위 paired bootstrap 2,000회','',
        '| v5−ID 전용 기준선 | 차이 95% 구간 |','|---|---:|']
    for k,label in [('precision','정밀도'),('recall','재현율'),('f1','F1')]:
        ci=t['paired_bootstrap2000_ci95_vs_id_baseline'][k];lines.append(f"| {label} | {100*ci['lower']:+.2f}～{100*ci['upper']:+.2f}%p |")
    db=sqlite3.connect((PRIOR/'attempts.sqlite3').resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    attempts=[dict(r) for r in db.execute("SELECT phase,model,state,started,finished,error_class FROM attempts WHERE phase LIKE 'repair-v5-%'")];db.close()
    assert len(attempts)==c['additional_calls']<=25
    lines+=['','## 실제 추가 요청과 재현 감사','',
        f"실제 추가 요청 {len(attempts)}/25회. Sol {sum(r['model']=='gpt-6.1-sol' for r in attempts)}회, Luna {sum(r['model']=='gpt-6-luna' for r in attempts)}회. 남은 전체 예산 {c['budget']['remaining']}회.",
        f"새 학습 유효 응답 {len(list((HOME/'inference/train').glob('*/valid/*.json')))}개, 새 개발 유효 응답 {len(list((HOME/'inference/development').glob('*/valid/*.json')))}개다. 나머지 입력은 기존 응답의 원래 출처·해시를 유지해 재사용했다.",
        '실패와 형식 재시도도 같은 원래 장부에 포함했다. 계정·모델 자동 전환과 예산 초기화는 하지 않았다.',
        '동결 소스·색인·응답, 원문 문자 위치, 이전 예측 보존, 저장 예측 오프라인 재채점 동일 검사를 통과했다.',
        'v2 고정 상위 교정기의 기존 학습 자료 노출 이력을 유지한다. 이 100편은 반복 사용한 개발 자료이며 독립 최종 시험이 아니다.',
        '기본 엔진 heuristic-v1, 운영 API·웹 화면·기존 서버 작업과 봉인 시험을 변경하지 않았다. 영양 문헌 정확도와 검수 시간 절감은 미측정이다.','']
    text='\n'.join(lines);out=HOME/'interpretation.md'
    if out.exists() and out.read_text(encoding='utf8')!=text:raise ValueError('보고서 변경')
    out.write_text(text,encoding='utf8')
    write('report-manifest.json',{'script_sha256':filehash(Path(__file__)),'comparison_sha256':filehash(HOME/'comparison.json'),
        'baseline_manifest_sha256':filehash(HOME/'baseline-preservation.json'),'report_sha256':filehash(out),
        'new_model_calls':0,'policy_changed':False})
    print(canonical({'report':str(out),'goal_met':c['goal_met'],'additional_calls':len(attempts)}))

if __name__=='__main__':main()
