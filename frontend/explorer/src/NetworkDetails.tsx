import { ArrowUpRight, X } from 'lucide-react';
import type { Entity, EvidenceEdge, NodeDetail } from './types';
import { entityType, tierLabel, edgeContext } from './networkModel';
import { relationLabel } from './GraphView';
const text=(v:unknown)=>v==null?'미확인':typeof v==='object'?JSON.stringify(v):String(v);
const safeUrl=(v:unknown)=>typeof v==='string'&&/^https?:\/\//.test(v)?v:undefined;
export default function NetworkDetails({node,edge,detail,records,names,loading,onRecord,onCenter,onClose}:{node?:Entity;edge?:EvidenceEdge|null;detail?:NodeDetail|null;records:EvidenceEdge[];names:Map<string,string>;loading:boolean;onRecord:(e:EvidenceEdge)=>void;onCenter:(e:Entity)=>void;onClose:()=>void}){
  const item=edge||node;
  if(!item)return null;
  const sources=(edge?.provenance||detail?.provenance||[]) as Record<string,unknown>[];
  return <section className="wb-network-detail"><button className="wb-close-selection" aria-label="네트워크 선택 상세 닫기" onClick={onClose}><X size={12}/>선택 닫기</button>
    <div className="wb-eyebrow">{edge?tierLabel(edge.tier):entityType(node!.kind)}</div><h2>{edge?relationLabel(edge.relation):node!.label}</h2><p className="wb-identifier">{item.id}</p>
    {loading&&<p role="status">출처 상세 조회 중…</p>}
    {edge?.tier==='predicted'&&<p className="wb-condition"><strong>계산 예측 · 실험 검증 아님</strong><br/>세포주 {text(edge.context?.tissue)}의 GraphRegLR 연결입니다. 원본 수치는 모델 파일의 필드이며 효과량·P값·임상 신뢰도가 아닙니다. 섭취 후 반응과 건강 효과는 미확인입니다.</p>}
    {edge?.tier==='extracted'&&<p className="wb-condition"><strong>자동 문헌 추출 후보 · 전문가 검수 대기</strong><br/>문장의 문자 위치를 대조한 상태입니다. 문장의 의미·대상군·인과성·섭취 조건이 검증된 것은 아닙니다.</p>}
    {edge?<><p className="ng-endpoints">{names.get(edge.subject)||edge.subject}<br/>↔<br/>{names.get(edge.object)||edge.object}</p><dl>{edgeContext(edge).map(r=><div key={r.label}><dt>{r.label}</dt><dd>{r.value}</dd></div>)}<div><dt>관계 원본명</dt><dd>{edge.relation}</dd></div><div><dt>검수 상태</dt><dd>{edge.review_status||'미확인'}</dd></div></dl><p className="wb-muted">출처 수록 여부와 전문가 검수는 별개입니다. 연결 자체는 식품의 효능이나 인과성을 입증하지 않습니다.</p>
      {edge.pmids?.length? <><h3>원문 문헌 · 고유 PMID {new Set(edge.pmids).size}개</h3>{[...new Set(edge.pmids)].filter(id=>/^\d+$/.test(id)).map(id=><a className="wb-source-link" key={id} href={`https://pubmed.ncbi.nlm.nih.gov/${id}/`} target="_blank" rel="noreferrer">PMID {id}<ArrowUpRight size={13}/></a>)}</>:<p className="wb-muted">개별 PMID 미수록</p>}
      {records.length>1&&<><h3>동일 맥락의 기록 {records.length}개</h3><div className="wb-network-records">{records.map(e=><button key={e.id} aria-pressed={edge.id===e.id} onClick={()=>onRecord(e)}>{e.id}</button>)}</div></>}
    </>:<><button onClick={()=>onCenter(node!)}>이 항목을 중심으로 탐색 <ArrowUpRight size={13}/></button><p>연결망에서 선택한 항목의 원본 기록입니다.</p>{detail?.measurements?.length? <><h3>성분 함량 · 원본 기준</h3><table className="wb-small-table"><thead><tr><th>성분</th><th>함량 · 기준</th></tr></thead><tbody>{detail.measurements.slice(0,10).map(m=><tr key={m.id}><td>{m.component_label||m.component}</td><td>{text(m.amount)} {m.unit}<small className="wb-row-sub">{m.basis} · {m.status}</small></td></tr>)}</tbody></table></>:null}{detail?.gaps?.map(g=><p className="wb-muted" key={g}>{g}</p>)}</>}
    <h3>자료 출처 · 버전</h3>{sources.length?sources.map((s,i)=><div key={i}><dl>{['id','name','source','version','release','license','export_allowed'].filter(k=>s[k]!=null).map(k=><div key={k}><dt>{k}</dt><dd>{text(s[k])}</dd></div>)}</dl>{safeUrl(s.url)&&<a className="wb-source-link" href={safeUrl(s.url)} target="_blank" rel="noreferrer">원본 자료<ArrowUpRight size={13}/></a>}</div>):<p>{loading?'조회 중':'상세 출처 미수록'} · source ID {edge?.source_ids?.join(', ')||'미확인'}</p>}
    {edge?.scientific?.limitations?.map(v=><p className="wb-muted" key={v}>{v}</p>)}
    <details><summary>원본 관계 맥락</summary><pre>{JSON.stringify(edge?.context||detail?.metadata||{},null,2)}</pre></details>
  </section>;
}
