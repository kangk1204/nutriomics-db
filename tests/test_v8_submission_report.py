import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/build_v8_submission_report.py'
spec=importlib.util.spec_from_file_location('build_v8_submission_report',SCRIPT)
report=importlib.util.module_from_spec(spec);spec.loader.exec_module(report)


def put(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf8')
    return path


def score(tp,fp,fn):
    return report.metrics({'tp':tp,'fp':fp,'fn':fn})


def papers(m,total=100):
    return {str(i):{'relation':m if i==1 else score(0,0,0)} for i in range(1,total+1)}


@pytest.fixture
def inputs(tmp_path):
    histories={}
    for label,counts in report.HISTORICAL.items():
        m=score(*counts)
        histories[label]=put(tmp_path/(label+'.json'),{'papers':100,'gold_relations':154,
            'gold_sha256':'a'*64,'arms':{'combined':{'metrics':m,'per_paper':papers(m)}}})
    progress=put(tmp_path/'progress.json',{'observed_at':'2026-10-23T06:44:45+00:00',
        'additional_model_calls':0,'default_engine':'heuristic-v1','sealed_test_opened':False,
        'collection':{'expected_papers':133261,'selected_unique':133261,'stored_selected':9297,
            'live_stored_unique_papers':9384,'live_oa_available_papers':1530,'processed':4400,
            'metadata_status':{'completed':4400,'pending':128861},'metadata_complete':False,
            'internal_snapshot':{'unique_papers':8012,'fulltext_papers':1302,'candidate_events_all_engines':8360,
                'human_approved_visible_events':0,'stored_reports':9}},
        'r2':{'stage':'train-relation','process_alive':True,'predictions_ready':False},
        'v8':{'status':'unavailable','error':'ssh timeout /private/path secret'}})
    return progress,histories


def test_missing_new_evaluations_are_unknown_not_zero_and_denominators_stay_separate(inputs):
    progress,histories=inputs
    data=report.build_payload(progress,history_paths=histories)
    for row in data['relation_comparison'][-2:]:
        assert row['metrics'] is None and row['status']=='미평가' and row['target_passed'] is None
    assert not any(row['target_passed'] for row in data['relation_comparison'][:4])
    rows={row['key']:row for row in data['counts']}
    assert rows['stored_live']['value']==9384 and rows['search_expected']['value']==133261
    assert rows['snapshot_fulltext']['value']==1302 and rows['fulltext_live']['value']==1530
    assert rows['extraction_success']['value'] is None
    assert rows['metadata_completed']['value']==4400
    assert rows['human_approved']['value']==0 and rows['stored_reports']['value']==9
    assert data['collection']['collection_complete'] is False
    assert '학습 실패로 판정하지 않음' in data['pipelines'][-1]['status']
    assert '/private/path' not in json.dumps(data) and 'secret' not in json.dumps(data)


def nlp_receipt():
    names = ['test_v5_active_passive_roles_match_direction[active]',
             'test_v5_active_passive_roles_match_direction[passive]',
             'test_v5_active_passive_roles_match_direction[uncertain]',
             'test_v5_planned_event_never_becomes_observed',
             'test_batch_identity_initializes_lazy_parser_before_registering']
    return {'schema_version':'nutriomics-nlp-validation-v1','observed_at':'2026-10-24T01:41:44+00:00',
            'tests':[{'name':name,'status':'passed','seconds':.1} for name in names],
            'versions':{'spacy':'3.7.5','scispacy':'0.6.2','en-core-sci-sm':'0.5.4',
                        'torch':'2.6.0+cpu','transformers':'4.49.0'},'exit_code':0,
            **{key:'a'*64 for key in ['source_manifest_sha256','test_sha256','log_sha256','junit_sha256']}}


def test_nlp_validation_is_bound_in_all_formats_without_changing_model_metrics(inputs,tmp_path):
    progress,histories=inputs
    base=report.build_payload(progress,history_paths=histories)
    path=put(tmp_path/'nlp.json',nlp_receipt())
    data=report.build_payload(progress,history_paths=histories,nlp_validation_path=path)
    assert data['nlp_validation']['passed']==5
    assert data['completion_assessments']['legacy_nlp_regressions']=='5건 통과'
    assert data['relation_comparison']==base['relation_comparison']
    source=next(s for s in data['sources'] if s['role']=='legacy_nlp_validation')
    assert source['sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    assert '구버전 NLP 회귀 검사' in report.markdown(data)
    assert 'test_v5_planned_event_never_becomes_observed' in report.html_report(data)
    assert 'NLP 검사' in report.csv_report(data)


@pytest.mark.parametrize('issue',['duplicate','wrong_version','missing_test','wrong_exit','wrong_hash'])
def test_incomplete_or_inconsistent_nlp_receipt_does_not_claim_five_passes(tmp_path,issue):
    data=nlp_receipt()
    if issue=='duplicate':data['tests'][0]=dict(data['tests'][1])
    if issue=='wrong_version':data['versions']['spacy']='0.0.0'
    if issue=='missing_test':data['tests'].pop()
    if issue=='wrong_exit':data['tests'][0]['status']='failed'
    if issue=='wrong_hash':data['test_sha256']='not a hash'
    with pytest.raises(ValueError):report.nlp_validation(put(tmp_path/'nlp.json',data),[])


def test_current_extractions_are_union_not_v8_results_and_queue_is_not_failed_papers(inputs):
    progress,histories=inputs
    value=json.loads(progress.read_text());c=value['collection']
    c.update({'extraction_success_papers':10,'live_current_extraction_papers_by_engine':{
        'heuristic-v1':8,'heuristic-v3':7},'live_abstract_papers':9100,
        'live_queue_counts':[{'stage':'extract','status':'failed','queue_items':20},
                             {'stage':'fulltext','status':'pending','queue_items':400}]})
    put(progress,value);data=report.build_payload(progress,history_paths=histories)
    rows={r['key']:r for r in data['counts']}
    assert rows['extraction_success']['value']==10
    assert sum(rows['extraction_engine:'+engine]['value'] for engine in ['heuristic-v1','heuristic-v3'])==15
    assert 'v8 성능 아님' in rows['extraction_success']['scope']
    assert '현재 원문 ID·해시 일치' in rows['extraction_success']['scope']
    assert rows['abstract_live']['value']==9100
    assert rows['queue:extract:failed']['value']==20 and rows['queue:extract:failed']['unit']=='행'
    assert '실패 문헌 수가 아님' in rows['queue:extract:failed']['scope']
    assert rows['extraction_failed']['value'] is None
    assert data['relation_comparison'][-1]['status']=='미평가'
    page=report.html_report(data)
    assert '엔진별 추출 문헌은 서로 중복될 수 있어 합산하지 않는다' in page
    assert '대기열 작업 · extract · failed' in page


def test_rejects_duplicate_queue_groups_and_impossible_engine_union():
    with pytest.raises(ValueError,match='합집합보다'):
        report.collection_rows({'collection':{'extraction_success_papers':3,
            'live_current_extraction_papers_by_engine':{'heuristic-v1':4}}})
    item={'stage':'extract','status':'failed','queue_items':10}
    with pytest.raises(ValueError,match='중복'):
        report.collection_rows({'collection':{'live_queue_counts':[item,item]}})


def test_recomputes_metrics_rejects_mismatches_and_wrong_baseline(inputs):
    assert score(65,63,89)['f1']==pytest.approx(.4609929078)
    with pytest.raises(ValueError,match='재계산'):report.metrics({'tp':65,'fp':63,'fn':89,'precision':.85})
    progress,histories=inputs
    data=json.loads(histories['v3'].read_text());data['gold_sha256']='b'*64;put(histories['v3'],data)
    with pytest.raises(ValueError,match='해시가 다릅니다'):report.build_payload(progress,history_paths=histories)


def test_rejects_same_count_different_paper_sets(inputs):
    progress,histories=inputs
    data=json.loads(histories['v4'].read_text());p=data['arms']['combined']['per_paper'];p['101']=p.pop('100');put(histories['v4'],data)
    with pytest.raises(ValueError,match='논문 집합'):report.build_payload(progress,history_paths=histories)


def test_real_new_evaluation_requires_matching_cohort_counts_and_hash(inputs,tmp_path):
    progress,histories=inputs;m=score(65,11,89)
    evaluation=put(tmp_path/'evaluation.json',{'papers':100,'gold_relations':154,'gold_sha256':'a'*64,
        'scores':{'nutriomics-evidence-v8':{'metrics':m,'per_paper':papers(m)}}})
    data=report.build_payload(progress,history_paths=histories,v8_evaluation=evaluation)
    assert data['relation_comparison'][-1]['target_passed'] is True
    assert data['completion_assessments']['full_system_completion']=='판정 보류'
    assert data['completion_assessments']['nutrition_domain_validation']=='미측정'
    assert data['completion_assessments']['seven_day_same_artifact_operation']=='미검증'
    assert data['relation_comparison'][-1]['metrics']['precision']==pytest.approx(65/76)
    assert data['relation_comparison'][-1]['source_sha256']==hashlib.sha256(evaluation.read_bytes()).hexdigest()
    d=json.loads(evaluation.read_text());d['gold_relations']=153;put(evaluation,d)
    with pytest.raises(ValueError,match='기준선과 다릅니다'):report.build_payload(progress,history_paths=histories,v8_evaluation=evaluation)


def test_target_is_conjunctive_and_internal():
    assert report.target_passed(score(65,11,89))
    assert not report.target_passed(score(64,0,90))
    assert not report.target_passed(score(65,12,89))
    assert not report.TARGET['agreement_target_verified']


def test_output_is_hash_bound_immutable_and_html_escaped(inputs,tmp_path):
    progress,histories=inputs;data=report.build_payload(progress,history_paths=histories)
    data['pipelines'][0]['stage']='<script>alert(1)</script>'
    directory=tmp_path/'report';manifest=report.write_report(data,directory)
    assert set(manifest['files'])=={'report.md','report.html','report.csv','report.json'}
    for name,digest in manifest['files'].items():assert hashlib.sha256((directory/name).read_bytes()).hexdigest()==digest
    page=(directory/'report.html').read_text(encoding='utf8')
    assert '<script>alert' not in page and '&lt;script&gt;' in page
    assert '미평가' in page and '협약' in page and '미측정' in page
    with pytest.raises(FileExistsError):report.write_report(data,directory)


def test_cannot_open_sealed_gold_inputs(tmp_path,monkeypatch):
    path=tmp_path/'sealed'/'gold.json'
    def forbidden(self):raise AssertionError('label file opened')
    monkeypatch.setattr(Path,'read_bytes',forbidden)
    with pytest.raises(ValueError,match='정답 라벨'):report.load_receipt(path,'anything',[])


def test_collection_complete_needs_explicit_metadata_and_membership_conditions():
    values={'expected_papers':10,'selected_unique':10,'stored_selected':10,'metadata_complete':False,'missing_memberships':0}
    assert not report.collection_rows({'collection':values})[1]['collection_complete']
    values['metadata_complete']=True
    assert report.collection_rows({'collection':values})[1]['collection_complete']
    values['missing_memberships']=1
    assert not report.collection_rows({'collection':values})[1]['collection_complete']


def test_frozen_denominator_and_live_ledger_survive_restart_without_claiming_complete():
    fixed={'expected_papers':100,'selected_unique':100,'stored_selected':12,'missing_count':88,
           'search_complete':True,'manifest_sha256':'a'*64,'query_version':'b'*64}
    current={'stage':'search','expected_papers':None,'processed':0,'contract_sha256':'b'*64,
             'frozen_search_inventory':fixed,'live_ingest_metadata_status':{'completed':10,'pending':90}}
    rows,state=report.collection_rows({'collection':current});counts={r['key']:r for r in rows}
    assert counts['search_expected']['value']==100 and counts['selected_stored']['value']==12
    assert counts['metadata_completed']['value']==10 and counts['metadata_pending']['value']==90
    assert counts['metadata_failed']['value']==0 and not state['collection_complete']
    assert state['frozen_search_manifest_sha256']=='a'*64
    current['frozen_search_inventory']={**fixed,'query_version':'wrong'}
    with pytest.raises(ValueError,match='계약'):report.collection_rows({'collection':current})


def test_training_profile_counts_are_progress_not_model_performance():
    states=report.pipeline_states({'v8':{'nutriomics-evidence-v8-run3':{
        'status':'running','stage':'train-ner','supervisor_alive':True,'completed_ner_models':3,
        'completed_ner_profiles':2,'completed_id_outer_folds':0,'completed_relation_outer_folds':0,
        'io_memory_probe_peak_rss_bytes':4*1024**3}}})
    value=states[-1]
    assert value['status']=='진행' and value['predictions_ready'] is False
    assert 'NER 학습 모델 3개 완료' in value['progress_detail']
    assert '자동 후보 profile 2개 완료' in value['progress_detail']
    assert 'IO 재현 최고 RSS 4.00 GiB · 모델 성능 아님' in value['progress_detail']


def test_resource_guard_stop_is_explicit_even_with_completed_profiles():
    value=report.pipeline_states({'v8':{'nutriomics-evidence-v8-run3':{
        'status':'resource_budget_exceeded','stage':'train-ner','supervisor_alive':False,
        'completed_ner_models':5,'completed_ner_profiles':5,'peak_child_rss_bytes':9*1024**3}}})[-1]
    assert '자원 한도 초과로 중단' in value['status']
    assert value['predictions_ready'] is False
    assert '중단 전 프로세스 최고 RSS 9.00 GiB' in value['progress_detail']


def test_disabled_r2_id_policy_is_not_reported_as_successful_correction():
    policy={kind:{action:{'enabled':False} for action in ('new','change')}
            for kind in ('gene','chemical')}
    state=report.pipeline_states({'r2':{'stage':'train-relation','process_alive':True,
                                      'id_training':{'policy':policy}}})[0]
    assert state['status']=='진행'
    assert state['progress_detail'].count('비활성')==4
    assert '100편 성능과 별도' in state['progress_detail']
    assert not state['predictions_ready']


def test_spare_gpu_native_completion_never_claims_main_cohort_evaluation():
    states = report.pipeline_states({'spare_gpu': {'host': '100.68.188.102',
        'pipeline': {'status': 'native_training_complete', 'stage': 'freeze', 'process_alive': False},
        'gpu': 'RTX 3080 Ti Laptop, 16384 MiB'}})
    spare = states[-1]
    assert '보조 관계·이벤트 학습 완료' in spare['status']
    assert '100편 평가와 별도' in spare['status']
    assert 'BioRED 100편 평가와 별도' in spare['progress_detail']
    assert spare['predictions_ready'] is False


def test_public_train_ner_diagnostic_does_not_replace_relation_evaluation(inputs):
    progress,histories=inputs
    data=json.loads(progress.read_text())
    data['v8']={'run4':{'stage':'train-id','status':'failed','supervisor_alive':False,
        'public_training_ner_diagnostics':{'official_training_only':True,
            'independent_final_performance':False,'training_papers':415,
            'boundary_metrics_on_public_train_OOF':{'gene_ner_only':score(70,20,30)}}}}
    put(progress,data)
    payload=report.build_payload(progress,history_paths=histories)
    assert payload['upstream_ner_training'][0]['papers']==415
    assert payload['relation_comparison'][-1]['metrics'] is None
    table=next(t for t in report.tables(payload) if '공식 학습 OOF' in t[0])
    assert len(table[2][0])==len(table[1])==9
    assert '100편 관계 평가와 별도' in report.markdown(payload)
    data['v8']['run4']['public_training_ner_diagnostics']['independent_final_performance']=True
    put(progress,data)
    with pytest.raises(ValueError,match='평가 범위'):
        report.build_payload(progress,history_paths=histories)


def test_prior_connection_record_never_replaces_current_unknown_status(inputs,tmp_path):
    progress,histories=inputs
    prior=put(tmp_path/'previous.json',{'observed_at':'2026-10-23T06:30:00+00:00',
        'v8':{'nutriomics-evidence-v8-run3':{'stage':'train-ner','status':'running','supervisor_alive':True,
            'completed_ner_models':4,'completed_ner_profiles':3,'prediction_manifest_exists':False}}})
    payload=report.build_payload(progress,history_paths=histories,previous_verified_progress=prior)
    assert '현재 상태 확인 불가' in payload['pipelines'][-1]['status']
    assert payload['previous_pipeline_observations'][0]['observed_at_kst']=='2026-10-23T15:30:00+09:00'
    assert '4개 완료' in payload['previous_pipeline_observations'][0]['progress_detail']
    assert payload['relation_comparison'][-1]['metrics'] is None
    assert '현재 상태와 별도' in report.markdown(payload)
    data=json.loads(prior.read_text());data['observed_at']='2026-10-23T07:30:00+00:00';put(prior,data)
    with pytest.raises(ValueError,match='늦습니다'):
        report.build_payload(progress,history_paths=histories,previous_verified_progress=prior)


def test_pipeline_completion_proof_outranks_dead_process_without_claiming_evaluation(inputs):
    states=report.pipeline_states({'r2':{'stage':'predict','process_alive':False,'predictions_ready':True},
        'v8':{
            'v8-complete':{'stage':'predict','supervisor_alive':False,'prediction_manifest_exists':True},
            'v8-clinical':{'stage':'clinical-role','supervisor_alive':False,'status':'clinical_training_complete'},
            'v8-frozen':{'stage':'freeze','supervisor_alive':False,'status':'completed'},
            'v8-waiting':{'stage':'dependency','supervisor_alive':True,'status':'waiting_for_r2'}}})
    by_name={s['model']:s for s in states}
    assert by_name['r2']['status']=='100편 예측 저장 완료·평가 별도'
    assert by_name['v8-complete']['status']=='100편 예측 저장 완료·평가 별도'
    assert by_name['v8-clinical']['status']=='임상 모델 학습 완료·평가 별도'
    assert by_name['v8-frozen']['status']=='작업 완료·성능 검증 별도'
    assert by_name['v8-waiting']['status']=='대기'
    progress,histories=inputs
    original=json.loads(progress.read_text());original['r2']['predictions_ready']=True;original['r2']['process_alive']=False;put(progress,original)
    payload=report.build_payload(progress,history_paths=histories)
    assert payload['relation_comparison'][-2]['status']=='미평가'
    assert payload['completion_assessments']['full_system_completion']=='판정 보류'


def test_clinical_native_diagnostic_requires_saved_report_comparison_and_ci(tmp_path):
    root=tmp_path/'clinical';root.mkdir();details=[];rows=[]
    for corpus in ['ebmnlp','evidenceoutcomes']:
        m=score(50,30,50);baseline=score(49,30,51)
        values=[{'pmid':str(i),'spans_outcomes':m if i==1 else score(0,0,0)} for i in range(1,201)]
        put(root/'predictions'/corpus/'report.json',{'papers':200,'metrics':{'spans_outcomes':m},'per_paper':values})
        comparison={}
        for label,code in [('v6','a'),('pio_only','b')]:
            value={'baseline':baseline,'candidate':m,'baseline_report_sha256':code*64,
                'same_cohort_and_gold_token_denominators':True,
                'difference':{'f1':m['f1']-baseline['f1']},
                'paired_bootstrap_f1_difference_ci95':{'iterations':2000,'lower':-.01,'upper':.04}}
            put(root/'predictions'/corpus/('comparison-'+code*12+'.json'),value);comparison[label]=value
        details.append({'corpus':corpus,'metrics':m,'same_input_gold_pmids_denominators_verified':True,'comparison':comparison})
        rows.append({'corpus':corpus,'papers':200})
    path=put(root/'summary.json',{'independent_final_performance':False,'rows':rows,'details':details})
    result,status=report.clinical_rows(path,[])
    assert len(result)==2 and all(not r['target_plus5pp_vs_v6'] for r in result)
    assert all(not r['comparisons']['v6']['difference_excludes_zero'] for r in result)
    assert all(r['full_ico_group_dose_time']=='미평가' for r in result)
    mismatch=root/'predictions/ebmnlp'/('comparison-'+'a'*12+'.json')
    d=json.loads(mismatch.read_text());d['paired_bootstrap_f1_difference_ci95']['iterations']=10;put(mismatch,d)
    with pytest.raises(ValueError,match='비교 원본'):report.clinical_rows(path,[])
