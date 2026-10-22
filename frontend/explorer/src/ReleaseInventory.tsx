import type { Meta } from './types';
import { offline } from './api';
import { releaseIdentity, reviewSummary } from './releasePresentation';

const number = (value: unknown) => typeof value === 'number' ? value.toLocaleString() : '미확인';

export default function ReleaseInventory({ meta }: { meta: Meta | null }) {
  if (!meta) return null;
  const counts = meta.counts;
  const tiers = (counts.by_tier || {}) as Record<string,unknown>;
  const science = (counts.science_kinds || {}) as Record<string,unknown>;
  const identity=releaseIdentity(meta);
  return <details className="wb-release-inventory">
    <summary>자료 범위·집계 단위·검수 상태 <span>{meta.scope === 'public' ? '공개 허용 자료' : '연구용 자료'} · {reviewSummary(meta)}</span></summary>
    <dl className="release-identity"><div><dt>조회 중인 자료</dt><dd>{identity.actual}</dd></div>{identity.dataset!==identity.actual&&<div><dt>데이터 버전</dt><dd>{identity.dataset}</dd></div>}{identity.software&&<div><dt>서비스 빌드</dt><dd>{identity.software}</dd></div>}{identity.composition&&<div><dt>큐레이션 구성</dt><dd>{identity.composition}</dd></div>}{identity.base&&<div><dt>원본 동결 release</dt><dd>{identity.base}</dd></div>}</dl>
    <p className="release-readiness">{identity.ready?'제출 준비 상태는 release metadata를 따릅니다.':meta.status==='candidate_pending_expert_review'?'검수 대기 후보 release입니다. 과학적 검수·최종 평가·공개 등록 완료를 뜻하지 않습니다.':'제출 준비 완료 여부는 미확인입니다.'} {offline&&'표시 범위는 이 파일에 내장된 snapshot입니다.'}</p>
    {!offline && <p><a href="/api/v1/explorer/dictionary" target="_blank" rel="noreferrer">현재 release의 자료 사전 JSON · 임상 수치와 통계 필드 정의</a></p>}
    <div className="release-grains">
      <div><strong>{number(counts.composition_backed_foods)}</strong><b>조성 보유 식품 기록</b><small>동일 이름·다른 조리상태를 합산하지 않음</small></div>
      <div><strong>{number(counts.measurement)}</strong><b>조성 기록</b><small>측정·계산·문헌 추정·결측 상태 포함</small></div>
      <div><strong>{number(counts.reanalyzed_study_groups)} / {number(counts.study_registry_records)}</strong><b>재분석 자료 보유 / 등록 연구</b><small>독립 연구·대상자 총합의 확정값이 아님</small></div>
      <div><strong>{number(counts.effect)}</strong><b>정량 오믹스 결과 행</b><small>특징 × 대비 × 분석 버전 · 대상자 수 아님</small></div>
      <div><strong>{number(tiers.predicted)}</strong><b>조절영역–유전자 계산 예측</b><small>세포주 모델 · 식품 효능 검증 아님</small></div>
      <div><strong>{number(science.clinical_endpoint_result)}</strong><b>원문 건강지표 요약 후보</b><small>공동 수록 · 유전자 매개·인과 효과 미확인</small></div>
    </div>
    {meta.scope === 'public' && !offline && <p className="release-downloads"><a href="/api/v1/public/files/nutriomics.sqlite3">전체 SQLite · 대용량</a><a href="/api/v1/public/files/tables/science_record.csv.gz">연구·대비·임상 기록 CSV</a><a href="/api/v1/public/files/dataset-metadata.json">자료 사전·집계 metadata</a><a href="/api/v1/public/files" target="_blank" rel="noreferrer">전체 파일·SHA256 목록</a></p>}
    <p>자동 문헌 후보 {number(counts.automatically_extracted_assertions)}건 · 공개 상세 제한 {number(counts.withheld_literature_assertions)}건. 제한 건수는 release 전체이며 선택한 음식과의 관련성이나 근거 부재를 뜻하지 않습니다.</p>
  </details>;
}
