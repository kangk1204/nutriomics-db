import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowRight, BookOpen, Check, ChevronDown, ClipboardList, ExternalLink, FileSearch, FlaskConical, Info, Link2, LoaderCircle, Microscope, Network, RefreshCw, SlidersHorizontal } from 'lucide-react';
import { frozenScientificContext, getScientificMatches, offline } from './api';
import LazyDetails from './LazyDetails';
import type { ApplicabilityCondition, Entity, EvidenceEdge, GraphPath, ReferenceContext, ScientificContext, ScientificContrast, ScientificEvidence as EvidenceItem, ScientificMatchResult } from './types';

const number = new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 6 });
const statusLabels = { match: '일치', mismatch: '차이 있음', unknown: '미확인' };
const groupLabels: Record<string, string> = { direct_context: '해당 중재 기록', same_analyte: '같은 성분의 연구', related: '관련 제품·성분 연구' };
const resultLabels: Record<string, string> = { existing_analysis: '공개 결과 재분석', published_context_only: '논문·연구 맥락', unresolved_visit_mapping: '방문 대응 미확인' };
const dimensionLabels: Record<string, string> = { taxon: '종', study_id: '연구', experiment_type: '실험 유형', molecular_identity: '분자 동일성', analyte: '분석 성분', food_form: '음식·제품 형태', food_formulation: '음식·제품 동일성', formulation: '투여 형태', exposure: '노출', dose: '용량', amount: '용량', intake_g: '음식 중량', duration: '섭취 기간', duration_days: '섭취 기간', regimen_duration_days: '반복 섭취 기간', response_time_h: '반응 시간', response_time: '반응 시간', timepoint: '측정 시점', tissue: '조직', species: '종', population: '대상군', frequency: '섭취 빈도', route: '투여 경로', comparator: '대조 조건', contrast: '분석 대비' };
const valueLabels: Record<string, string> = { single: '한 번 섭취', single_meal: '한 번 섭취', daily: '매일 섭취', per_day: '매일 섭취', blood: '혈액', whole_blood: '전혈', plasma: '혈장', serum: '혈청', pending: '전문가 검수 대기', pending_expert_review: '전문가 검수 대기', candidate_label: '이름 대응 후보', source_identifier_match: '원본 식별자 대응', source_feature: '연구의 원본 특성', not_reviewed: '전문가 검수 미완료', curator_checked: '출처 대조 완료', full_text: '원문 확인', official_dataset: '공식 자료 확인', curator_paraphrase: '출처 기반 요약', unknown: '미확인' };
const safeUrl = (value: unknown) => typeof value === 'string' && /^https?:\/\//.test(value) ? value : null;
function displayNumber(value: number) { return value !== 0 && Math.abs(value) < 0.001 ? value.toExponential(3) : number.format(value); }
function text(value: unknown, empty = '미확인'): string {
  if (value == null || value === '') return empty;
  if (typeof value === 'number') return Number.isFinite(value) ? displayNumber(value) : empty;
  if (typeof value === 'string') return valueLabels[value] || value;
  if (Array.isArray(value)) return value.map(item => text(item)).join(' · ');
  if (typeof value === 'object') {
    const record = value as Record<string, unknown>;
    if (record.amount != null || record.value != null) return `${record.label ? text(record.label) + ' · ' : ''}${text(record.amount ?? record.value)} ${text(record.unit, '')}`.trim();
    return Object.entries(record).map(([key, item]) => `${dimensionLabels[key] || key}: ${text(item)}`).join(' · ');
  }
  return String(value);
}
function SourceDetails({ records }: { records: Record<string, unknown>[] }) {
  const locations = [...new Set(records.map(source => text(source.source_location, '')).filter(Boolean))];
  return <div className="scientific-source-section">
    <h4><BookOpen size={13} />원문·근거 위치</h4>
    {locations.length > 0 && <p className="source-locator-preview">{locations[0]}{locations.length > 1 && <span> · 추가 위치 {locations.length - 1}개</span>}</p>}
    <LazyDetails className="scientific-sources" summary={<>출처 기록·버전·접근 범위 <span>{records.length}</span><ChevronDown size={13} /></>}>{() => <div>
      {records.length ? records.map((source, index) => {
        const url = safeUrl(source.url) || safeUrl(source.source_url);
        return <article className="scientific-source" key={String(source.id || index)}><div><strong>{text(source.label || source.name || source.id)}</strong>{url && <a href={url} target="_blank" rel="noreferrer">원문 <ExternalLink size={12} /></a>}</div><dl><dt>근거 위치</dt><dd>{text(source.source_location)}</dd><dt>자료 버전</dt><dd>{text(source.version)}</dd><dt>접근 범위</dt><dd>{text(source.access_level)}</dd>{source.hash_scope != null && <><dt>해시 대상</dt><dd>{text(source.hash_scope)}</dd></>}</dl></article>;
      }) : <p className="scientific-gap">노출할 수 있는 원문 위치 정보가 없습니다.</p>}
      <p className="scientific-fineprint">출처 해시는 자료 이력을 확인하는 정보입니다. 분자 동일성이나 연구 결론의 검증 수준을 대신하지 않습니다.</p>
    </div>}</LazyDetails>
  </div>;
}
function Conditions({ rows }: { rows: ApplicabilityCondition[] }) {
  return <div className="conditions-section"><h5><SlidersHorizontal size={13} />입력 조건과 연구 조건</h5>{!rows.length ? <p className="scientific-gap">비교 가능한 조건 정보가 없습니다.</p> : <div className="conditions-scroll" tabIndex={0} role="region" aria-label="조건 비교 표, 가로로 이동 가능"><table className="conditions-table"><thead><tr><th>조건</th><th>현재 입력</th><th>실제 연구</th><th>대응</th></tr></thead><tbody>{rows.map((condition, index) => <tr key={`${condition.dimension}-${index}`}><th scope="row">{dimensionLabels[condition.dimension] || condition.dimension.replaceAll('_', ' ')}</th><td>{text(condition.input, '미입력')}</td><td>{text(condition.study)}</td><td><span className={`condition-status ${condition.status || 'unknown'}`}>{statusLabels[condition.status] || '미확인'}</span><details className="condition-reason"><summary>판정 근거</summary><p>{condition.reason || '대응 이유 미확인'}</p></details></td></tr>)}</tbody></table></div>}</div>;
}
export function ScientificEdgeContext({ scientific }: { scientific: EvidenceEdge['scientific'] }) {
  if (!scientific) return <p className="muted-note">이 release에서 해당 관계의 과학적 맥락 비교는 미제공입니다.</p>;
  const rows: ApplicabilityCondition[] = Array.isArray(scientific.conditions) ? scientific.conditions : Object.entries(scientific.conditions || {}).map(([dimension, study]) => ({dimension, study, status: 'unknown', reason: '관계에 기록된 조건입니다. 현재 입력과의 대응 판정은 제공되지 않았습니다.'}));
  return <div className="edge-scientific-context"><div><strong>성분·분자 대응</strong><p>{scientific.mapping_identity?.label || '식별자 대응 미확인'}</p><small>{scientific.mapping_identity?.note || '관계 수록 사실만으로 분자 동일성을 확인하지 않습니다.'}</small></div><div><strong>연구 근거 종류</strong><p>{scientific.study_evidence?.label || '근거 종류 미확인'}</p><small>{scientific.study_evidence?.original_design || '원본 설계 미확인'}</small></div><Conditions rows={rows} />{scientific.limitations?.map((limitation, index) => <p className="muted-note" key={index}>{limitation}</p>)}</div>;
}
export function ScientificPathContext({ path }: { path?: GraphPath }) {
  if (!path?.scientific) return null;
  const scientific = path.scientific;
  return <details className="path-scientific-details"><summary><Microscope size={13} />경로를 구성하는 근거의 조건 비교</summary><p>여러 출처를 이어 만든 가설입니다. 아래 조건이 일치해도 전체 경로의 사람 효과가 검증된 것은 아닙니다.</p>{scientific.compatibility?.length ? <div className="conditions-scroll" tabIndex={0} role="region" aria-label="경로 구성 근거의 조건 비교"><table className="conditions-table"><thead><tr><th>조건</th><th>연결별 원본 값</th><th>대응</th></tr></thead><tbody>{scientific.compatibility.map((condition, index) => <tr key={`${condition.dimension}-${index}`}><th scope="row">{dimensionLabels[condition.dimension] || condition.dimension}</th><td>{condition.values.map(value => text(value)).join(' → ')}</td><td><span className={`condition-status ${condition.status}`}>{statusLabels[condition.status as keyof typeof statusLabels] || '미확인'}</span><details className="condition-reason"><summary>판정 근거</summary><p>{condition.reason}</p></details></td></tr>)}</tbody></table></div> : <p>구성 근거의 조건 대응 정보가 없습니다.</p>}{scientific.limitations?.map((limitation, index) => <p key={index}>{limitation}</p>)}</details>;
}
const fieldLabels: Record<string, string> = { effect_estimate: '효과량', standard_error: 'SE', q_value: 'FDR (q)', p_value: 'P 값', effect_unit: '효과 단위', unit: '단위', contrast: '대비', n_people: '독립 대상자', n_samples: '원본 시료수', contrast_assay_count: '대비 시료수', n_people_scope: '대상자 범위', n_samples_scope: '시료 범위', analysis_version: '분석 버전', multiple_testing: '다중검정', sample_scope: '포함 시료', feature_id: '특성 ID', feature_label: '특성 이름', feature_type: '분석 유형', feature_mapping: '특성 대응', annotation_gene: '프로브 주석 유전자', genome_build: '게놈 버전', gene_mapping_note: '주석 해석' };
function ResultTable({ rows, total, kind }: { rows: Record<string, unknown>[]; total?: number | null; kind: string }) {
  const [expanded, setExpanded] = useState(false);
  if (!rows.length) return <p className="scientific-gap">{kind === '임상' ? '연결된 임상지표 결과가 없습니다. 오믹스 변화로 임상 효과를 대신하지 않습니다.' : '현재 선택에 대한 특성별 결과가 없습니다. 결과 미보고는 변화량 0이 아닙니다.'}</p>;
  const shown = rows.slice(0, expanded ? rows.length : 6);
  return <><div className="results-scroll" tabIndex={0} role="region" aria-label="관찰 결과 표, 가로로 이동 가능"><table className="scientific-results"><thead><tr><th>{kind === '임상' ? '건강지표' : '특성'}</th><th>효과량</th><th>FDR</th><th>세부 근거</th></tr></thead><tbody>{shown.map((row, index) => {
    const featureMapping = row.feature_mapping as Record<string, unknown> | undefined;
    const estimate = row.effect_estimate ?? row.effect_size ?? row.delta_beta ?? row.log_fc;
    const label = row.outcome ?? row.endpoint ?? row.feature_label ?? row.feature_id ?? row.name ?? row.id;
    return <tr key={String(row.id || row.feature_id || index)}><th scope="row">{text(label)}{featureMapping && <small className="feature-mapping-flag">{featureMapping.status === 'candidate_label' ? '이름 대응 후보' : text(featureMapping.status)}{featureMapping.reason != null && <span title={text(featureMapping.reason)}><Info size={10} /></span>}</small>}</th><td><strong>{text(estimate, '—')}</strong><small>{text(row.effect_unit ?? row.unit, '단위 미확인')}{row.standard_error != null ? ` · SE ${text(row.standard_error)}` : ''}</small></td><td>{text(row.q_value ?? row.fdr ?? row.adjusted_p_value, '—')}<small>{row.p_value == null ? 'P 미보고' : `P ${text(row.p_value)}`}</small></td><td><LazyDetails summary="기록">{() => <dl>{Object.entries(row).filter(([key]) => key !== 'metadata').map(([key, value]) => <div key={key}><dt>{fieldLabels[key] || key.replaceAll('_', ' ')}</dt><dd>{text(value)}</dd></div>)}{row.metadata != null && <div><dt>원본 metadata</dt><dd>{text(row.metadata)}</dd></div>}</dl>}</LazyDetails></td></tr>;
  })}</tbody></table></div><div className="result-coverage"><span>화면 {shown.length}행 / 응답 {rows.length}행{total != null && ` · 원본 결과 ${number.format(total)}행`}</span>{rows.length > 6 && <button onClick={() => setExpanded(!expanded)}>{expanded ? '간단히 보기' : '받은 결과 모두 보기'}</button>}</div></>;
}
function ContrastCard({ contrast }: { contrast: ScientificContrast }) {
  const source = safeUrl(contrast.source_url);
  const allResults = safeUrl(contrast.all_results_url) || (!offline && contrast.all_results_url?.startsWith('/api/v1/') ? contrast.all_results_url : null);
  const roleNote = contrast.exposure_role_note || (contrast.matched_exposure_role === 'shared_in_both_arms' ? '이 노출은 비교 양쪽에 공통으로 포함됩니다. 대비 결과를 이 성분의 독립 효과로 해석하지 않습니다.' : '');
  const scopeConflicts = contrast.analysis_context?.sample_scope_conflicts;
  const hasScopeConflicts = scopeConflicts != null && typeof scopeConflicts === 'object' && Object.keys(scopeConflicts).length > 0;
  return <LazyDetails className="scientific-contrast" summary={<>
    <div className="contrast-heading"><strong>{contrast.label || contrast.comparison || contrast.contrast || '분석 대비'}</strong><span>{contrast.series_accession || '자료 ID 미확인'}</span><ChevronDown size={14} /></div>
    <p className="contrast-comparison-preview">{contrast.comparison || contrast.contrast || '비교 조건 미확인'}</p>
    <div className="contrast-counts"><span title={contrast.n_people_scope}>독립 대상자 <b>{text(contrast.n_people, '—')}</b></span><span title={contrast.n_samples_scope}>원본 시료수 <b>{text(contrast.n_samples, '—')}</b></span><span>결과 <b>{text(contrast.effect_total, '미확인')}행</b></span></div>
    {contrast.applicability?.length ? <div className="contrast-condition-preview">{contrast.applicability.map((condition, index) => <span key={`${condition.dimension}-${index}`} title={`현재 입력: ${text(condition.input, '미입력')} · 연구: ${text(condition.study)} · ${condition.reason}`}>{dimensionLabels[condition.dimension] || condition.dimension} <b className={`condition-status ${condition.status}`}>{statusLabels[condition.status] || '미확인'}</b></span>)}</div> : null}
    {roleNote && <p className="contrast-role-preview">{roleNote}</p>}
    {hasScopeConflicts && <p className="contrast-role-preview">시료 범위의 출처별 수치가 일치하지 않습니다. 충돌한 항목은 미확인으로 유지하며, 출처별 값은 분석 범위에서 확인합니다.</p>}
    <span className="contrast-open-hint">조건 · 효과량 · FDR · 출처</span>
  </>}>{() => <div className="contrast-body">
    <div className="contrast-counts">{contrast.n_people_scope && <small>{contrast.n_people_scope}</small>}{contrast.n_samples_scope && <small>{contrast.n_samples_scope}</small>}</div>
    {contrast.design_classification && <p className="scientific-fineprint">대비 설계: {contrast.design_classification}</p>}
    {roleNote && <p className="shared-exposure-note"><Info size={13} />{roleNote}</p>}
    {contrast.comparison_exposures?.length ? <LazyDetails className="analysis-context" summary="실제 비교식·제품 구성 확인">{() => <dl>{contrast.comparison_exposures!.map((exposure, index) => <div key={String(exposure.id || index)}><dt>{text(exposure.label || exposure.id, `비교 노출 ${index + 1}`)}</dt><dd>{text(exposure)}</dd></div>)}</dl>}</LazyDetails> : null}
    {contrast.applicability && <Conditions rows={contrast.applicability} />}
    {contrast.effect_preview && <p className="scientific-fineprint">원본 특성 이름 순으로 표시한 미리보기입니다. 유의성이나 건강 효과 순위가 아닙니다.</p>}
    {contrast.gene_filter_applied && <p className="scientific-fineprint">선택 유전자 대응 결과 {contrast.effect_total == null ? '미확인' : `${number.format(contrast.effect_total)}행`} · 이 대비의 전체 기존 결과 {contrast.source_effect_total == null ? '미확인' : `${number.format(contrast.source_effect_total)}행`}. 대응 결과가 없다고 연구 전체 결과가 없는 것은 아닙니다.</p>}
    {contrast.effects_status && contrast.effects_status !== 'available' ? <p className="scientific-gap" role="status">{contrast.effects_status === 'query_timeout' ? '결과 조회 제한시간을 초과했습니다.' : contrast.effects_status === 'analysis_key_mismatch' ? '원본 결과와 분석 키가 일치하지 않습니다.' : '원본 결과를 현재 조회할 수 없습니다.'} 근거 부재나 결과 0건을 의미하지 않습니다.</p> : <ResultTable rows={contrast.effects || []} total={contrast.effect_total} kind="오믹스" />}
    {contrast.analysis_context && <LazyDetails className="analysis-context" summary="분석 범위·다중검정·버전 확인">{() => <dl>{Object.entries({ ...contrast.analysis_context, analysis_version: contrast.analysis_version }).map(([key, value]) => <div key={key}><dt>{fieldLabels[key] || key.replaceAll('_', ' ')}</dt><dd>{text(value)}</dd></div>)}</dl>}</LazyDetails>}
    <div className="contrast-links">{source && <a href={source} target="_blank" rel="noreferrer">원본 연구 <ExternalLink size={11} /></a>}{allResults && <a href={allResults} target="_blank" rel="noreferrer">전체 결과 <ExternalLink size={11} /></a>}<code>{contrast.analysis_version || '분석 버전 미확인'}</code></div>
  </div>}</LazyDetails>;
}
export function ScientificStudyCard({ item }: { item: EvidenceItem }) {
  const exposure = item.exposure || {}; const identity = item.mapping_identity || {}; const study = item.study_evidence || {};
  const review = study.expert_review_status;
  const commonConditions = (item.applicability || []).filter(condition => !condition.contrast_id);
  return <article className="human-evidence-card"><header><div className="evidence-card-tags"><span className="evidence-group">{groupLabels[item.evidence_group || ''] || '대응 관계 미확인'}</span><span>{resultLabels[item.result_status || ''] || '결과 상태 미확인'}</span></div><h3>{item.study_label || item.study_id}</h3><div className="study-identifiers"><code>{item.study_id}</code><code className="evidence-id" title={item.id}>{item.id}</code></div><div className="study-summary-line"><span>배정량 <b>{exposure.amount == null ? '미확인' : `${text(exposure.amount)} ${text(exposure.unit, '단위 미확인')}`}</b></span><span>조직 <b>{text(study.tissue)}</b></span><span>기간 <b>{exposure.duration_days == null ? '미확인' : `${text(exposure.duration_days)}일`}</b></span></div></header><div className="three-evidence-axes"><div><span className="axis-heading"><Link2 size={12} />성분·분자 대응</span><strong>{identity.label || '식별자 대응 미확인'}</strong><small>{identity.note || '대응 근거를 별도로 확인해야 합니다.'}</small><LazyDetails summary="대응 방법">{() => <><p>{identity.method || '방법 미확인'}</p><p>{identity.quantity_transfer_allowed === true ? '이 기록에 대해 정량 대응을 허용한 근거가 있습니다.' : '이 대응만으로 성분량을 다른 제품·분자 용량으로 환산하지 않습니다.'}</p></>}</LazyDetails></div><div><span className="axis-heading"><Microscope size={12} />연구 근거 종류</span><strong>{study.label || '연구 근거 종류 미확인'}</strong><small>{study.original_design || '원본 설계 미확인'}</small><span className="review-note">{review ? text(review) : '전문가 검수 상태 미확인'}</span></div><div><span className="axis-heading"><SlidersHorizontal size={12} />입력 조건 일치</span><strong>조건별로 비교</strong><small>용량·기간·조직·대상군을 독립적으로 확인합니다.</small><span className="axis-note">종합 효능 점수 없음</span></div></div><section className="actual-exposure"><h4><ClipboardList size={13} />연구에서 실제로 사용한 노출</h4><strong>{exposure.label || '노출 명칭 미확인'}</strong><dl><div><dt>형태</dt><dd>{text(exposure.formulation)}</dd></div><div><dt>제품 설명</dt><dd>{text(exposure.actual_product_description)}</dd></div><div><dt>배정·투여량</dt><dd>{exposure.amount == null ? '미확인' : `${text(exposure.amount)} ${text(exposure.unit, '단위 미확인')}`}</dd></div><div><dt>용량 기준</dt><dd>{text(exposure.dose_basis)}</dd></div><div><dt>기간·빈도</dt><dd>{exposure.duration_days == null ? '기간 미확인' : `${text(exposure.duration_days)}일`} · {text(exposure.frequency)}</dd></div><div><dt>종·조직</dt><dd>{text(study.species)} · {text(study.tissue)}</dd></div><div><dt>대상군</dt><dd>{text(study.population)}</dd></div><div><dt>배정</dt><dd>{text(study.allocation)}</dd></div></dl></section><Conditions rows={commonConditions} /><section className="study-contrast-section"><h4><FlaskConical size={13} />관찰된 오믹스 결과와 대비</h4>{item.contrasts?.length ? item.contrasts.map(contrast => <ContrastCard key={contrast.id} contrast={contrast} />) : <p className="scientific-gap">연결할 수 있는 공개 재분석 대비가 없습니다. 출판된 연구 맥락만 유지합니다.</p>}</section><section className="clinical-section"><h4>같은 연구의 임상지표</h4><ResultTable rows={item.clinical_results || []} kind="임상" /></section><SourceDetails records={item.provenance || []} />{item.limitations?.length > 0 && <section className="scientific-limitations"><h5><Info size={12} />해석 범위</h5><ul>{item.limitations.map((limitation, index) => <li key={index}>{limitation}</li>)}</ul></section>}</article>;
}
export function ScientificEvidenceList({ records }: { records: EvidenceItem[] }) {
  const shared = records.filter(item => item.mapping_identity?.method === 'shared_measured_component');
  const primary = records.filter(item => item.mapping_identity?.method !== 'shared_measured_component');
  return <div className="human-evidence-list">
    {primary.map(item => <ScientificStudyCard item={item} key={item.id} />)}
    {shared.length > 0 && <LazyDetails className="shared-evidence-group" summary={<>
      <span><Link2 size={14} />공유 성분 관련 연구</span>
      <strong>중재 기록 {shared.length}개</strong>
      <small>효능 근거 아님 · 펼쳐서 탐색</small>
      <ChevronDown size={15} />
    </>}>{() => <div className="shared-evidence-body">
      <p>지방·단백질·탄수화물 등 측정 성분을 공유해 대응한 기록입니다. 음식·제품 동일성이나 해당 성분의 독립 효과를 확인한 목록은 아닙니다. {new Set(shared.map(item => item.study_id)).size}개 연구의 {shared.length}개 중재 기록이며 효능 순위로 배열하지 않았습니다.</p>
      {shared.map(item => <LazyDetails className="shared-study-disclosure" key={item.id} summary={<>
        <strong>{item.study_label || item.study_id}</strong>
        <span>{item.exposure?.label || '노출 명칭 미확인'}</span>
        <small>{item.mapping_identity.label || '공유 성분 대응'} · {item.contrasts?.length || 0}개 대비</small>
      </>}>{() => <ScientificStudyCard item={item} />}</LazyDetails>)}
    </div>}</LazyDetails>}
  </div>;
}
interface Draft { tissue: string; response_time_h: string; regimen_duration_days: string; frequency: string; population: string }
const emptyDraft: Draft = { tissue: '', response_time_h: '', regimen_duration_days: '', frequency: '', population: '' };
function referenceFromDraft(draft: Draft): ReferenceContext {
  return { ...(draft.tissue.trim() ? { tissue: draft.tissue.trim() } : {}), ...(draft.response_time_h !== '' ? { response_time_h: Number(draft.response_time_h) } : {}), ...(draft.regimen_duration_days !== '' ? { regimen_duration_days: Number(draft.regimen_duration_days) } : {}), ...(draft.frequency ? { frequency: draft.frequency as 'single' | 'daily' } : {}), ...(draft.population.trim() ? { population: draft.population.trim() } : {}) };
}
interface Props { seed: Entity | null; intakeG: number; onIntakeG: (amount: number) => void; geneNode?: Entity | null; onClearGene?: () => void; onScientificContext: (context: ScientificContext | null) => void; onGraph: () => void }
export default function ScientificEvidencePanel({ seed, intakeG, onIntakeG, geneNode, onClearGene, onScientificContext, onGraph }: Props) {
  const [draft, setDraft] = useState<Draft>(emptyDraft); const [result, setResult] = useState<ScientificMatchResult | null>(null);
  const [loading, setLoading] = useState(false); const [dirty, setDirty] = useState(false); const [error, setError] = useState('');
  const requestVersion = useRef(0); const controller = useRef<AbortController | null>(null);
  const notify = useRef(onScientificContext); notify.current = onScientificContext;
  const isFood = seed?.kind === 'food';
  const contextValid = ['response_time_h', 'regimen_duration_days'].every(key => {
    const value = draft[key as keyof Draft]; return value === '' || Number.isFinite(Number(value)) && Number(value) >= 0;
  });
  const valid = Boolean(seed) && (offline || contextValid && (!isFood || Number.isFinite(intakeG) && intakeG > 0 && intakeG <= 20000));
  const invalidate = useCallback(() => { ++requestVersion.current; controller.current?.abort(); setLoading(false); setDirty(true); setResult(null); setError(''); notify.current(null); }, []);
  const submit = async () => {
    if (!seed || !valid) return;
    controller.current?.abort(); const current = ++requestVersion.current; const abort = new AbortController(); controller.current = abort;
    const payload: ScientificContext = { seed_id: seed.id, ...(isFood ? { intake_g: intakeG } : {}), reference_context: referenceFromDraft(draft), ...(geneNode?.kind === 'gene' ? { gene_node_id: geneNode.id } : {}) };
    setLoading(true); setDirty(false); setError(''); setResult(null); notify.current(null);
    try { const response = await getScientificMatches(payload, abort.signal); if (current !== requestVersion.current) return;
      if (response.seed_id !== seed.id) throw new Error('응답의 탐색 기준이 현재 선택과 다릅니다. 다시 비교해주세요.');
      setResult(response); const frozenG = response.reference_context?.intake_g;
      const submitted = offline ? (frozenScientificContext?.seed_id === seed.id ? frozenScientificContext : { seed_id: seed.id, ...(typeof frozenG === 'number' ? { intake_g: frozenG } : {}), reference_context: response.reference_context || {} }) : payload;
      notify.current(submitted);
    } catch (failure) { if (current === requestVersion.current && (failure as Error).name !== 'AbortError') setError((failure as Error).message); }
    finally { if (current === requestVersion.current) setLoading(false); }
  };
  const submitLatest = useRef(submit); submitLatest.current = submit;
  useEffect(() => { setResult(null); setLoading(false); setError(''); notify.current(null); if (seed) void submitLatest.current(); return () => { ++requestVersion.current; controller.current?.abort(); }; }, [seed?.id, geneNode?.id]);
  const previousIntake = useRef(intakeG);
  useEffect(() => { if (previousIntake.current !== intakeG && !offline) invalidate(); previousIntake.current = intakeG; }, [intakeG, invalidate]);
  const edit = (key: keyof Draft, value: string) => { setDraft(previous => ({ ...previous, [key]: value })); invalidate(); };
  const displayedResult = result && result.seed_id === seed?.id && (offline || !isFood || result.reference_context?.intake_g === intakeG) ? result : null;
  const evidence = displayedResult?.observed_evidence || [];
  const frozen = displayedResult?.reference_context || {};
  return <section className="scientific-panel" aria-label="사람 중재 근거와 입력 조건 비교"><div className="scientific-panel-intro"><h2><FileSearch size={19} />연구 조건 비교</h2><p>제품·성분의 대응과 실제 연구 조건을 비교합니다. 개인 건강 효과 예측은 제공하지 않습니다.</p></div>{!seed ? <div className="scientific-empty"><FileSearch size={29} /><h3>먼저 탐색할 항목을 선택하세요</h3><p>왼쪽 표에서 음식이나 성분을 선택하면 관련 사람 연구를 확인합니다.</p></div> : <><div className="scientific-anchor"><span>선택 항목</span><strong>{seed.label}</strong><code>{seed.id}</code>{!offline && geneNode?.kind === 'gene' && <span className="gene-filter">발현 결과: {geneNode.label}{onClearGene && <button type="button" onClick={onClearGene} aria-label="유전자 결과 필터 해제">해제</button>}</span>}</div>{offline ? <div className="frozen-scientific-context"><Info size={14} /><div><strong>내보내기 시점의 조건 비교</strong><p>독립 HTML에서 조건을 바꾸거나 재계산하지 않습니다. 연결망에서 다른 유전자를 선택해도 이 저장 결과의 필터는 바뀌지 않습니다.</p>{frozenScientificContext?.gene_node_id && <p>저장된 유전자 조건: <code>{frozenScientificContext.gene_node_id}</code></p>}<dl>{Object.entries(frozen).map(([key, value]) => <div key={key}><dt>{dimensionLabels[key] || key}</dt><dd>{text(value)}</dd></div>)}</dl></div></div> : <form className="scientific-context-form" onSubmit={event => { event.preventDefault(); void submit(); }}><div className="context-primary"><label>음식 중량 <span>g</span><input type="number" min="0.1" max="20000" step="any" value={isFood ? intakeG : ''} onChange={event => onIntakeG(Number(event.target.value))} disabled={!isFood} aria-label="연구 조건 비교용 음식 중량 g" placeholder={isFood ? '100' : '음식 선택 시 사용'} /></label><label>조직 <span>선택</span><input list="scientific-tissues" value={draft.tissue} onChange={event => edit('tissue', event.target.value)} maxLength={100} placeholder="미입력" aria-label="비교할 조직" /><datalist id="scientific-tissues"><option value="blood">혈액</option><option value="whole_blood">전혈</option><option value="PBMC">PBMC</option><option value="plasma">혈장</option><option value="serum">혈청</option></datalist></label><label>반응 시간 <span>h</span><input type="number" min="0" step="any" value={draft.response_time_h} onChange={event => edit('response_time_h', event.target.value)} placeholder="미입력" aria-label="섭취 후 반응 시간 h" /></label><button className="button primary" type="submit" disabled={!valid || loading}>{loading ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}근거 비교</button></div><details className="context-advanced"><summary>반복 섭취·대상군 조건 <span>미입력은 미확인으로 유지</span><ChevronDown size={12} /></summary><div><label>반복 섭취 기간 <span>일</span><input type="number" min="0" step="any" value={draft.regimen_duration_days} onChange={event => edit('regimen_duration_days', event.target.value)} placeholder="미입력" aria-label="반복 섭취 기간 일" /></label><label>섭취 빈도 <span>선택</span><select value={draft.frequency} onChange={event => edit('frequency', event.target.value)} aria-label="섭취 빈도"><option value="">미입력</option><option value="single">한 번 섭취</option><option value="daily">매일 섭취</option></select></label><label>대상군 <span>선택</span><input value={draft.population} onChange={event => edit('population', event.target.value)} maxLength={300} placeholder="예: 건강 성인" aria-label="비교 대상군" /></label></div></details><p className="context-basis-note">음식 g를 성분 투여량·혈중 농도로 자동 환산하지 않습니다. 조리상태·원본 중량 기준은 우측 상세에서 확인하세요.</p></form>}
    {dirty && <div className="scientific-state-note" role="status"><Info size={14} />입력 조건이 바뀌었습니다. ‘근거 비교’를 눌러 새 조건으로 확인해주세요.</div>}{!contextValid && <div className="scientific-state-note">시간·기간은 0 이상의 유한한 숫자로 입력해주세요.</div>}{loading && <div className="scientific-loading" role="status"><LoaderCircle className="spin" size={20} /><strong>실제 중재 기록과 비교하는 중</strong><p>노출·대비·원문 위치와 입력 조건을 확인합니다.</p></div>}{error && <div className="scientific-empty"><Info size={26} /><h3>조건 비교 자료를 불러오지 못했습니다</h3><p>{error}</p><button className="button secondary" onClick={() => void submit()}>다시 비교</button></div>}{displayedResult && <><div className="evidence-list-heading"><h3>중재 기록 <span>{evidence.length}</span></h3><span>독립 연구 {new Set(evidence.map(item => item.study_id)).size}개 · 효능 순위 없음</span></div>{evidence.length > 0 && <p className="scientific-fineprint">관련 제품·성분 연구에는 공통 영양성분만 공유하는 기록도 포함됩니다. 각 기록의 제품 동일성·용량·대비를 확인해주세요.</p>}{evidence.length ? <ScientificEvidenceList records={evidence} /> : <div className="scientific-empty"><ClipboardList size={28} /><h3>대응하는 사람 중재 기록이 없습니다</h3><p>미확인 관계를 임상 효과로 채우지 않습니다. 연결 가설 탭에서 기전 관련성을 따로 탐색할 수 있습니다.</p><button className="button secondary" onClick={onGraph}><Network size={14} />연결 가설 탐색</button></div>}{displayedResult.evidence_gaps?.length > 0 && <section className="scientific-limitations match-gaps"><h5><Info size={13} />현재 확인할 수 없는 부분</h5><ul>{displayedResult.evidence_gaps.map((gap, index) => <li key={index}>{gap}</li>)}</ul></section>}<div className="scientific-version"><span>{displayedResult.note || '관찰된 연구 결과이며 현재 입력의 건강 효과를 예측하지 않습니다.'}</span><code>{displayedResult.rule_version} · {displayedResult.release_id}</code></div></>}</>}</section>;
}
