"""Read-only post-score diagnostics. Never selects a policy or makes a model request."""
import os
import sys
from pathlib import Path
from collections import Counter
for name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[name]='1'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nutri_literature.repair_v4.common import HOME,ROOT,read,sha,canonical,immutable
from nutri_literature.repair_v4 import representation as rep,learning,pipeline
from nutri_literature import evaluation_v6

OUT=ROOT/'runtime/chatgpt-repair-v4-postscore'

def native_ids(document,kind):
    return {i for e in document.get('entities',[]) if e['kind']==kind for i in e.get('native_ids',[])}

def error_class(document,key):
    gold=evaluation_v6.relation_sets(document,{'relations':[]})[0]
    pair=set(key[1]);chem=native_ids(document,'component');genes=native_ids(document,'gene')
    if any(k[:2]==key[:2] for k in gold):return 'relation_type_mismatch'
    chemical={i for i in pair if i.startswith(('C','D'))}
    gene=pair-chemical
    if not chemical or not gene:return 'unrecognized_endpoint_identifier_shape'
    if not chemical<=chem:return 'chemical_id_not_annotated'
    if not gene<=genes:return 'gene_id_not_annotated'
    return 'annotated_endpoints_pair_not_annotated'

def first_gap(key,paper,chosen,document_bags,observed):
    if key in observed:return 'matched'
    pair=set(key[1]);chemical={i for i in pair if i.startswith(('C','D'))};gene=pair-chemical
    if len(chemical)!=1 or len(gene)!=1:return 'unsupported_gold_identifier_shape'
    wanted={'chemical':next(iter(chemical)),'gene':next(iter(gene))}
    for kind in ('chemical','gene'):
        if not any(m['kind']==kind and wanted[kind] in rep.strict_ids(m,paper['document']) for m in paper['mentions']):
            return kind+'_exact_boundary_not_detected'
    for kind in ('chemical','gene'):
        if not any(m['kind']==kind and wanted[kind] in {r['native_id'] for r in m['candidates']} for m in paper['mentions']):
            return kind+'_id_not_retrieved'
    for kind in ('chemical','gene'):
        if not any(m['kind']==kind and chosen[m['key']]['selected_id']==wanted[kind] for m in paper['mentions']):
            return kind+'_id_not_selected'
    matching=[b for b in document_bags if set(b['ids'])==pair]
    if not matching:return 'no_resolved_mention_pair_bag'
    if not any(b['witnesses'].get(key[2]) for b in matching):return 'no_matching_native_type_witness'
    return 'type_competition_or_training_threshold'

def identity_gaps(paper,policy,chosen):
    records=[]
    for m in paper['mentions']:
        truth=rep.strict_ids(m,paper['document']);old=set(m['old_ids'])
        if not truth or not old or old&truth:continue
        decision=chosen[m['key']];menu={r['native_id'] for r in m['candidates']}
        if decision['selected_id'] in truth:reason='corrected'
        elif not menu&truth:reason='gold_id_missing_from_candidates'
        elif decision.get('proposed_id') is None:reason='nil_ranked_first'
        elif decision.get('proposed_id') not in truth:reason='incorrect_top_rank'
        else:reason='selection_gate_or_existing_id_retention'
        records.append({'pmid':paper['pmid'],'kind':m['kind'],'start':m['start'],'end':m['end'],'text':m['text'],
            'old_ids':sorted(old),'gold_ids':sorted(truth),'selected_id':decision['selected_id'],
            'proposed_id':decision.get('proposed_id'),'ranking_priority':decision.get('ranking_priority'),
            'ranking_margin':decision.get('ranking_margin'),'selection_reason':decision['selection_reason'],
            'reason':reason,'change_policy_enabled':policy['change']['enabled']})
    return records

def main():
    # No development annotations are opened until the fixed full evaluation has finished.
    if not (HOME/'completion-audit.json').exists():
        raise RuntimeError('v4 전체 예측·채점·오프라인 재현 감사 완료 전 사후 진단 금지')
    pipeline.verify_freeze()
    from nutri_literature.repair_v3.runner import gold
    documents,gold_sha=gold();by_id={d['pmid']:d for d in documents}
    comparison=read(HOME/'comparison.json');selected=read(HOME/'selection.json')
    policy=selected['model']['identity']
    relation_rows=[];id_rows=[];errors=[];rejections=Counter();total=Counter()
    for paper in pipeline.load('development'):
        paper['document']=by_id[paper['pmid']]
        prediction=read(HOME/'predictions/combined'/('paper-'+paper['pmid']+'.json'))
        expected,observed,_,_=evaluation_v6.relation_sets(paper['document'],prediction)
        chosen={m['key']:learning.choose(m,policy) for m in paper['mentions']}
        bags=rep.bags(paper,policy)
        for key in sorted(expected):
            stage=first_gap(key,paper,chosen,bags,observed);total[stage]+=1
            relation_rows.append({'key':key,'first_observed_gap':stage})
        id_rows.extend(identity_gaps(paper,policy,chosen))
        rejections.update(paper.get('event_rejections',{}))
        for arm in ('v2','v3','combined'):
            previous=read(HOME/'predictions'/arm/('paper-'+paper['pmid']+'.json'))
            predkeys=evaluation_v6.relation_sets(paper['document'],previous)[1]
            by_key={(paper['pmid'],tuple(sorted((r['entity1'],r['entity2']))),r['predicate']):r for r in previous['relations']}
            for key in sorted(predkeys-expected):
                r=by_key[key]
                errors.append({'arm':arm,'key':key,'annotation_comparison':error_class(paper['document'],key),
                    'source_evidence':r.get('evidence',[]),'endpoints':r.get('endpoints',[]),
                    'evidence_event_key':r.get('evidence_event_key'),'expert_adjudication':False})
    assert len(relation_rows)==154 and total['matched']==comparison['arms']['combined']['metrics']['tp']
    assert sum(total.values())-total['matched']==comparison['arms']['combined']['metrics']['fn']
    values={'gold_sha256':gold_sha,'papers':100,'gold_relations':154,'first_gap_counts':dict(total),
        'relation_records':relation_rows,'known_wrong_existing_id_reasons':dict(Counter(r['reason'] for r in id_rows)),
        'existing_id_records':id_rows,'source_validation_rejections':dict(rejections),
        'error_counts':{arm:dict(Counter(r['annotation_comparison'] for r in errors if r['arm']==arm)) for arm in ('v2','v3','combined')},
        'association_error_counts':{arm:dict(Counter(r['annotation_comparison'] for r in errors if r['arm']==arm and r['key'][2]=='Association')) for arm in ('v2','v3','combined')},
        'false_positive_records':errors,'policy_or_threshold_changed':False,
        'scope':'first observed gaps and annotation comparisons, not causal ablation or biological adjudication',
        'calls':0,'core_comparison_sha256':sha((HOME/'comparison.json').read_bytes()),
        'core_freeze_sha256':sha((HOME/'freeze.json').read_bytes())}
    immutable(OUT/'diagnostics.json',values)
    lines=['# v4 사후 오류 진단','',
        '모델·규칙·임계값을 변경하지 않고 완료된 개발 100편·정답 154건을 분석했다. 추가 추론은 0회다.',
        '첫 관측 누락 단계이며, 해당 단계를 고치면 반드시 복원된다는 인과 실험 결과가 아니다. 주석 부재는 생물학적 오류 확정과 구분한다.','',
        '| 최초 관측 단계 | 건수 |','|---|---:|']
    lines.extend('| '+stage+' | '+str(count)+' |' for stage,count in sorted(total.items()))
    lines+=['','## 기존 잘못된 ID가 남은 이유','']
    lines.extend('- '+reason+': '+str(count)+'건.' for reason,count in sorted(Counter(r['reason'] for r in id_rows).items()))
    lines+=['','정답 위치에 대응하고 기존 ID가 주석 ID와 다른 경우만 이 표에 포함한다. 미주석·미대응 언급은 별도로 해석해야 한다.','',
            '## Association 오탐의 주석 대조 분류','', '| 버전 | 유형 불일치 | 물질 ID 미주석 | 유전자 ID 미주석 | 관계쌍 미주석 |','|---|---:|---:|---:|---:|']
    for arm in ('v2','v3','combined'):
        c=values['association_error_counts'][arm]
        lines.append('| '+arm+' | '+' | '.join(str(c.get(k,0)) for k in ('relation_type_mismatch','chemical_id_not_annotated','gene_id_not_annotated','annotated_endpoints_pair_not_annotated'))+' |')
    report='\n'.join(lines)+'\n';path=OUT/'diagnostics.md'
    if path.exists() and path.read_text(encoding='utf8')!=report:raise RuntimeError('사후 보고서 변경')
    path.write_text(report,encoding='utf8')
    immutable(OUT/'manifest.json',{'source_sha256':sha(Path(__file__).read_bytes()),
        'core_comparison_sha256':values['core_comparison_sha256'],'core_freeze_sha256':values['core_freeze_sha256'],
        'artifacts':{p.name:sha(p.read_bytes()) for p in (OUT/'diagnostics.json',OUT/'diagnostics.md')},
        'model_or_policy_changed':False,'new_model_calls':0})
    print(canonical({'first_gap_counts':dict(total),'known_wrong_id_reasons':values['known_wrong_existing_id_reasons'],
                     'association_error_counts':values['association_error_counts'],'new_model_calls':0}),flush=True)

if __name__=='__main__':main()
