"""Preserve the first report; clarify the actually executed r1 editor and ID gates."""
import os,sys
from pathlib import Path
for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[key]='1'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts import report_chatgpt_repair_v5_revision as reporting
from nutri_literature import repair_v5_revision as r1
from nutri_literature.repair_v3.runner import gold
from nutri_literature.evaluation_v6 import relation_sets

def main():
    reporting.main()
    initial=r1.read(r1.V5/'selection.json');revised=r1.read(r1.HOME/'selection.json')
    docs,_=gold();sets={}
    for arm in ('identity_baseline','combined'):
        sets[arm]=set().union(*(relation_sets(d,r1.read(r1.HOME/'predictions'/arm/('paper-'+d['pmid']+'.json')))[1] for d in docs))
    added=len(sets['combined']-sets['identity_baseline']);removed=len(sets['identity_baseline']-sets['combined'])
    text=(r1.HOME/'implementation-report.md').read_text(encoding='utf8')
    marker='동결 선택 모드: `'+revised['selected']['mode']+'`.'
    extra=(f"최초 v5 선택은 `{initial['selected']['mode']}`, 수정본 v5-r1 선택은 `{revised['selected']['mode']}`다. "
           f"수정본은 실제 편집기 정책으로 실행했으나, 최종 원시 ID·유형 관계 집합의 추가 {added}건·삭제 {removed}건이다. "
           '편집기 실행을 생략해 기준선으로 돌아간 것과, 실행해도 최종 관계를 바꾸지 못한 것을 구분한다. '
           '훈련 내부 기준이 미달하여 개발 문헌 신규 추론 6회는 실행하지 않았다.')
    assert text.count(marker)==1;text=text.replace(marker,marker+'\n'+extra)
    policy=revised['model']['identity']
    assert policy['change']['enabled'] is False
    section=('## ID 교정이 0건인 직접 원인\n\n'
             '수정본의 기존 ID 변경 정책은 학습 fold 외 예측에서 정밀도 90%를 만족하는 설정을 찾지 못해 비활성화됐다. '
             '따라서 새 검색에서 정답 후보를 찾아도 이미 선택된 잘못된 ID는 자동 교정하지 않는다. ID 검색과 ID 변경 정책의 병목은 서로 다르다.\n'
             f"NIL에서 신규 ID를 고르는 정책은 fold 외 정답 {policy['new']['correct']}건·오답 {policy['new']['wrong']}건으로 선택됐다. "
             '그러나 100편에서는 신규 선택 13건 중 원시 ID 엄격 정답이 5건(38.46%)에 그쳤다. 작은 선택 표본의 90% 목표를 개발 자료의 달성값으로 표현할 수 없다. '
             '미주석 ID를 생물학적으로 틀렸다고 확정하지 않지만, 같은 주평가 기준에서는 실패로 유지했다.\n'
             '기존 ID 손상 0건은 구현 오류가 수정됐음을 확인하는 결과다. ID 정규화 정확도 목표나 관계 정밀도 목표를 달성했다는 의미는 아니다.\n\n')
    text=text.replace('## 검색 실패 45건의 추적',section+'## 검색 실패 45건의 추적')
    output=r1.HOME/'final-report.md'
    if output.exists():assert output.read_text(encoding='utf8')==text
    output.write_text(text,encoding='utf8')
    r1.write('final-report-manifest.json',{'script_sha256':r1.filehash(Path(__file__)),'report_sha256':r1.filehash(output),
        'preserved_first_report_sha256':r1.filehash(r1.HOME/'implementation-report.md'),
        'comparison_sha256':r1.filehash(r1.HOME/'comparison.json'),
        'selected_initial':initial['selected']['mode'],'selected_revision':revised['selected']['mode'],
        'relation_added':added,'relation_removed':removed,'new_calls':0})
    print(r1.canonical({'final_report':str(output),'added':added,'removed':removed,'new_calls':0}))

if __name__=='__main__':main()
