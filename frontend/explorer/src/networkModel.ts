import type { Entity, EvidenceEdge, GraphData, GraphPath } from './types';
import { nutrientReadingPriority } from './graphFocus';

export type NetworkMode = 'path' | 'neighbors' | 'all';
export type Position = { x: number; y: number };
export const entityType = (kind: string) => ({food:'음식',diet:'실험 노출',nutrient:'영양성분',component:'성분',compound:'화합물',chemical:'화합물',gene:'유전자',cpg:'CpG',methylation_probe:'비-CpG 프로브',regulatory_region:'조절 영역',pathway:'경로',disease:'질환',outcome:'건강지표',health:'건강',omics:'오믹스 기록'}[kind] || kind);
export function laneOf(node: Entity): number {
  if (['food','diet'].includes(node.kind)) return 0;
  if (['nutrient','component','compound','chemical'].includes(node.kind)) return 1;
  if (['cpg','methylation_probe','regulatory_region'].includes(node.kind)) return 2;
  if (['gene','pathway','omics'].includes(node.kind)) return 3;
  return 4;
}
export const LANE_LABELS = ['음식 · 실험 노출','성분 · 화합물','CpG · 조절 영역','유전자 · 경로','건강지표 · 질환'];
export const tierClass = (tier: string): 'recorded'|'indirect'|'extracted' => tier === 'extracted' ? 'extracted' : ['candidate','inferred','predicted','regulatory_identity','restricted'].includes(tier) ? 'indirect' : 'recorded';
export const tierLabel = (tier: string) => ({observed_composition:'식품 조성 기록',observed:'관찰·보고 기록',curated:'DB 수록 관계',candidate:'후보 대응',inferred:'추론 관계',predicted:'계산 예측',regulatory_identity:'조절 주석·식별자',restricted:'상세 이용 제한',extracted:'자동 추출 후보'}[tier] || tier);
function stable(value: unknown): string {
  if (Array.isArray(value)) return '['+value.map(stable).join(',')+']';
  if (value && typeof value === 'object') return '{'+Object.entries(value).sort(([a],[b])=>a.localeCompare(b)).map(([k,v])=>JSON.stringify(k)+':'+stable(v)).join(',')+'}';
  return JSON.stringify(value) ?? 'null';
}
export interface EdgeBundle { id: string; edge: EvidenceEdge; ids: string[]; records: EvidenceEdge[]; pmids: string[]; sources: string[] }
export const topologyKey=(graph:GraphData)=>JSON.stringify([graph.release_id,graph.nodes.map(n=>n.id),graph.edges.map(e=>[e.id,e.subject,e.object])]);
/** Presentation bundles never pool different relations, organisms, review states or contexts. */
export function bundleEdges(edges: EvidenceEdge[]): EdgeBundle[] {
  const groups = new Map<string,EvidenceEdge[]>();
  for(const edge of edges){
    const key=stable([edge.subject,edge.object,edge.relation,edge.tier,edge.taxon,edge.review_status,edge.context,edge.scientific]);
    const group=groups.get(key)||[];group.push(edge);groups.set(key,group);
  }
  return [...groups.values()].map(records=>({id:records[0].id,edge:records[0],ids:records.map(e=>e.id),records,pmids:[...new Set(records.flatMap(e=>e.pmids||[]))],sources:[...new Set(records.flatMap(e=>e.source_ids||[]))]}));
}
/** Every displayed element remains a source element; filtering cannot create a new path. */
export function projectNetwork(graph:GraphData,seed:string,mode:NetworkMode,path?:GraphPath,hiddenTiers:Set<string>=new Set()):GraphData {
  const available=graph.edges.filter(e=>!hiddenTiers.has(tierClass(e.tier)));
  const existing=new Set(graph.nodes.map(n=>n.id));
  const validPaths=graph.paths.filter(p=>p.node_ids.every(id=>existing.has(id))&&p.edge_ids.every(id=>available.some(e=>e.id===id)));
  const chosen=path&&validPaths.some(p=>p.id===path.id)?path:undefined;
  const ids=new Set<string>();
  const pathEdgeIds=new Set(chosen?.edge_ids||[]);
  if(mode==='all')graph.nodes.forEach(n=>ids.add(n.id));
  else {
    const roots=mode==='path'&&chosen ? chosen.node_ids : [seed];
    roots.filter(id=>existing.has(id)).forEach(id=>ids.add(id));
    const labels=new Map(graph.nodes.map(n=>[n.id,n.label]));
    const adjacent=available.filter(e=>roots.includes(e.subject)||roots.includes(e.object)).sort((a,b)=>{
      const rank=(e:EvidenceEdge)=>e.relation==='contains_nutrient'?10+nutrientReadingPriority(labels.get(e.object)||''):0;
      return rank(a)-rank(b)||a.id.localeCompare(b.id);
    });
    const cap=mode==='path'&&chosen?roots.length+1:16;
    for(const edge of adjacent){
      if(mode==='path'&&chosen&&ids.has(edge.subject)&&ids.has(edge.object))continue;
      if(ids.size+(ids.has(edge.subject)?0:1)+(ids.has(edge.object)?0:1)>cap)continue;
      ids.add(edge.subject);ids.add(edge.object);pathEdgeIds.add(edge.id);
    }
  }
  return {...graph,nodes:graph.nodes.filter(n=>ids.has(n.id)),edges:available.filter(e=>ids.has(e.subject)&&ids.has(e.object)&&(mode!=='path'||!chosen||pathEdgeIds.has(e.id))),paths:validPaths};
}
/** Deterministic semantic columns, not causal ordering. No source records are modified. */
export function semanticPositions(nodes:Entity[],path?:GraphPath):Record<string,Position>{
  const columns=[...new Set(nodes.map(laneOf))].sort((a,b)=>a-b);
  const result:Record<string,Position>={};
  let offset=0;
  for(const lane of columns){
    const members=nodes.filter(n=>laneOf(n)===lane).sort((a,b)=>{
      const ia=path?.node_ids.indexOf(a.id)??-1,ib=path?.node_ids.indexOf(b.id)??-1;
      return (ia<0?100:ia)-(ib<0?100:ib)||a.label.localeCompare(b.label)||a.id.localeCompare(b.id);
    });
    const rows=Math.min(members.length,6);
    members.forEach((node,i)=>{result[node.id]={x:90+offset+Math.floor(i/rows)*185,y:100+((i%rows)-(rows-1)/2)*112}});
    offset+=Math.ceil(members.length/rows)*210;
  }
  return result;
}
/** Geometric distance from the selected item; this is not a causal ordering. */
export function focusPositions(nodes:Entity[],edges:EvidenceEdge[],seed:string):Record<string,Position>{
  const ids=new Set(nodes.map(node=>node.id));
  if(!ids.has(seed))return semanticPositions(nodes);
  const distances=new Map<string,number>([[seed,0]]),queue=[seed];
  const adjacent=new Map<string,Set<string>>();
  for(const edge of edges){
    if(!ids.has(edge.subject)||!ids.has(edge.object))continue;
    for(const [from,to] of [[edge.subject,edge.object],[edge.object,edge.subject]]){
      if(!adjacent.has(from))adjacent.set(from,new Set());adjacent.get(from)!.add(to);
    }
  }
  for(let i=0;i<queue.length;i++)for(const target of adjacent.get(queue[i])||[]){
    if(!distances.has(target)){distances.set(target,distances.get(queue[i])!+1);queue.push(target)}
  }
  const result:Record<string,Position>={[seed]:{x:0,y:0}};
  const peers=nodes.filter(node=>node.id!==seed).sort((a,b)=>(distances.get(a.id)??100)-(distances.get(b.id)??100)||laneOf(a)-laneOf(b)||a.label.localeCompare(b.label)||a.id.localeCompare(b.id));
  for(let start=0;start<peers.length;start+=20){
    const ring=peers.slice(start,start+20),level=Math.floor(start/20),rx=ring.length<=5?160+level*140:270+level*140,ry=ring.length<=5?120+level*100:180+level*100;
    ring.forEach((node,i)=>{const angle=-Math.PI/2+2*Math.PI*i/ring.length;result[node.id]={x:rx*Math.cos(angle),y:ry*Math.sin(angle)}});
  }
  return result;
}
export function restorePinned(defaults:Record<string,Position>,saved:Record<string,Position>,pinned:Set<string>):Record<string,Position>{
  return Object.fromEntries(Object.entries(defaults).map(([id,pos])=>[id,pinned.has(id)&&saved[id]?saved[id]:pos]));
}
export function edgeContext(edge:EvidenceEdge):{label:string;value:string}[]{
  const c=edge.context||{};
  const value=(keys:string[])=>keys.map(k=>c[k]).find(v=>v!==null&&v!==undefined&&v!=='');
  const text=(v:unknown)=>v==null?'미확인':typeof v==='object'?stable(v):String(v);
  const doseKey=['dose','amount','dose_g','amount_per_100g'].find(k=>c[k]!==null&&c[k]!==undefined&&c[k]!=='');
  const dose=doseKey?c[doseKey]:undefined;
  const doseUnit=value(['dose_unit','amount_unit','unit']);
  const durationKey=['duration','duration_days','response_time_h','timepoint'].find(k=>c[k]!==null&&c[k]!==undefined&&c[k]!=='');
  const duration=durationKey?c[durationKey]:undefined;
  const m=(c.measurement&&typeof c.measurement==='object'?c.measurement:null) as Record<string,unknown>|null;
  const missingAmount = m?.status==='not_measured'?'함량 미측정':m?.status==='below_detection'?'검출한계 미만':'함량 미확인';
  const measurementText=m?`${m.amount==null?missingAmount:text(m.amount)} ${m.unit||'단위 미확인'} · ${m.basis||'기준 미확인'} · ${m.status||'상태 미확인'}`:'미확인';
  const doseText=typeof dose==='number'?`${dose} ${doseUnit||(doseKey==='dose_g'?'g':'단위 미확인')}${doseKey==='amount_per_100g'?' / 원본 100g 기준':''}`:dose==null?measurementText:text(dose);
  const durationText=typeof duration==='number'?`${duration} ${durationKey==='duration_days'?'일':durationKey==='response_time_h'?'h':'단위 미확인'}`:text(duration);
  return [{label:'종',value:text(edge.taxon||edge.scientific?.study_evidence?.species||value(['organism','species','taxon']))},
    {label:'조직·세포',value:text(edge.scientific?.study_evidence?.tissue||value(['tissue','cell_type','cell_line']))},
    {label:'용량·함량',value:doseText},
    {label:'기간·시점',value:durationText}];
}
