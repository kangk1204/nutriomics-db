"""Read-only, post-evaluation report for the preserved v5 and corrected v5-r1 runs."""
import os,sys,json,sqlite3
from pathlib import Path
from collections import Counter
for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[key]='1'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nutri_literature import repair_v5_revision as r1
from nutri_literature.repair_v5 import common
from nutri_literature.evaluation_v6 import relation_sets

def pct(v):return '정의 불가' if v is None else format(v,'.2%')

def main():
    r1.verify()
    report=r1.read(r1.HOME/'comparison.json');s=r1.read(r1.HOME/'selection.json')
    initial=r1.read(r1.V5/'comparison.json');complete=r1.read(r1.HOME/'completion-audit.json')
    assert complete['offline_rescore_identical']
    for name,digest in r1.read(r1.V5/'baseline-preservation.json')['files'].items():
        assert r1.filehash(r1.ROOT/name)==digest,name
    from nutri_literature.repair_v3.runner import gold
    from scripts.analyze_chatgpt_repair_v4 import error_class
    docs,_=gold();byid={d['pmid']:d for d in docs};truth=set();observed={}
    for d in docs:truth.update(relation_sets(d,{'relations':[]})[0])
    assert len(truth)==154 and len(docs)==100
    for arm in r1.ARMS:
        predictions={d['pmid']:r1.read(r1.HOME/'predictions'/arm/('paper-'+d['pmid']+'.json')) for d in docs}
        assert common.native_metrics(docs,predictions)==report['arms'][arm]
        observed[arm]=set().union(*(relation_sets(d,predictions[d['pmid']])[1] for d in docs))
    errors={a:dict(Counter(error_class(byid[k[0]],k) for k in values-truth)) for a,values in observed.items()}
    types={}
    for arm,values in observed.items():
        types[arm]={}
        for typ in sorted({k[2] for k in truth|values}):
            g={k for k in truth if k[2]==typ};p={k for k in values if k[2]==typ}
            types[arm][typ]={'tp':len(g&p),'fp':len(p-g),'fn':len(g-p)}
    stages=Counter()
    for record in initial['previous45_records']:
        k=record['key'];key=(k[0],tuple(k[1]),k[2])
        stage=('matched' if key in observed['combined'] else
               'in50_not_final' if record['now_in50'] else
               'rank51_to200_not_ranked' if record['now_in200'] else 'absent200')
        stages[stage]+=1
    assert sum(stages.values())==45
    path=common.PRIOR/'attempts.sqlite3'
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    calls=[dict(row) for row in db.execute("SELECT phase,model,state,started,finished,error_class FROM attempts WHERE phase LIKE 'repair-v5-%'")]
    db.close();assert len(calls)==report['total_v5_calls']<=25
    train_valid=len(list((r1.V5/'inference/train').glob('*/valid/*.json')))
    dev_valid=len(list((r1.V5/'inference/development').glob('*/valid/*.json')))
    labels={'v2':'v2','v3':'v3','v4':'v4 결합','identity_baseline':'ID 전용 기준선',
            'v5_initial':'v5 최초 동결 구성','retrieval_only':'v5-r1 ID 상태 수정',
            'events_only':'v5-r1 이벤트·기존 ID 고정','combined':'v5-r1 최종 선택'}
    lines=['# 관계 추출 v5 구현·추가 추론·동일 기준 재평가','',
           '**판정: '+('목표 충족.' if report['goal_met'] else '목표 미달.')+'** 정탐 65건 이상, 재현율 65/154 이상, 정밀도 85% 이상을 동시에 요구했다.',
           '같은 개발 100편·정답 관계 154건과 원시 표준 ID·유형 평가기를 사용했다. 방향은 주지표에 합치지 않았다.','',
           '| 구성 | 정탐 | 오탐 | 누락 | 정밀도 | 재현율 | F1 |',
           '|---|---:|---:|---:|---:|---:|---:|']
    for arm in r1.ARMS:
        m=report['arms'][arm]['metrics']
        lines.append(f"| {labels[arm]} | {m['tp']} | {m['fp']} | {m['fn']} | {pct(m['precision'])} | {pct(m['recall'])} | {pct(m['f1'])} |")
    t=report['transitions']['combined']
    lines+=['',f"ID 전용 기준선의 정탐 65건 중 유지 {t['retained65']}건·상실 {t['lost65']}건·신규 복원 {t['new']}건이다.",
            '정탐 65건일 때 정밀도 85%를 만족하려면 오탐은 최대 11건이어야 한다.','',
            '## 실제 구현과 발견한 오류','',
            '전체 공식 별칭 704,518개를 스트리밍한 SQLite FTS5 단어·문자 검색 색인을 만들었다. 표기·어순 변형, 역사적 이름, 기존 후보를 함께 조회한다. 물질의 상위 분류·교체 기록을 동일성으로 바꾸지 않았다.',
            'ID 내부 검색 200개·순위 학습 50개·추론 메뉴 최대 20개를 구분했다. 관계 의미·ID 정확성·최종 엄격 수락 목표를 분리하고, 기존 관계는 후보로 보존했다. 신규 관계 유형에는 대응하는 원문 사건 근거가 필요하다.',
            '최초 동결 v5의 사후 진단에서 기존 자동 정규화 ID 상태를 일부 잃는 구현 오류를 발견했다. 관계에 쓰이지 않았던 기존 ID를 신규 후보로 취급하여 다수의 올바른 ID가 NIL로 바뀌었다. 최초 동결 소스·모델·평가를 보존하고 v5-r1에서 모든 fold 내부의 기존 ID 상태를 전달하도록 수정했다.',
            'v5-r1은 개발 진단 후의 일반적인 구현 수정이다. 새 프롬프트·추론 요청·임계값 격자·평가기 변경은 없으며, ID 번호나 정답 관계 유지 목록을 학습 특징으로 쓰지 않았다.','',
            '## 학습 선택과 미채택 사유','',
            '공식 학습에서 추가 검증 응답이 공통으로 있는 200편을 사용했다. 양성 142편·관계 549건을 모두 포함하고 음성 표집 가중치를 별도 보고했다. 전체 384편을 새로 모델 검증한 것으로 표현하지 않는다.',
            '외부 5-fold·내부 3-fold에서 ID 모델·분류기·선택 정책을 다시 적합했다. C=0.1·1·10과 사전 고정 임계값 격자를 사용하고 선택 후 100편 전체 예측을 저장한 다음 채점했다.',
            '동결 선택 모드: `'+s['selected']['mode']+'`.']
    if s['selected']['mode']=='baseline':
        lines.append('최종 구성은 학습에서 선택한 기존 ID 전용 경로다. 새 편집기를 채택한 개선으로 해석하지 않는다. 학습 결과가 기준을 충족하지 못해 개발용 6회 추가 요청도 실행하지 않았다.')
    for key,label in [('outer_actual','검토 200편의 실제 건수'),('outer_weighted','음성 표집 가중 추정')]:
        v=s[key];lines.append(f"{label}: TP {v['tp']:g}, FP {v['fp']:g}, FN {v['fn']:g}, 정밀도 {pct(v['precision'])}, 재현율 {pct(v['recall'])}, F1 {pct(v['f1'])}.")
    lines+=['','## ID 상태 수정의 실제 영향','',
            '| 종류 | 최초 v5 기존 정답 ID 손상 | 수정본 기존 정답 ID 손상 | 수정본 신규·변경 | 그중 엄격 정답 | 기존 오연결 교정 |',
            '|---|---:|---:|---:|---:|---:|']
    for kind,label in [('chemical','물질'),('gene','유전자')]:
        old=initial['id_decision_counts'];new=report['id_decisions']
        lines.append(f"| {label} | {old.get(kind+'_correct_existing_damaged',0)} | {new.get(kind+'_correct_existing_damaged',0)} | {new.get(kind+'_new_or_changed',0)} | {new.get(kind+'_new_or_changed_strict_correct',0)} | {new.get(kind+'_wrong_existing_corrected',0)} |")
    lines+=['','이는 해당 ID 모듈의 언급 단위 진단이다. 최종 관계 경로가 기준선이면 이 변경을 최종 채택한 것으로 해석하지 않는다. 초기 비교는 고정 v4 언급 선택, 수정본 비교는 fold 내부에서 재현한 기존 ID 전용 선택을 기준으로 한다.','',
            '## 검색 실패 45건의 추적','',
            '| 수정 후 단계 | 관계 건수 |','|---|---:|']
    stage_names={'matched':'최종 관계 정탐','in50_not_final':'검색 50위 이내이나 최종 미검출',
                 'rank51_to200_not_ranked':'검색 51～200위이나 순위 모델에 미입력','absent200':'검색 200위 안에도 없음'}
    for k in stage_names:lines.append(f"| {stage_names[k]} | {stages[k]} |")
    lines+=['','후보 검색 복원과 최종 정탐 복원은 다르다. 기존 실패 45건 중 상위 50 후보에 정답 ID가 들어온 관계 수를 최종 정탐 증가로 계산하지 않았다.','',
            '| 정확 경계가 대응한 언급 | 수 | 검색 Recall@10 | Recall@50 | Recall@200 |',
            '|---|---:|---:|---:|---:|']
    cov=initial['candidate_coverage']
    for kind,label in [('chemical','물질'),('gene','유전자')]:
        n=cov[kind+'_exact_mentions'];values=[pct(cov.get(kind+'_recall'+str(k)+'_hits',0)/n) for k in (10,50,200)]
        lines.append(f"| {label} | {n} | "+' | '.join(values)+' |')
    lines+=['','검색 지표는 정확 경계가 맞은 언급에 조건부다. 전체 자동 개체 탐지 재현율이나 ID 자동 선택 정밀도를 의미하지 않는다.','',
            '## 남은 오탐과 관계 유형','',
            '| 구성 | 유형 불일치 | 물질 ID 미주석 | 유전자 ID 미주석 | 관계쌍 미주석 |','|---|---:|---:|---:|---:|']
    for arm in ('v4','identity_baseline','retrieval_only','combined'):
        e=errors[arm];lines.append('| '+labels[arm]+' | '+' | '.join(str(e.get(k,0)) for k in ('relation_type_mismatch','chemical_id_not_annotated','gene_id_not_annotated','annotated_endpoints_pair_not_annotated'))+' |')
    lines+=['','주석 부재와 생물학적 오류 확정은 구분했다. 정답 관계를 추가하거나 평가 분모에서 제외하지 않았다.','',
            '| 최종 유형 | 정탐 | 오탐 | 누락 |','|---|---:|---:|---:|']
    for typ,m in types['combined'].items():lines.append(f"| {typ} | {m['tp']} | {m['fp']} | {m['fn']} |")
    lines+=['','## 비교의 불확실성과 재현 검증','',
            '| 최종−ID 전용 | 논문 단위 paired bootstrap 2,000회 차이 95% 구간 |','|---|---:|']
    for key,label in [('precision','정밀도'),('recall','재현율'),('f1','F1')]:
        ci=t['paired_bootstrap2000_ci95'][key]
        lines.append(f"| {label} | {100*ci['lower']:+.2f}～{100*ci['upper']:+.2f}%p |")
    lines+=['',f"실제 추가 추론 {len(calls)}/25회: Sol medium {sum(c['model']=='gpt-6.1-sol' for c in calls)}회, Luna max {sum(c['model']=='gpt-6-luna' for c in calls)}회. 학습 유효 응답 {train_valid}개, 개발 신규 유효 응답 {dev_valid}개다.",
            f"원래 공유 장부 {report['budget']['attempts']}/3000회이며 잔여 {report['budget']['remaining']}회다. 실패·형식 재시도도 사용량에 포함했다. v5-r1의 새 요청은 0회다.",
            '새 추론은 원문 기반 후보 변화·NIL·불일치로 고른 학습 8편에 두 모델을 적용했다. 기존 오탐 목록이나 정답 ID를 호출 문헌 선택에 사용하지 않았다. 과거 응답은 원래 입력·해시를 유지해 재사용했다.',
            '75개 관련 회귀시험을 통과했다. 원문 위치, 기존 예측 해시, 동결 소스·모델·색인·응답, 저장 예측 재채점 일치를 확인했다.',
            '작업은 로컬 Windows CPU 배치에서 수행했다. 별도 서버 재평가나 운영 전환을 완료했다고 주장하지 않는다. 운영 v1·API·웹·기존 서버 작업·봉인 시험은 변경하지 않았다.',
            '이 100편은 반복 사용한 개발 비교 자료다. v2 고정 상위 모델의 기존 학습 자료 노출 이력을 포함해 독립 최종 성능으로 주장하지 않는다. 영양 문헌의 실제 정확도와 검수 시간 절감은 미측정이다.','']
    output=r1.HOME/'implementation-report.md';text='\n'.join(lines)
    if output.exists():assert output.read_text(encoding='utf8')==text
    output.write_text(text,encoding='utf8')
    r1.write('post-evaluation-diagnostics.json',{'error_classes':errors,'per_type':types,'previous45_final_stages':dict(stages),
             'additional_calls':calls,'development_valid':dev_valid,'training_valid':train_valid,'policy_changed':False})
    r1.write('report-manifest.json',{'report_sha256':r1.filehash(output),'script_sha256':r1.filehash(Path(__file__)),
             'comparison_sha256':r1.filehash(r1.HOME/'comparison.json'),'previous_v5_preserved':True,'offline_rescore_identical':True,
             'source_freeze_verified':True,'new_calls':0})
    print(common.canonical({'report':str(output),'goal_met':report['goal_met'],'calls':len(calls)}))

if __name__=='__main__':main()
