import { exposureRecord } from './catalogSearch';
import type { Entity, ScientificContrast, ScientificEvidence } from './types';

export function exposurePanelPresentation(seed:Entity|null,detail:Entity|null,isOffline:boolean) {
  const withheld=[detail?.metadata,seed?.metadata].find(value=>value?.exposure_export_status==='withheld');
  if(withheld){
    const reason=String(withheld.exposure_withheld_reason||'exposure_source_permission_unavailable');
    const reasons:Record<string,string>={canonical_exposure_identifier_mismatch:'노출 ID와 원본 기록이 일치하지 않아 상세 값을 내보내지 않았습니다.',exposure_source_permission_unavailable:'노출 출처의 재배포 권한을 확인할 수 없어 상세 값이 제외되었습니다.',exposure_source_redistribution_not_permitted:'출처 이용조건에 따라 노출 상세 값을 내보내지 않았습니다.'};
    return {state:'withheld' as const,message:'이용조건·식별자 확인에 따른 노출 상세 제한',reason:reasons[reason]||'이 snapshot에서 노출 상세의 공개가 제한되었습니다.'};
  }
  if(exposureRecord(detail)||exposureRecord(seed))return {state:'available' as const,message:'',reason:''};
  if(isOffline)return {state:'not_embedded' as const,message:'이 snapshot에는 노출 상세가 내장되지 않았습니다.',reason:'자료 미내장은 결과 부재나 변화량 0을 의미하지 않습니다. 원본 서비스에서 확인하세요.'};
  if(detail)return {state:'unavailable' as const,message:'연결된 원본 노출 상세를 확인할 수 없습니다.',reason:'연구 문맥 노드와 정식 중재 노출을 구분해 선택하세요.'};
  return {state:'loading' as const,message:'원본 노출 상세 조회 중',reason:''};
}

const labels:Record<string,string>={exploratory_confounded_extraction_time:'탐색 관찰 · 추출법–시점 교란',supplement_mapping_unresolved:'보충제별 효과 귀속 미확인'};
const flagLabels:Record<string,string>={extraction_method_aligned_with_time:'추출 방법과 방문 시점이 함께 변경됨',uncontrolled_before_after:'무대조 전후 비교',supplement_visit_mapping_unresolved:'보충제–방문 대응 미확인',rna_and_clinical_denominators_differ:'RNA 분석과 임상 대상자 분모가 다름',dha_formulation_amount_unresolved:'DHA 투여 형태·용량 미확인'};
export function sourceClassificationPresentation(exposure?:ScientificEvidence,contrast?:ScientificContrast) {
  const classification=contrast?.science_review_context?.source_classification||contrast?.study_source_classification||exposure?.source_classification||exposure?.science_review_context?.source_classification;
  const flags=classification?.flags||[];
  const excluded=[contrast?.primary_dietary_evidence_eligible,contrast?.science_review_context?.primary_dietary_evidence_eligible,exposure?.primary_dietary_evidence_eligible,exposure?.science_review_context?.primary_dietary_evidence_eligible,classification?.primary_dietary_evidence_eligible].includes(false);
  const attribution=[exposure?.supplement_attribution_established,classification?.supplement_attribution_established].includes(false);
  if(!classification&&!excluded&&!attribution)return null;
  return {title:labels[classification?.classification||'']||(attribution?'보충제별 효과 귀속 미확인':'원문 기반 근거 분류'),
    primaryLabel:excluded?'식이 주근거 제외':null,attributionLabel:attribution?'보충제별 귀속 미확인':null,
    reason:classification?.reason||'판정 세부 내용은 원본 연구·검수 기록에서 확인하세요.',
    flags:flags.map(value=>flagLabels[value]||value),references:classification?.source_references||[],
    boundary:'출처 기반 분류이며 전문가 승인 상태와 별개입니다.'};
}
