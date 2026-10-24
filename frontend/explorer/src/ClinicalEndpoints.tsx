import { useEffect, useState } from 'react';
import { ArrowUpRight, Info } from 'lucide-react';
import { fetchWithTimeout, offline } from './api';
import { clinicalPValue, clinicalComparison, clinicalDispersion, clinicalResultType } from './clinicalPresentation';

type RecordValue = Record<string, unknown>;
const display = (value: unknown): string => value == null ? '미보고' : typeof value === 'object' ? JSON.stringify(value) : String(value);
const url = (value: unknown): string | undefined => typeof value === 'string' && /^https?:\/\//.test(value) ? value : undefined;

export function ClinicalFacts({ records }: { records: RecordValue[] }) {
  return <div className="clinical-records">{records.map((record, index) => {
    const source = (record.source || record.provenance || {}) as RecordValue;
    const endpoint = (record.endpoint || {}) as RecordValue;
    const sourceRecords = (Array.isArray(record.source_references) ? record.source_references : [source]) as RecordValue[];
    const sources = sourceRecords.filter((item,i,all) => all.findIndex(other => (other.url || other.source_url) === (item.url || item.source_url)) === i);
    const tests = (Array.isArray(record.statistical_tests) ? record.statistical_tests : []) as RecordValue[];
    const href = url(source.url || record.source_url || record.url);
    const observations = Array.isArray(record.observations) ? record.observations as RecordValue[] : [];
    return <details className="clinical-fact" key={String(record.id || index)} open={index===0}>
      <summary><span className="rr-clinical-title">{display(endpoint.label_ko || endpoint.label || record.endpoint_label || record.endpoint_name || record.label || record.endpoint_id)}</span><span className="clinical-pending">검수 대기</span></summary>
      <div className="rr-clinical-body">
      <div className="rr-clinical-meta"><span>임상 대상자 <strong>{display(record.n_people)}</strong></span><span>원문 수치 수록 · 전문가 판정 전</span></div>
      <dl>{['result_type','statistic_type','estimand','comparison','timepoint','value','population_scope','p_value','p_value_scope'].filter(key => record[key] != null).map(key => <div key={key}><dt>{({result_type:'결과 종류',statistic_type:'통계 정의',estimand:'추정 대상',comparison:'비교',timepoint:'시점',value:'보고값',population_scope:'대상 범위',p_value:'P값',p_value_scope:'P값 검정 범위'} as Record<string,string>)[key]}</dt><dd>{key==='comparison'?clinicalComparison(record[key]):key==='result_type'?clinicalResultType(record[key]):display(record[key])}</dd></div>)}</dl>
      {record.n_people_scope != null && <details className="rr-clinical-scope"><summary>대상자 집계 범위</summary><p>{display(record.n_people_scope)}</p></details>}
      {record.source_location != null && <div className="rr-source-location"><span>원문 위치</span><p>{display(record.source_location)}</p></div>}
      {observations.length > 0 && <div className="rr-observations" aria-label="군·시점별 임상 보고값">{observations.map((observation,i) => <div className="rr-observation" key={i}><div><strong>{display(observation.arm ?? observation.group)}</strong><small>{display(observation.time_label ?? observation.timepoint)}</small></div><div><b>{display(observation.reported_value ?? observation.value)}</b><span>{display(observation.unit ?? endpoint.unit ?? record.unit)}</span>{observation.dispersion != null && <small>{clinicalDispersion(observation.dispersion)}</small>}<small>{display(observation.summary_statistic)}</small></div></div>)}</div>}
      {endpoint.unit_caveat != null && <p className="wb-muted">단위 주의: {display(endpoint.unit_caveat)}</p>}
      {tests.length > 0 && <><h4>원문 통계 검정 · 비교 범위</h4>{tests.map((test,i) => <dl key={i}><div><dt>검정 비교</dt><dd>{display(test.comparison_scope)}</dd></div><div><dt>보고 P값·원문 표기</dt><dd>{clinicalPValue(test)}</dd></div><div><dt>다중검정</dt><dd>{display(test.multiplicity)}</dd></div></dl>)}</>}
      {record.interpretation_limit != null && <p className="wb-muted">{display(record.interpretation_limit)}</p>}
      {sources.filter(s => url(s.url || s.source_url)).map((s,i) => <a key={i} className="wb-source-link" href={url(s.url || s.source_url)} target="_blank" rel="noreferrer">원문 · {display(s.label || s.id)} <ArrowUpRight size={13}/></a>)}
      {href && <a className="wb-source-link" href={href} target="_blank" rel="noreferrer">원문·표 위치 확인 <ArrowUpRight size={13}/></a>}
      <details><summary>보고값·통계·출처 전체</summary><pre>{JSON.stringify(record,null,2)}</pre></details>
      </div>
    </details>;
  })}</div>;
}

export default function ClinicalEndpoints({ studyId }: { studyId?: string }) {
  const [data,setData] = useState<RecordValue | null>(null);
  const [error,setError] = useState('');
  useEffect(() => {
    setData(null);setError('');
    if (!studyId || offline) return;
    const controller = new AbortController();
    void fetchWithTimeout(`/api/v1/explorer/studies/${encodeURIComponent(studyId)}/clinical`,{signal:controller.signal},async response => {
      if (!response.ok) throw new Error(`임상지표 조회 실패 (${response.status}) · 결과 부재를 의미하지 않습니다.`);
      return response.json() as Promise<RecordValue>;
    }).then(setData).catch(reason => { if (!controller.signal.aborted) setError((reason as Error).message); });
    return () => controller.abort();
  },[studyId]);
  if (!studyId) return null;
  const records = (Array.isArray(data?.clinical_endpoints) ? data.clinical_endpoints : []) as RecordValue[];
  return <section className="clinical-panel" aria-label="같은 연구의 임상지표">
    <h3>같은 연구의 임상지표</h3><p className="clinical-boundary"><Info size={14}/>유전자 변화와 임상 결과의 공동 수록입니다. 매개·인과 관계나 개인 건강 효과를 의미하지 않습니다.</p>
    {offline ? <p>이 HTML에는 선택한 건강 노드의 자료만 내장됩니다. 추가 연구의 임상지표는 원본 서비스에서 조회하세요.</p> : error ? <p role="alert">{error}</p> : !data ? <p role="status">임상지표·원문 범위 조회 중…</p> : records.length ? <ClinicalFacts records={records}/> : <p>이 연구의 수록된 임상지표가 없습니다. 미측정·미보고 여부는 아래 점검 상태를 확인하세요.</p>}
    {data?.clinical_endpoint_audit != null && <details><summary>자료 확보·결측·검수 상태</summary><pre>{JSON.stringify(data.clinical_endpoint_audit,null,2)}</pre></details>}
  </section>;
}
