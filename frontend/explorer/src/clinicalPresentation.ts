type Value = Record<string, unknown>;
const object = (value: unknown): Value => value && typeof value === 'object' && !Array.isArray(value) ? value as Value : {};
const text = (value: unknown): string => value == null ? '미보고' : typeof value === 'object' ? JSON.stringify(value) : String(value);

export function clinicalFacts(value: unknown): Value[] {
  const row = object(value);
  const records = object(row.context).clinical_endpoints ?? object(row.metadata).clinical_endpoints;
  return Array.isArray(records) ? records.filter(record => record && typeof record === 'object' && !Array.isArray(record)) as Value[] : [];
}

export function clinicalTableSummary(value: unknown) {
  const records = clinicalFacts(value);
  const first = records[0];
  const observations = Array.isArray(first?.observations) ? first.observations as Value[] : [];
  const tests = Array.isArray(first?.statistical_tests) ? first.statistical_tests as Value[] : [];
  const endpoint = object(first?.endpoint);
  const comparison = object(first?.comparison);
  return {
    title: `${text(endpoint.label_ko ?? endpoint.label ?? first?.endpoint_id)} · ${text(comparison.arm ?? comparison.kind ?? first?.result_type)}`,
    values: observations.map(observation => `${text(observation.time_label ?? observation.timepoint)}: ${text(observation.reported_value ?? observation.value)} ${text(observation.unit ?? endpoint.unit ?? first?.unit)}`).join(' / ') || '보고값 상세 확인',
    scope: `원문 요약 ${observations.length}개 · 별도 검정 ${tests.length}개 · 단일 치료 효과 아님`,
  };
}

export function clinicalPValue(test: Value) {
  return text(test.reported_label ?? test.p_value);
}

/** Present known source fields without estimating or changing their scale. */
export function clinicalComparison(value: unknown): string {
  const comparison = object(value);
  if (comparison.kind !== 'crossover_order_stratified_reported_iAUC') return text(value);
  const parts = ['교차시험', '섭취 순서별 식후 iAUC'];
  if (comparison.dose_g != null) parts.push(`${text(comparison.dose_g)} g`);
  const window = comparison.response_window_h;
  if (Array.isArray(window) && window.length === 2) parts.push(`${text(window[0])}–${text(window[1])}시간`);
  return parts.join(' · ');
}

export function clinicalDispersion(value: unknown): string {
  const dispersion = object(value);
  if (dispersion.kind === 'IQR' && dispersion.lower != null && dispersion.upper != null) {
    return `IQR ${text(dispersion.lower)}–${text(dispersion.upper)}`;
  }
  return text(value);
}

export function clinicalResultType(value: unknown): string {
  return ({crossover_iAUC_summary:'교차시험의 식후 iAUC 요약', arm_timepoint_summary:'군·시점별 요약', arm_summary:'군별 요약'} as Record<string,string>)[String(value)] || text(value);
}
