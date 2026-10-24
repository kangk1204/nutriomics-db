import { BookOpen, Dna, FlaskConical, GitBranch, HeartPulse, Leaf, MapPin, Network, Search, Table2, SlidersHorizontal } from 'lucide-react';
import { normalizeKind } from './api';
import type { Entity, Meta, MappingIdentity } from './types';
import type { SearchKind } from './catalogSearch';

export const KIND_ICONS = {food:Leaf,component:FlaskConical,gene:Dna,cpg:Dna,regulatory_region:MapPin,health:HeartPulse,pathway:GitBranch};
const typeNames:Record<string,string>={food:'음식',diet:'연구 노출',exposure:'연구 노출',nutrient:'영양성분',chemical:'화합물',compound:'화합물',gene:'유전자',cpg:'CpG',methylation_probe:'프로브',regulatory_region:'조절 영역',disease:'질환',outcome:'건강지표',pathway:'경로'};
export function EntityBadge({entity}:{entity:Entity}) {
  const kind=normalizeKind(entity.kind),Icon=KIND_ICONS[kind];
  return <span className={`ws-type ws-type-${kind}`}><Icon size={14}/>{typeNames[entity.kind]||entity.kind}{!window.__NUTRIOMICS_SNAPSHOT__&&<a href={`?view=literature&q=${encodeURIComponent(entity.label)}`} title={`${entity.label} 관련 문헌 검색`} aria-label={`${entity.label} 관련 문헌 검색`} style={{marginLeft:6,color:'inherit',display:'inline-flex'}}><BookOpen size={12}/></a>}</span>;
}

export function WorkspaceWelcome({meta,onSearch}:{meta:Meta|null;onSearch:(kind:SearchKind,q:string)=>void}) {
  const count=(key:string)=>typeof meta?.counts[key]==='number'?Number(meta.counts[key]).toLocaleString('ko-KR'):null;
  const nodes=count('nodes')||count('node'),edges=count('edges')||count('edge');
  return <section className="ws-welcome" aria-label="탐색 시작">
    <div className="ws-start-label"><Search size={17}/><span>탐색 시작</span></div>
    <h2>식품·유전자·중재 탐색</h2>
    <p>항목을 선택하면 성분량, 관련 사람 연구, 실험 조건과 원문을 확인할 수 있습니다.</p>
    <div className="ws-start-grid">
      {[{kind:'inputs' as SearchKind,q:'Yogurt',name:'요구르트',description:'식품 조성과 관련 중재',Icon:Leaf},{kind:'exposure' as SearchKind,q:'포도씨 추출물',name:'포도씨 추출물',description:'배정 용량·기간으로 연구 탐색',Icon:BookOpen},{kind:'gene' as SearchKind,q:'APOE',name:'APOE',description:'발현·조절·질환 관련 근거',Icon:Dna},{kind:'health' as SearchKind,q:'blood pressure',name:'혈압',description:'원문에 보고된 건강지표',Icon:HeartPulse}].map(({kind,q,name,description,Icon})=><button key={kind} onClick={()=>onSearch(kind,q)}><Icon size={22}/><strong>{name}</strong><span>{description}</span><small>수록 항목 검색</small></button>)}
    </div>
    <div className="ws-workflows">
      <h3>선택한 항목을 네 가지 관점으로 확인합니다</h3>
      <div><Table2 size={19}/><span><b>근거 비교</b><small>노출·대비·조직·대상자와 통계 종류</small></span></div>
      <div><Dna size={19}/><span><b>발현·DNAm</b><small>원 분석 버전과 대비별 결과</small></span></div>
      <div><MapPin size={19}/><span><b>조절 위치</b><small>원본 좌표와 시험·예측 연결</small></span></div>
      <div><Network size={19}/><span><b>연결망</b><small>관계의 출처와 후보 경로의 약한 구간</small></span></div>
    </div>
    {(nodes||edges)&&<div className="ws-release-summary"><BookOpen size={17}/><span>현재 자료 범위</span>{nodes&&<b>{nodes}개 항목</b>}{edges&&<b>{edges}개 관계 기록</b>}<small>관계 수는 독립 연구·대상자 수와 다릅니다.</small></div>}
  </section>;
}

interface ReferenceInputProps {
  food:boolean; offline:boolean; intake:number; tissue:string; hours:string; days:string; frequency:string;
  onIntake:(value:number)=>void; onTissue:(value:string)=>void; onHours:(value:string)=>void; onDays:(value:string)=>void; onFrequency:(value:''|'single'|'daily')=>void;
}
export function ReferenceInputs({food,offline,intake,tissue,hours,days,frequency,onIntake,onTissue,onHours,onDays,onFrequency}:ReferenceInputProps) {
  const configured=[tissue,hours,days,frequency].filter(value=>value!=='').length;
  return <div className="wb-inputs rr-inputs">
    {food&&<label className="rr-intake">섭취량 <span className="ws-input-unit"><input aria-label="섭취량 g" disabled={offline} type="number" min="0.1" step="1" value={intake} onChange={e=>onIntake(Number(e.target.value)||100)}/><span>g</span></span></label>}
    <details className="rr-reference-options"><summary><SlidersHorizontal size={15}/><span>조건 설정</span><small>{configured?`${configured}개 조건 입력`:'조직·시간·기간'}</small></summary><div className="rr-reference-fields">
      <label>조직 <select aria-label="조직" disabled={offline} value={tissue} onChange={e=>onTissue(e.target.value)}><option value="">미지정</option><option>whole blood</option><option>PBMC</option><option>primary monocytes</option><option>K562</option><option>THP-1</option><option>Jurkat</option></select></label>
      <label>반응 시점 <select aria-label="반응 시점" disabled={offline} value={hours} onChange={e=>onHours(e.target.value)}><option value="">미지정</option><option value="2">2시간</option><option value="4">4시간</option><option value="6">6시간</option></select></label>
      <label>섭취 기간 <span className="ws-input-unit"><input aria-label="섭취 기간 일" disabled={offline} type="number" min="0" step="any" value={days} onChange={e=>onDays(e.target.value)} placeholder="미지정"/><span>일</span></span></label>
      <label>빈도 <select aria-label="섭취 빈도" disabled={offline} value={frequency} onChange={e=>onFrequency(e.target.value as ''|'single'|'daily')}><option value="">미지정</option><option value="single">한 번 섭취</option><option value="daily">매일 섭취</option></select></label>
    </div></details><span className="ws-unknown-note">미지정 조건은 미확인으로 유지</span>
  </div>;
}

export function LoadingRows({label}:{label:string}) {
  return <div className="ws-skeleton" role="status" aria-label={label}><span className="ws-skeleton-label">{label}</span>{[0,1,2,3,4].map(n=><div key={n}><i/><span><i/><i/></span></div>)}</div>;
}

export function MatchingBadge({identity}:{identity:MappingIdentity}) {
  const names:Record<string,string>={same_record:'동일 원본 노출',same_analyte:'동일 분석 성분',related_food:'관련 식품 · 제품 차이',related_component:'공통 성분 · 복합 중재',unknown:'대응 미확인'};
  return <span className="ws-matching-badge" title={identity.note||identity.method||'대응 방법 미확인'}>{names[identity.status||'unknown']||identity.label||'대응 미확인'}</span>;
}

export function EvidenceSummary({studies,contrasts,external,busy,externalLabel='수록 외부 관계'}:{studies:number|null;contrasts:number|null;external:number|null;busy:boolean;externalLabel?:string}) {
  return <div className="ws-evidence-summary" aria-label="조회 범위" aria-busy={busy}>
    {[['중재 대응 연구',studies,'음식·성분·노출 대응 · 연구 ID 중복 제거'],['중재 대비',contrasts,'원본 대비 ID 중복 제거'],[externalLabel,external,'집계 단위: 현재 필터의 관계 기록']].map(([name,value,note])=><div key={String(name)}><span>{name}</span><strong>{value==null?'—':Number(value).toLocaleString('ko-KR')}</strong><small>{note}</small></div>)}
  </div>;
}
