import type { Meta } from './types';

export function releaseIdentity(meta: Meta) {
  return {actual:meta.release_id,dataset:meta.dataset_release_id||meta.release_id,software:meta.service_build_id,
    composition:meta.candidate_composition_release_id,base:meta.immutable_base_release_id,
    ready:meta.submission_ready===true&&meta.status==='submission_ready'};
}
export function reviewSummary(meta: Meta): string {
  const summary=meta.review_summary;
  if(!summary)return '전문가 검수 상태 미확인';
  const approved=summary.expert_approved_records;
  const pending=summary.pending_records??summary.pending;
  if(typeof approved!=='number')return '사람 승인 건수 미감사';
  return `사람 승인 ${approved.toLocaleString('ko-KR')}건${typeof pending==='number'?` · 대기 ${pending.toLocaleString('ko-KR')}건`:''}`;
}
