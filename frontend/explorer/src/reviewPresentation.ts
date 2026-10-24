import { reviewLabel } from './catalogSearch';
import type { InterventionRecord, ScienceReviewContext, ScientificContrast, ScientificEvidence } from './types';

type Decision='accept'|'correct'|'exclude'|'unconfirmed'|'pending';
type State=Decision|'missing'|'invalid'|'legacy';
interface Badge {scope:string;label:string;state:State;recordId?:string;binding:string}
export interface ReviewPresentation {items:Badge[];summary:string;boundary:string;restricted:boolean}
const names:Record<Decision,string>={accept:'승인',correct:'수정 필요',exclude:'제외',unconfirmed:'미확인',pending:'검수 대기'};
const decisions=new Set(Object.keys(names));
const hash=(value:unknown):value is string=>typeof value==='string'&&/^[a-fA-F0-9]{64}$/.test(value);
const priority:Record<State,number>={exclude:0,correct:1,unconfirmed:2,invalid:3,pending:4,missing:5,accept:6,legacy:7};

function scoped(context:ScienceReviewContext|undefined,kind:string,id:string|undefined,scope:string):Badge {
  const decision=context?.human_review;
  if(!decision)return {scope,label:`${scope}: 개별 판정 정보 미제공`,state:'missing',recordId:id,binding:'개별 사람 판정 정보가 제공되지 않았습니다.'};
  if(!hash(context?.canonical_record_sha256)||decision.record_sha256!==context.canonical_record_sha256||decision.record_kind!==kind||!id||decision.record_id!==id||!decisions.has(decision.decision||'')){
    return {scope,label:`${scope}: 판정 결속 미확인`,state:'invalid',recordId:id,binding:'판정의 원본 해시·종류·ID가 현재 기록과 일치하는지 확인할 수 없습니다.'};
  }
  const state=decision.decision as Decision;
  if(state!=='pending'&&(decision.reviewer_attestation!==true||decision.reviewer_type!=='human'))return {scope,label:`${scope}: 사람 판정 확인 필요`,state:'invalid',recordId:id,binding:'사람 검수자의 명시적 판정 확인 정보가 없습니다.'};
  if(state==='accept'&&context?.scientific_approval===false)return {scope,label:`${scope}: 판정 정보 불일치`,state:'invalid',recordId:id,binding:'개별 승인 판정과 제공된 승인 상태가 일치하지 않습니다.'};
  return {scope,label:`${scope}: ${names[state]}`,state,recordId:id,binding:state==='pending'?'현재 기록 해시에 결속된 사람 판정은 아직 대기 상태입니다.':`현재 기록의 해시·종류·ID와 사람 판정 확인 정보가 일치합니다.${decision.reason?` ${decision.reason}`:''}`};
}
function presentation(items:Badge[],limited=false):ReviewPresentation {
  const sorted=[...items].sort((a,b)=>priority[a.state]-priority[b.state]);
  const restricted=limited||sorted.some(item=>['exclude','correct','unconfirmed','invalid'].includes(item.state));
  const pending=sorted.some(item=>['pending','missing'].includes(item.state));
  return {items:sorted,restricted,summary:restricted?'제한·미확인 판정 우선 확인':pending?'범위별 검수 대기·미제공':'범위별 판정 기록',boundary:'표시된 개별 기록 범위의 상태입니다. 연구 승인만으로 대비·전체 데이터셋의 검수 완료를 의미하지 않습니다.'};
}
function legacy(status:unknown,scope='원본 표기'):ReviewPresentation {
  return {items:[{scope,label:`${scope}: ${reviewLabel(status)}`,state:'legacy',binding:'개별 해시 결속 사람 판정이 제공되지 않아 원본의 상태 표기를 유지합니다.'}],summary:'원본 검수 상태',restricted:false,boundary:'원본 상태 표기이며 개별 연구·대비의 새로운 사람 판정으로 승격하지 않습니다.'};
}
export function evidenceReviewPresentation(exposure?:ScientificEvidence,contrast?:ScientificContrast,fallback?:unknown):ReviewPresentation {
  const study=exposure?.science_review_context,comparison=contrast?.science_review_context;
  if(!study?.human_review&&!comparison?.human_review)return legacy(fallback??exposure?.study_evidence?.expert_review_status);
  const items=[scoped(study,'study',exposure?.study_id,'연구')];
  if(contrast)items.push(scoped(comparison,'contrast',contrast.id,'대비'));
  const limited=[exposure?.primary_dietary_evidence_eligible,study?.primary_dietary_evidence_eligible,contrast?.primary_dietary_evidence_eligible,comparison?.primary_dietary_evidence_eligible].includes(false);
  return presentation(items,limited);
}
export function exposureReviewPresentation(record?:InterventionRecord,seedRecord?:InterventionRecord):ReviewPresentation {
  const context=record?.science_review_context||(record?.id===seedRecord?.id?seedRecord?.science_review_context:undefined);
  if(context?.human_review)return presentation([scoped(context,'exposure',record?.id,'노출')],context.primary_dietary_evidence_eligible===false);
  const status=record?.publication_review_status;
  const mapped:Record<string,Decision>={accept:'accept',accepted:'accept',correct:'correct',correction_required:'correct',exclude:'exclude',excluded:'exclude',unconfirmed:'unconfirmed',pending:'pending'};
  if(status&&mapped[status]){
    const state=mapped[status],bound=hash(record?.record_sha256)||hash(record?.canonical_record_sha256)||hash(record?.original_record_sha256);
    if(state!=='pending'&&!bound)return presentation([{scope:'노출 공개판정',label:'노출 공개판정: 원본 결속 미확인',state:'invalid',binding:'공개판정에 대응하는 원본 기록 해시가 제공되지 않았습니다.'}]);
    const result=presentation([{scope:'노출 공개판정',label:`노출 공개판정: ${names[state]}`,state,binding:'공개된 원본 노출 기록의 publication_review_status입니다.'}]);
    result.boundary='해당 노출 기록의 공개판정 표기입니다. 연구·대비의 사람 승인이나 전체 검수 완료를 뜻하지 않습니다.';
    return result;
  }
  return legacy(record?.metadata?.expert_review_status,'노출 원본 표기');
}
