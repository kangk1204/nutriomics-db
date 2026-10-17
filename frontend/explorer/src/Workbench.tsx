import { useEffect, useMemo, useRef, useState } from 'react';
import { flexRender, getCoreRowModel, useReactTable, type ColumnDef } from '@tanstack/react-table';
import { Search, Download, ExternalLink, ChevronLeft, ChevronRight, X, SlidersHorizontal, Network, Table2, Dna, MapPin, LoaderCircle, PanelLeftClose, PanelLeftOpen, BookOpen, Info, Pin, Check } from 'lucide-react';
import * as echarts from 'echarts/core';
import { BarChart } from 'echarts/charts';
import { AriaComponent, GridComponent, TooltipComponent, DataZoomComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import { analyzeFood, exportGraph, frozenScientificContext, getEdge, getEntities, getGraph, getLoci, getMeta, getNode, getOmicsResults, getScientificMatches, getWorkbenchEvidence, getWorkbenchRecord, normalizeKind, offline, saveBlob, scientificSnapshotSeed } from './api';
import GraphView from './GraphView';
import NetworkDetails from './NetworkDetails';
import { contrastConditions } from './evidenceConditions';
import { mealPresentation } from './mealPresentation';
import { calculationId } from './foodIdentity';
import { summarizeScientificEvidence } from './evidenceSummary';
import { EntityBadge, WorkspaceWelcome, LoadingRows, EvidenceSummary, MatchingBadge, KIND_ICONS } from './WorkbenchSurface';
import type { Entity, EvidenceEdge, Filters, GraphData, Kind, Locus, Meta, NodeDetail, ScientificContrast, ScientificEvidence, ScientificMatchResult, WorkbenchEvidence } from './types';
import './workbench.css';
import './studio.css';

echarts.use([BarChart, AriaComponent, GridComponent, TooltipComponent, DataZoomComponent, CanvasRenderer]);
const kinds: Record<Kind,string> = {food:'음식',component:'성분',gene:'유전자',cpg:'CpG·프로브',regulatory_region:'조절 영역',health:'건강',pathway:'경로'};
const entityKindLabel=(e:Entity)=>e.kind==='diet'?'실험 노출':kinds[normalizeKind(e.kind)];
const layers: Record<string,string> = {all:'전체 수록 근거',dietary:'사람 식이중재',eqtm:'CpG–발현 연관',regulatory_tested:'조절 실험 · 시험된 쌍',regulatory_predicted:'계산 예측',literature_candidate:'자동 문헌 후보',clinical:'측정 건강지표',molecular_target:'분자 assay',molecular_identity:'식별자 관계'};
const f = (value: unknown):string => value == null ? '미보고' : typeof value === 'number' ? (Math.abs(value)>0 && Math.abs(value)<0.001 ? value.toExponential(2) : Number(value.toPrecision(5)).toLocaleString('en-US')) : typeof value === 'object' ? JSON.stringify(value) : String(value);
const link = (value:unknown) => typeof value==='string' && /^https?:\/\//.test(value) ? value : undefined;
interface Row {id:string;layer:string;title:string;study:string;tissue?:string;n?:number|null;assays?:number|null;effect?:number|null;unit?:string;p?:unknown;q?:unknown;context?:string;contrast?:ScientificContrast;exposure?:ScientificEvidence;reg?:WorkbenchEvidence;raw?:Record<string,unknown>}

function LociView({items,onSelect}:{items:Locus[];onSelect:(id:string)=>void}) {
  const host=useRef<HTMLDivElement>(null);
  const chromosomes=[...new Set(items.map(r=>r.chromosome))];
  const [chr,setChr]=useState('');
  const active=chr && chromosomes.includes(chr)?chr:chromosomes[0];
  useEffect(()=>{
    if (!host.current || !items.length) return;
    const chart=echarts.init(host.current);
    const rows=items.filter(r=>r.chromosome===active);
    const minimum=Math.min(...rows.map(r=>r.start));
    chart.setOption({aria:{enabled:true,description:`${active} 조절 영역 원본 좌표. 관계의 인과성이나 유의성을 뜻하지 않습니다.`},
      animation:false,grid:{left:155,right:35,top:18,bottom:50},tooltip:{trigger:'axis'},
      xAxis:{type:'value',min:minimum,axisLabel:{formatter:(v:number)=>v.toLocaleString('en-US')}},
      yAxis:{type:'category',inverse:true,data:rows.map(r=>`${r.subject_id.split(':').pop()} · ${r.tissue||'미확인'}`)},
      dataZoom:[{type:'inside',xAxisIndex:0},{type:'slider',xAxisIndex:0,height:16,bottom:5},
        {type:'slider',yAxisIndex:0,start:0,end:Math.min(100,20/rows.length*100),right:5,width:12,top:18,bottom:50,filterMode:'none'}],
      series:[{type:'bar',stack:'interval',data:rows.map(r=>r.start),itemStyle:{color:'transparent'},emphasis:{disabled:true},silent:true},
        {type:'bar',stack:'interval',data:rows.map(r=>Math.max(r.end-r.start,1)),barWidth:8,itemStyle:{color:'#596f91'}}]});
    chart.on('click',params=>{if(rows[params.dataIndex])onSelect(rows[params.dataIndex].evidence_id)});
    const observer=new ResizeObserver(()=>chart.resize());observer.observe(host.current);
    return()=>{observer.disconnect();chart.dispose()};
  },[items,active,onSelect]);
  if (!items.length) return <div className="wb-empty"><MapPin/><h3>표시할 수 있는 검증된 좌표가 없습니다</h3><p>식별자 연관 근거는 비교 표에서 확인할 수 있습니다. 기준이 미확인인 좌표는 중첩 분석에 사용하지 않습니다.</p></div>;
  return <><div className="wb-inline"><label>원본 기준 GRCh38/hg38 · <select value={active} onChange={e=>setChr(e.target.value)}>{chromosomes.map(c=><option key={c}>{c}</option>)}</select></label><span>{items.length}개 위치 · 원본 좌표 관례는 상세 참조</span></div><div ref={host} className="wb-locus-chart"/><div className="wb-table-scroll"><table className="wb-small-table"><thead><tr><th>영역</th><th>원본 위치</th><th>근거</th><th>조직·세포</th></tr></thead><tbody>{items.map(r=><tr key={r.evidence_id} onClick={()=>onSelect(r.evidence_id)}><td><button>{r.subject_id}</button></td><td>{r.chromosome}:{r.start.toLocaleString()}–{r.end.toLocaleString()}</td><td>{layers[r.layer]}</td><td>{r.tissue||'미확인'}</td></tr>)}</tbody></table></div></>;
}

function GeneIdentifiers({detail,onSelect,onError}:{detail:NodeDetail|null;onSelect:(e:Entity)=>void;onError:(message:string)=>void}) {
  const links=detail?.verified_identifier_links||[];
  if(!links.length)return null;
  return <section><h3>공식 유전자 식별자</h3><p className="wb-muted">HGNC의 정확한 ID 대응입니다. 통계·조직·식이 효과를 합치지 않습니다.</p>{links.map(e=>{const target=e.subject_id===detail?.id?e.object_id:e.subject_id;return <button key={e.id} onClick={()=>{void getNode(target).then(onSelect).catch(error=>onError(error.message))}}>{target}</button>})}</section>;
}

function MealSummary({meal}:{meal:Record<string,unknown>}) {
  const {components,unquantified,interpretation,status}=mealPresentation(meal);
  const [all,setAll]=useState(false);
  const shown=all?components:components.slice(0,8);
  return <section className="wb-meal-summary" aria-label="계산된 성분 섭취량" aria-live="polite">
    <div className="wb-meal-status">{status==='measured'?'정량 계산 완료':status==='partial'?'일부 성분 계산':'계산 가능한 성분 없음'} · {components.length}개 성분</div>
    <table className="wb-small-table"><thead><tr><th>성분</th><th>섭취량 · 단위</th></tr></thead><tbody>{shown.map((row,i)=><tr key={`${row.id}:${i}`}><td>{row.name}<details><summary>함량 출처</summary>{row.sources.length?row.sources.map((source,j)=><dl key={j}><div><dt>자료 · 버전</dt><dd>{f(source.source_id)} · {f(source.release)}</dd></div><div><dt>함량 기준</dt><dd>{f(source.basis)}</dd></div><div><dt>원본 기록</dt><dd>{link(source.source_ref)?<a href={link(source.source_ref)} target="_blank" rel="noreferrer">원본 자료</a>:f(source.source_ref)}</dd></div></dl>):<p>출처 기록 미확인</p>}</details></td><td>{f(row.amount)} {row.unit||'단위 미확인'}</td></tr>)}</tbody></table>
    {components.length>8&&<button onClick={()=>setAll(!all)}>{all?'주요 성분만 표시':`전체 ${components.length}개 성분 표시`}</button>}
    {unquantified.length>0&&<details><summary>계산에서 제외된 기록 {unquantified.length}개</summary>{unquantified.map((row,i)=><div className="wb-condition" key={i}><b>{f(row.name)}</b><p>{f(row.status)} · 기준 {f(row.basis)}</p><p>{Array.isArray(row.exclusion_reasons)?row.exclusion_reasons.join(' · '):'제외 사유 미확인'}</p><p>자료 {f(row.source_id)} · {f(row.release)}</p></div>)}</details>}
    <p>{interpretation}</p>
    <button onClick={()=>saveBlob(new Blob([JSON.stringify(meal,null,2)],{type:'application/json;charset=utf-8'}),'nutriomics-meal-calculation.json')}>전체 계산·출처 JSON 저장</button>
  </section>;
}

export default function Workbench({onLegacy}:{onLegacy:()=>void}) {
  const [meta,setMeta]=useState<Meta|null>(null),[kind,setKind]=useState<Kind>(scientificSnapshotSeed?normalizeKind(scientificSnapshotSeed.kind):'food'),[q,setQ]=useState('');
  const [domain,setDomain]=useState('all'),[page,setPage]=useState(1),[entities,setEntities]=useState<Entity[]>([]),[total,setTotal]=useState(0),[searchError,setSearchError]=useState(''),[searchRetry,setSearchRetry]=useState(0);
  const [seed,setSeed]=useState<Entity|null>(scientificSnapshotSeed||null),[detail,setDetail]=useState<NodeDetail|null>(null);
  const [matches,setMatches]=useState<ScientificMatchResult|null>(null),[reg,setReg]=useState<WorkbenchEvidence[]>([]),[regTotal,setRegTotal]=useState(0),[regNotice,setRegNotice]=useState(''),[regPage,setRegPage]=useState(1);
  const [layer,setLayer]=useState('all'),[tissue,setTissue]=useState(frozenScientificContext?.reference_context?.tissue||''),[hours,setHours]=useState(frozenScientificContext?.reference_context?.response_time_h==null?'':String(frozenScientificContext.reference_context.response_time_h)),[intake,setIntake]=useState(frozenScientificContext?.intake_g||100);
  const [regBusy,setRegBusy]=useState(false),[regLoaded,setRegLoaded]=useState(false),[browserOpen,setBrowserOpen]=useState(true),[inspectorOpen,setInspectorOpen]=useState(true),[mobilePanel,setMobilePanel]=useState<'search'|'workspace'|'details'>(scientificSnapshotSeed?'workspace':'search');
  const [tab,setTab]=useState('compare'),[selected,setSelected]=useState<Row|null>(null),[pinned,setPinned]=useState<Row[]>([]);
  const mainPanel=useRef<HTMLElement>(null),inspectorPanel=useRef<HTMLElement>(null);
  useEffect(()=>{mainPanel.current?.scrollTo({top:0})},[seed?.id,tab]);
  useEffect(()=>{inspectorPanel.current?.scrollTo({top:0})},[seed?.id,selected?.id]);
  const [graph,setGraph]=useState<GraphData|null>(null),[loci,setLoci]=useState<Locus[]>([]),[edge,setEdge]=useState<EvidenceEdge|null>(null);
  const [lociStatus,setLociStatus]=useState<'loading'|'available'|'error'>('loading');
  const [networkNode,setNetworkNode]=useState<Entity|null>(null),[networkDetail,setNetworkDetail]=useState<NodeDetail|null>(null),[networkRecords,setNetworkRecords]=useState<EvidenceEdge[]>([]),[networkBusy,setNetworkBusy]=useState(false);
  const networkRequest=useRef(0),graphRequest=useRef(0);
  const [error,setError]=useState(''),[busy,setBusy]=useState(false),[searchBusy,setSearchBusy]=useState(false),[meal,setMeal]=useState<Record<string,unknown>|null>(null);
  const [mealBusy,setMealBusy]=useState(false);
  const mealRequest=useRef(0);
  const mealFoodId=calculationId(detail);
  useEffect(()=>{mealRequest.current++;setMeal(null);setMealBusy(false)},[seed?.id,intake]);
  const calculateMeal=async()=>{
    if(!mealFoodId)return;
    const requestId=++mealRequest.current;setMeal(null);setMealBusy(true);
    try{const result=await analyzeFood(mealFoodId,intake);if(requestId===mealRequest.current)setMeal(result)}
    catch(error){if(requestId===mealRequest.current)setError((error as Error).message)}
    finally{if(requestId===mealRequest.current)setMealBusy(false)}
  };
  const [results,setResults]=useState<Record<string,unknown>[]>([]),[resultStatus,setResultStatus]=useState(''),[featureType,setFeatureType]=useState('gene_expression'),[resultPage,setResultPage]=useState(1),[resultTotal,setResultTotal]=useState<number|null>(null);
  const filters:Filters={domain,tier:'all',include_extracted:layer==='literature_candidate'};
  useEffect(()=>{getMeta().then(setMeta).catch(e=>setError(e.message))},[]);
  useEffect(()=>{const controller=new AbortController();setSearchBusy(true);setSearchError('');setEntities([]);setTotal(0);const timer=setTimeout(()=>{
    getEntities(kind,q,filters,page,25,'label','asc',controller.signal).then(r=>{setEntities(r.items);setTotal(r.total)}).catch(e=>{if(!controller.signal.aborted&&e.name!=='AbortError'){setSearchError(e.message);setError(e.message)}}).finally(()=>{if(!controller.signal.aborted)setSearchBusy(false)});
  },220);return()=>{clearTimeout(timer);controller.abort()}},[kind,q,domain,page,searchRetry,layer]);
  useEffect(()=>{
    if(!seed)return;let active=true;networkRequest.current++;graphRequest.current++;setNetworkNode(null);setNetworkDetail(null);setNetworkRecords([]);setNetworkBusy(false);setDetail(null);setGraph(null);setLoci([]);setSelected(null);setEdge(null);setMeal(null);setError('');
    getNode(seed.id).then(r=>{if(active)setDetail(r)}).catch(e=>{if(active)setError(e.message)});
    getGraph(seed.id,filters,80,200).then(r=>{if(active)setGraph(r)}).catch(e=>{if(active)setError(e.message)});
    setLociStatus('loading');
    getLoci(seed.id).then(r=>{if(active){setLoci(r.items);setLociStatus('available')}}).catch(()=>{if(active){setLoci([]);setLociStatus('error')}});
    return()=>{active=false};
  },[seed?.id,domain,layer]);
  useEffect(()=>{
    if(!seed)return;const controller=new AbortController();setBusy(true);setMatches(null);
    const context={seed_id:seed.id,intake_g:intake,reference_context:{tissue:tissue||null,response_time_h:hours===''?null:Number(hours)}};
    const timer=setTimeout(()=>getScientificMatches(context,controller.signal).then(r=>{if(!controller.signal.aborted)setMatches(r)}).catch(e=>{if(e.name!=='AbortError')setError(e.message)}).finally(()=>{if(!controller.signal.aborted)setBusy(false)}),250);
    return()=>{clearTimeout(timer);controller.abort()};
  },[seed?.id,intake,tissue,hours]);
  useEffect(()=>{
    if(!seed)return;const controller=new AbortController();setReg([]);setRegTotal(0);setRegNotice('');setRegLoaded(false);setRegBusy(true);
    getWorkbenchEvidence(seed.id,layer==='dietary'?'all':layer,tissue,regPage,controller.signal).then(r=>{if(!controller.signal.aborted){setReg(r.items);setRegTotal(r.total);setRegNotice(r.status==='withheld'?(r.gaps||[]).join(' '):'');setRegLoaded(r.status!=='not_indexed')}}).catch(e=>{if(e.name!=='AbortError')setError(e.message)}).finally(()=>{if(!controller.signal.aborted)setRegBusy(false)});
    return()=>controller.abort();
  },[seed?.id,layer,tissue,regPage]);
  useEffect(()=>{setRegPage(1)},[seed?.id,layer,tissue]);
  const rows=useMemo(()=>{
    const values:Row[]=[];
    if(layer==='all'||layer==='dietary')for(const e of matches?.observed_evidence||[])for(const c of e.contrasts){
      values.push({id:`${e.id}:${c.id}`,layer:'dietary',title:c.comparison||c.label||c.contrast||c.id,study:e.study_label||e.study_id,tissue:e.study_evidence.tissue,n:c.n_people,assays:c.n_samples,context:`${e.exposure.label} · ${f(e.exposure.amount)} ${e.exposure.unit||''}`,contrast:c,exposure:e});
    }
    if(layer!=='dietary')for(const e of reg)values.push({id:e.id,layer:e.layer,title:`${e.subject_id} ↔ ${e.object_id}`,study:String(e.study?.title||e.source?.source||e.source?.version||e.study_id||'자료원'),tissue:e.tissue,n:e.n_people,assays:e.n_samples,effect:e.effect,unit:e.effect_unit,p:e.p_value_original??e.p_value,q:e.q_value,reg:e});
    return values;
  },[matches,reg,layer]);
  useEffect(()=>{setSelected(old=>old?rows.find(row=>row.id===old.id)||old:null)},[rows]);
  const columns=useMemo<ColumnDef<Row>[]>(()=>[
    {id:'pin',header:'비교',cell:({row})=><button aria-label={`${row.original.title} 비교 고정`} aria-pressed={pinned.some(r=>r.id===row.original.id)} disabled={pinned.length>=4&&!pinned.some(r=>r.id===row.original.id)} className="wb-pin" onClick={e=>{e.stopPropagation();setPinned(old=>old.some(r=>r.id===row.original.id)?old.filter(r=>r.id!==row.original.id):old.length<4?[...old,row.original]:old)}}>{pinned.some(r=>r.id===row.original.id)?<Check size={16}/>:<Pin size={16}/>}</button>},
    {accessorKey:'title',header:'근거·대비',cell:({row})=><><button className="wb-row-title" onClick={()=>selectRow(row.original)}>{row.original.title}</button><span className={`wb-row-sub ws-layer-${row.original.layer}`}>{layers[row.original.layer]||row.original.layer}{row.original.exposure&&` · ${row.original.context}`}</span>{row.original.exposure&&<MatchingBadge identity={row.original.exposure.mapping_identity}/>}</>},
    {accessorKey:'study',header:'연구·자료원'}, {accessorKey:'tissue',header:'조직·세포',cell:i=>f(i.getValue())},
    {accessorKey:'n',header:'대상자',cell:i=>f(i.getValue())},{accessorKey:'assays',header:'Assay',cell:i=>f(i.getValue())},
    {id:'effect',header:'결과·통계',cell:({row})=>row.original.contrast?<span>원 분석 결과 조회</span>:<><b>{f(row.original.effect)}</b><span className="wb-row-sub">{row.original.unit||'정량 결과 미보고'} · P {f(row.original.p)}</span></>},
    {id:'review',header:'검수',cell:({row})=><span className="wb-review">{(row.original.reg?.expert_review_status||row.original.exposure?.study_evidence.expert_review_status)==='approved'?'검수 승인':(row.original.reg?.expert_review_status||row.original.exposure?.study_evidence.expert_review_status)==='pending'?'전문가 검수 대기':'검수 미확인'}</span>}
  ],[pinned]);
  const table=useReactTable({data:rows,columns,getCoreRowModel:getCoreRowModel()});
  const selectEntity=(e:Entity)=>{setSeed(e);setKind(normalizeKind(e.kind));setTab('compare');setMobilePanel('workspace');setInspectorOpen(true)};
  const inspectNetworkNode=async(e:Entity)=>{const request=++networkRequest.current;setNetworkNode(e);setNetworkDetail(null);setEdge(null);setNetworkRecords([]);setNetworkBusy(true);try{const r=await getNode(e.id);if(request===networkRequest.current)setNetworkDetail(r)}catch(e){if(request===networkRequest.current)setError((e as Error).message)}finally{if(request===networkRequest.current)setNetworkBusy(false)}};
  const inspectNetworkEdge=async(e:EvidenceEdge,ids:string[])=>{const request=++networkRequest.current;setNetworkNode(null);setNetworkDetail(null);setEdge(e);setNetworkRecords((graph?.edges||[]).filter(record=>ids.includes(record.id)));setNetworkBusy(true);try{const r=await getEdge(e.id);if(request===networkRequest.current)setEdge(r)}catch(e){if(request===networkRequest.current)setError((e as Error).message)}finally{if(request===networkRequest.current)setNetworkBusy(false)}};
  const centerNetwork=(e:Entity)=>{setSeed(e);setKind(normalizeKind(e.kind));setTab('graph');setMobilePanel('workspace');setInspectorOpen(true)};
  const expandNetwork=async(maxNodes=250,maxEdges=750)=>{if(!seed)return;const id=++graphRequest.current;setError('');try{const r=await getGraph(seed.id,filters,maxNodes,maxEdges);if(id===graphRequest.current)setGraph(r)}catch(e){if(id===graphRequest.current)setError((e as Error).message)}};
  const closeNetworkSelection=()=>{networkRequest.current++;setNetworkNode(null);setNetworkDetail(null);setEdge(null);setNetworkRecords([]);setNetworkBusy(false);setMobilePanel('workspace')};
  const exportSelection=async(format:'html'|'csv'|'json',selection=graph)=>{
    if(!selection||!meta)return;try{
      const context={seed_id:seed!.id,intake_g:intake,reference_context:{tissue:tissue||null,response_time_h:hours===''?null:Number(hours)}};
      const r=await exportGraph({format,release_id:meta.release_id,node_ids:selection.nodes.map(n=>n.id),edge_ids:selection.edges.map(e=>e.id),positions:Object.fromEntries(Object.entries(window.__NUTRIOMICS_POSITIONS__||{}).filter(([id])=>selection.nodes.some(n=>n.id===id))),filters:{...filters,scientific_context:context}});saveBlob(r.blob,r.filename);
    }catch(e){setError((e as Error).message)}
  };
  const resultRequest=useRef(0);
  useEffect(()=>{resultRequest.current++;setResults([]);setResultStatus('');setResultTotal(null)},[selected?.contrast?.id,featureType,resultPage]);
  const loadResults=async()=>{if(!selected?.contrast)return;const requestId=++resultRequest.current;setResultStatus('loading');try{const r=await getOmicsResults(selected.contrast,featureType,resultPage);if(requestId!==resultRequest.current)return;setResults(r.items);setResultTotal(r.total);setResultStatus(r.status==='available'?`조회 완료 · 원본 측정 유형 ${r.feature_type||featureType}`:r.status==='feature_type_unavailable'?`요청 유형 미수록 · 제공 유형 ${(r.available_feature_types||[]).join(', ')||'미확인'}`:r.status==='not_embedded'?'이 snapshot에는 전체 분석 결과가 포함되지 않았습니다. 원본 서비스에서 확인하세요.':r.status)}catch(e){if(requestId===resultRequest.current){setResultStatus('error');setError((e as Error).message)}}};
  const source=selected?.reg?.source||selected?.exposure?.provenance?.[0];
  const tabs=[['compare','근거 비교',Table2],['omics','발현·DNAm',Dna],['loci','조절 위치',MapPin],['graph','연결망',Network]] as const;
  const reviewStatus=selected?.reg?.expert_review_status||selected?.exposure?.study_evidence.expert_review_status;
  const {studies:studyCount,contrasts:contrastCount}=summarizeScientificEvidence(matches);
  const searchExample=(kind:Kind,query:string)=>{setKind(kind);setQ(query);setPage(1);setMobilePanel('search');setBrowserOpen(true)};
  const selectRow=(row:Row)=>{setSelected(row);setEdge(null);setResults([]);setResultStatus('');setResultPage(1);setInspectorOpen(true);setMobilePanel('details')};
  return <div className={`wb-app wb-studio ws-mobile-${mobilePanel}`}>
    <a className="ws-skip-link" href="#workspace">작업 화면으로 이동</a>
    <header className="wb-header">
      <div className="ws-brand"><span className="ws-brand-mark"><GitBranchMark/></span><div className="wb-wordmark">NutriOmics <span>Explorer</span></div></div>
      <div className="wb-header-sub">연구 근거 탐색 <span>Research use</span></div>
      <div className="wb-header-actions"><button className="ws-legacy-button" onClick={onLegacy}>기존 탐색 화면</button><label className="ws-export"><Download size={16}/><select aria-label="내보내기 형식" value="" disabled={!graph} onChange={e=>void exportSelection(e.target.value as 'html'|'csv'|'json')}><option value="">내보내기</option><option value="html">독립 HTML</option><option value="csv">CSV</option><option value="json">JSON</option></select></label></div>
    </header>
    <div className="wb-context-bar"><div className="ws-release-label"><BookOpen size={14}/><span className="wb-release">{meta?.release_id||'자료 버전 연결 중'}{offline?' · 내장 snapshot':''}</span>{typeof meta?.scope==='string'&&meta.scope.includes('rights-filtered')&&<span className="ws-scope-badge" title="원본 release에서 이용조건에 따라 허용된 조성과 관계만 포함한 자료입니다.">허용된 자료 부분집합</span>}</div><label>건강 범위 <select aria-label="건강 범위" value={domain} onChange={e=>{setDomain(e.target.value);setPage(1)}}><option value="all">전체 수록 항목</option><option value="blood_pressure">혈압</option><option value="lipids">지질</option><option value="obesity">비만</option></select></label><span className="wb-context-note">관찰 결과 · 조절 연관 · 예측을 구분합니다</span></div>
    <nav className="ws-mobile-nav" aria-label="화면 영역">{([['search','검색',Search],['workspace','작업',Table2],['details','상세',BookOpen]] as const).map(([id,label,Icon])=><button key={id} aria-pressed={mobilePanel===id} onClick={()=>{setMobilePanel(id);if(id==='search')setBrowserOpen(true);if(id==='details')setInspectorOpen(true)}}><Icon size={16}/>{label}</button>)}</nav>
    {error&&<div role="alert" className="wb-error"><Info size={17}/><span>{error}</span><button onClick={()=>setError('')} aria-label="오류 닫기"><X size={17}/></button></div>}
    <div className={`wb-layout ${tab==='graph'&&seed?'wb-network-layout':''} ${!browserOpen?'ws-browser-collapsed':''} ${!inspectorOpen?'ws-inspector-collapsed':''} ${selected||networkNode||edge||mobilePanel==='details'?'ws-detail-active':''}`}>
      <aside className="wb-browser" aria-label="항목 탐색">
        <div className="wb-panel-label">항목 탐색 <span>{searchBusy?'검색 중':searchError?'조회 실패':`${total.toLocaleString()}개`}</span></div>
        <div className="wb-kinds" role="group" aria-label="항목 유형">{Object.entries(kinds).map(([k,label])=>{const Icon=KIND_ICONS[k as Kind];return <button key={k} aria-pressed={kind===k} className={kind===k?'active':''} onClick={()=>{setKind(k as Kind);setPage(1);setQ('')}}><Icon size={16}/>{label}</button>})}</div>
        <label className="wb-search"><Search size={18}/><input aria-label="항목 검색" value={q} onChange={e=>{setQ(e.target.value);setPage(1)}} placeholder="이름·별칭·식별자"/>{q&&<button aria-label="검색어 지우기" onClick={()=>{setQ('');setPage(1)}}><X size={15}/></button>}{searchBusy&&<LoaderCircle className="wb-loading" size={16}/>}</label>
        <div className="wb-entity-heading"><span>{kinds[kind]} 이름</span><span title="관계 기록 수 · 독립 연구 수 아님">관계</span></div>
        <div className="wb-entity-list" aria-busy={searchBusy}>{searchBusy?<LoadingRows label="항목 검색 중"/>:entities.map(e=><button key={e.id} className={seed?.id===e.id?'selected':''} aria-current={seed?.id===e.id?'true':undefined} onClick={()=>selectEntity(e)}><span><b>{e.label}</b><small>{e.id}</small></span><span>{e.degree?.toLocaleString()||'0'}</span></button>)}{searchError&&!searchBusy&&<div role="status" className="ws-no-results"><Info size={24}/><b>검색 결과를 확인할 수 없습니다</b><p>{searchError}</p><button onClick={()=>setSearchRetry(n=>n+1)}>검색 다시 시도</button></div>}{!searchError&&!entities.length&&!searchBusy&&<div className="ws-no-results"><Search size={24}/><b>일치하는 항목이 없습니다</b><p>다른 이름·별칭·원본 식별자를 입력하세요.</p>{q&&<button onClick={()=>{setQ('');setPage(1)}}>검색어 지우기</button>}</div>}</div>
        <div className="wb-pager"><button aria-label="이전 검색 페이지" disabled={page===1||searchBusy||Boolean(searchError)} onClick={()=>setPage(p=>p-1)}><ChevronLeft size={18}/></button><span>{page} <span className="ws-page-divider">/</span> {searchError?'미확인':Math.max(1,Math.ceil(total/25))}</span><button aria-label="다음 검색 페이지" disabled={page*25>=total||searchBusy||Boolean(searchError)} onClick={()=>setPage(p=>p+1)}><ChevronRight size={18}/></button></div>
      </aside>
      <main ref={mainPanel} className="wb-main" id="workspace" tabIndex={-1}>
        <div className="wb-heading"><div className="ws-heading-copy">{seed?<EntityBadge entity={seed}/>:<div className="wb-eyebrow">연구 워크벤치</div>}<h1>{seed?.label||'수록 근거 탐색'}</h1>{seed&&<span className="wb-identifier">{seed.id}</span>}</div><div className="ws-heading-actions">{busy&&<LoaderCircle aria-label="조건 조회 중" className="wb-loading" size={18}/>}<button className="ws-browser-toggle" aria-label={browserOpen?'검색 패널 접기':'검색 패널 열기'} aria-expanded={browserOpen} onClick={()=>setBrowserOpen(!browserOpen)}>{browserOpen?<PanelLeftClose size={19}/>:<PanelLeftOpen size={19}/>}</button>{seed&&tab!=='graph'&&<button aria-label="근거 상세 열기" className="ws-inspector-toggle" onClick={()=>{setInspectorOpen(!inspectorOpen);setMobilePanel('details')}}><BookOpen size={18}/><span>상세</span></button>}</div></div>
        {seed&&<div className="wb-inputs"><span className="ws-context-title"><SlidersHorizontal size={16}/>비교 조건</span>{seed.kind==='food'&&<label>섭취량 <span className="ws-input-unit"><input aria-label="섭취량 g" disabled={offline} type="number" min="0.1" step="1" value={intake} onChange={e=>setIntake(Number(e.target.value)||100)}/><span>g</span></span></label>}<label>조직 <select aria-label="조직" disabled={offline} value={tissue} onChange={e=>setTissue(e.target.value)}><option value="">미지정</option><option>whole blood</option><option>PBMC</option><option>primary monocytes</option><option>K562</option><option>THP-1</option><option>Jurkat</option></select></label><label>반응 시점 <select aria-label="반응 시점" disabled={offline} value={hours} onChange={e=>setHours(e.target.value)}><option value="">미지정</option><option value="2">2시간</option><option value="4">4시간</option><option value="6">6시간</option></select></label><span className="ws-unknown-note">미지정 조건은 미확인으로 유지</span></div>}
        <nav className="wb-tabs" aria-label="작업 화면">{tabs.map(([id,label,Icon])=><button key={id} aria-current={tab===id?'page':undefined} className={tab===id?'active':''} onClick={()=>{setTab(id);setMobilePanel('workspace')}}><Icon size={17}/>{label}</button>)}</nav>
        {!seed?<WorkspaceWelcome meta={meta} onSearch={searchExample}/>:<>
          {pinned.length>0&&tab!=='graph'&&<div className="wb-comparison"><div className="wb-comparison-title"><span><Pin size={15}/>고정 비교</span><small>{pinned.length}/4 · 통계 종류별로 읽기</small></div><div className="wb-compare-grid">{pinned.map(r=><article key={r.id}><button className="wb-close" onClick={()=>setPinned(old=>old.filter(x=>x.id!==r.id))} aria-label={`${r.title} 비교 해제`}><X size={15}/></button><small>{layers[r.layer]}</small><h3>{r.title}</h3><p>{r.tissue||'조직 미확인'} · 대상자 {f(r.n)}</p><b>{r.contrast?'대비별 결과 조회':`${f(r.effect)} ${r.unit||''}`}</b><p>{r.context||r.study}</p></article>)}</div></div>}
          {tab==='compare'&&<>
            <EvidenceSummary studies={studyCount} contrasts={contrastCount} external={regLoaded?regTotal:null} busy={busy||regBusy}/>
            <div className="wb-evidence-toolbar"><div><h2>조건별 근거</h2><span>서로 다른 실험·통계는 별도 기록으로 표시합니다.</span></div><label>근거 종류 <select aria-label="근거 종류" value={layer} onChange={e=>setLayer(e.target.value)}>{Object.entries(layers).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label></div>
            {regNotice&&<p className="wb-condition" role="status">{regNotice}</p>}
            <div className="wb-table-scroll" aria-label="조건별 근거 표" tabIndex={0}><table className="wb-evidence-table"><thead>{table.getHeaderGroups().map(g=><tr key={g.id}>{g.headers.map(h=><th scope="col" key={h.id}>{flexRender(h.column.columnDef.header,h.getContext())}</th>)}</tr>)}</thead><tbody>{table.getRowModel().rows.map(r=><tr key={r.id} className={selected?.id===r.original.id?'selected':''} onClick={()=>selectRow(r.original)}>{r.getVisibleCells().map(c=><td key={c.id}>{flexRender(c.column.columnDef.cell,c.getContext())}</td>)}</tr>)}</tbody></table>{!rows.length&&((busy||regBusy)?<LoadingRows label="연구 조건 조회 중"/>:<div className="wb-empty compact"><BookOpen/><h3>{error?'조회가 완료되지 않았습니다':regNotice?'문헌 후보 상세 이용 제한':'해당 조건의 수록 근거가 없습니다'}</h3><p>{error?'연결 상태와 표시된 오류를 확인해주세요.':'근거 부재는 효과가 없다는 결과가 아닙니다.'}</p></div>)}</div>
            <div className="wb-table-foot"><span>관계·대비 기록 수이며 독립 대상자 수와 다릅니다.</span><div className="wb-pager"><button aria-label="이전 근거 페이지" disabled={regPage===1||regBusy} onClick={()=>setRegPage(p=>p-1)}><ChevronLeft size={17}/></button><span>{regPage} / {Math.max(1,Math.ceil(regTotal/25))}</span><button aria-label="다음 근거 페이지" disabled={regPage*25>=regTotal||regBusy} onClick={()=>setRegPage(p=>p+1)}><ChevronRight size={17}/></button></div></div>
            <details className="wb-method-note"><summary><Info size={15}/>자료 범위·결측·해석 기준</summary><p>동일 연구의 assay와 반복 방문은 독립 대상자로 집계하지 않습니다. eQTM은 연관, CRISPR 목록은 시험된 쌍, GraphRegLR는 계산 예측입니다. 일부 자원은 공개된 선택 요약만 포함합니다. 전문 검수 대기와 자료 부재를 그대로 보존합니다.</p>{matches?.evidence_gaps.map(g=><p key={g}>{g}</p>)}</details>
          </>}
          {tab==='omics'&&<div className="wb-omics"><div className="ws-section-heading"><Dna size={22}/><div><h2>원 분석 결과</h2><p>측정 유형·대비·분석 버전을 함께 확인합니다.</p></div></div>{selected?.contrast?<>
            <div className="ws-contrast-banner"><span>선택한 대비</span><h3>{selected.contrast.comparison||selected.contrast.label||selected.contrast.contrast}</h3><code>{selected.contrast.series_accession} · {selected.contrast.analysis_version||'분석 버전 미확인'}</code></div>
            <div className="wb-inline"><label>측정 유형 <select value={featureType} onChange={e=>setFeatureType(e.target.value)}><option value="gene_expression">유전자 발현</option><option value="cpg">CpG</option><option value="dmr">DMR</option><option value="pathway">경로</option></select></label><label>페이지 <input type="number" min="1" value={resultPage} onChange={e=>setResultPage(Math.max(1,Number(e.target.value)))}/></label><button disabled={resultStatus==='loading'} className="wb-primary" onClick={()=>void loadResults()}>{resultStatus==='loading'?<LoaderCircle className="wb-loading" size={16}/>:<Table2 size={16}/>}모든 결과 조회</button></div>
            <div className="ws-result-status" role="status">{resultStatus==='loading'?'원 분석 결과 조회 중':resultStatus==='error'?'결과 조회 실패':resultStatus?`${resultStatus}${resultTotal==null?'':` · 결과 ${f(resultTotal)}건`}`:'조회 버튼을 누르면 선택한 대비의 전체 결과를 불러옵니다.'}</div>
            <div className="wb-table-scroll" tabIndex={0} aria-label="오믹스 결과 표"><table className="wb-small-table"><thead><tr><th>특성</th><th>효과·단위</th><th>P</th><th>FDR</th><th>분석 버전</th></tr></thead><tbody>{results.map((r,i)=><tr key={i}><td className="ws-feature-cell">{f(r.feature_label||r.feature_id)}</td><td>{f(r.effect_estimate)} <small>{f(r.effect_unit)}</small></td><td>{f(r.p_value)}</td><td>{f(r.q_value)}</td><td><code>{f(r.analysis_version)}</code></td></tr>)}</tbody></table></div><p className="ws-analysis-note">FDR의 검정 범위와 시료 포함 기준은 대비 상세·원 분석 방법에서 확인하세요.</p>
          </>:<><div className="ws-inline-empty"><Table2 size={22}/><div><h3>먼저 중재 대비를 선택하세요</h3><p>근거 비교에서 사람 중재 기록을 선택하면 해당 대비의 전체 결과를 조회할 수 있습니다.</p><button onClick={()=>setTab('compare')}>근거 비교로 이동</button></div></div><h3>선택 항목의 직접 발현 결과</h3>{detail?.effects?.length?<div className="wb-table-scroll" tabIndex={0}><table className="wb-small-table"><thead><tr><th>연구</th><th>특성</th><th>대비</th><th>효과·단위</th><th>FDR</th></tr></thead><tbody>{detail.effects.map((r,i)=><tr key={i}><td>{f(r.series_accession)}</td><td>{f(r.feature_id)}</td><td>{f(r.contrast)}</td><td>{f(r.effect_estimate)} {f(r.effect_unit)}</td><td>{f(r.q_value)}</td></tr>)}</tbody></table></div>:<p className="ws-analysis-note">{!detail?'항목 상세 조회 중':'직접 대응한 발현 결과가 없습니다. CpG–유전자 주석으로 발현 결과를 생성하지 않습니다.'}</p>}</> }</div>}
          {tab==='loci'&&<div className="ws-loci"><div className="ws-section-heading"><MapPin size={22}/><div><h2>조절 영역의 위치</h2><p>좌표·세포 유형·근거 종류를 함께 탐색합니다.</p></div></div>{lociStatus==='loading'?<LoadingRows label="조절 위치 조회 중"/>:lociStatus==='error'?<div className="wb-empty"><MapPin/><h3>조절 위치 조회가 완료되지 않았습니다</h3><p>조회 실패는 좌표가 없다는 결과가 아닙니다. 항목을 다시 선택해 조회해주세요.</p></div>:<LociView items={loci} onSelect={id=>{const row=rows.find(r=>r.id===id);setInspectorOpen(true);setMobilePanel('details');if(row)setSelected(row);else void getWorkbenchRecord(id).then(e=>setSelected({id:e.id,layer:e.layer,title:String(e.subject_id)+' ↔ '+String(e.object_id),study:String(e.study?.title||e.study_id||e.source?.source||'원본 자료'),tissue:e.tissue,n:e.n_people,assays:e.n_samples,effect:e.effect,unit:e.effect_unit,p:e.p_value_original??e.p_value,q:e.q_value,reg:e})).catch(e=>setError(e.message))}}/>}</div>}
          {tab==='graph'&&<div className="wb-graph"><GraphView graph={graph} loading={!graph&&!error} error={error} seedId={seed.id} selectedId={edge?.id||networkNode?.id||seed.id} onNode={e=>{setInspectorOpen(true);setMobilePanel('details');void inspectNetworkNode(e)}} onEdge={(e,ids)=>{setInspectorOpen(true);setMobilePanel('details');void inspectNetworkEdge(e,ids)}} onCenter={centerNetwork} onExport={shown=>{void exportSelection('html',shown)}} onRetry={()=>{void expandNetwork(80,200)}} onExpand={()=>expandNetwork()} canExpand={Boolean(graph?.truncated)}/><p className="wb-graph-note">연결선은 서로 다른 근거를 이어 탐색하는 관계입니다. 식품의 효능·인과 경로를 뜻하지 않습니다.</p></div>}
        </>}
      </main>
      <aside ref={inspectorPanel} className={`wb-inspector ${tab==='graph'&&(networkNode||edge)?'wb-network-inspector-open':''}`} aria-label="근거 상세">
        <div className="wb-panel-label"><span><BookOpen size={16}/>근거 상세</span><button aria-label="근거 상세 닫기" onClick={()=>{setInspectorOpen(false);setMobilePanel('workspace');if(tab==='graph')closeNetworkSelection()}}><X size={17}/></button></div>
        {tab==='graph'&&(edge||networkNode)?<NetworkDetails node={networkNode||undefined} edge={edge} detail={networkDetail} records={networkRecords} names={new Map(graph?.nodes.map(n=>[n.id,n.label])||[])} loading={networkBusy} onRecord={e=>{void inspectNetworkEdge(e,networkRecords.map(r=>r.id))}} onCenter={centerNetwork} onClose={closeNetworkSelection}/>:selected?<>
          <span className={`ws-evidence-kind ws-layer-${selected.layer}`}>{layers[selected.layer]}</span><h2>{selected.title}</h2><p>{selected.study}</p><details className="ws-record-identity"><summary>원본 근거 ID</summary><code className="ws-record-id">{selected.id}</code></details>
          <div className="ws-review-line">{reviewStatus==='approved'?<Check size={15}/>:<Info size={15}/>}<span>{reviewStatus==='approved'?'전문가 검수 승인':reviewStatus==='pending'?'전문가 검수 대기':reviewStatus||'전문가 검수 미확인'}</span></div>
          <h3>연구 조건</h3><dl><div><dt>조직·세포</dt><dd>{selected.tissue||'미확인'}</dd></div><div><dt>대상자</dt><dd>{f(selected.n)}{selected.contrast?.n_people_scope&&<details className="ws-count-scope"><summary>집계 범위</summary><small>{selected.contrast.n_people_scope}</small></details>}</dd></div><div><dt>Assay</dt><dd>{f(selected.assays)}{selected.contrast?.n_samples_scope&&<details className="ws-count-scope"><summary>집계 범위</summary><small>{selected.contrast.n_samples_scope}</small></details>}</dd></div></dl>
          {selected.layer==='regulatory_predicted'&&<section className="wb-condition"><h3>계산 예측 · 실험 검증 아님</h3><p>{selected.tissue||'세포주 미확인'}의 cCRE–유전자 연결입니다. 사람 식이중재의 관찰 결과가 아닙니다.</p><dl><div><dt>원본 수치 필드</dt><dd>{f(selected.reg?.context?.source_numeric_fields)}</dd></div><div><dt>방법</dt><dd>{f(selected.reg?.context?.method)}</dd></div><div><dt>원본 위치</dt><dd>{f(selected.reg?.context?.source_interval)}</dd></div></dl><p>thresholded 원본의 필드입니다. 효과량·P/FDR·건강 효과의 확률로 해석하지 않습니다.</p></section>}
          {selected.layer==='literature_candidate'&&<section className="wb-condition"><h3>자동 추출 후보 · 의미 검수 대기</h3><blockquote>{f(selected.reg?.context?.quote)}</blockquote><p>문자 위치 [{f(selected.reg?.context?.start)}, {f(selected.reg?.context?.end)}) · title/abstract 기준</p><p>원문 문자 일치는 의미 정확도나 인과성 검증이 아닙니다. 용량·조직·대상군·건강 효과는 확정하지 않았습니다.</p></section>}
          {selected.reg&&<><h3>효과와 불확실성</h3><dl><div><dt>효과 정의</dt><dd>{selected.unit||'정량 효과 미보고'}</dd></div><div><dt>효과량</dt><dd>{f(selected.effect)}</dd></div><div><dt>원본 P / FDR</dt><dd>{f(selected.p)} / {f(selected.q)}</dd></div><div><dt>신뢰구간</dt><dd>{selected.reg.ci_low!=null&&selected.reg.ci_high!=null?`${f(selected.reg.ci_low)} – ${f(selected.reg.ci_high)}`:'미보고'}</dd></div><div><dt>가설군</dt><dd>{selected.reg.hypothesis_family||'미보고'}</dd></div><div><dt>원문 위치</dt><dd>{selected.reg.source_ref||'미확인'}</dd></div></dl></>}
          {selected.exposure&&<><h3>실험 노출·적용 조건</h3><MatchingBadge identity={selected.exposure.mapping_identity}/><p className="ws-mapping-note">{selected.exposure.mapping_identity.note||selected.exposure.mapping_identity.label}</p><p>{selected.context}</p>{contrastConditions(selected.exposure,selected.contrast).map((a,i)=><div key={i} className={`wb-condition ${a.status}`}><b>{a.dimension}</b><span>{a.status==='match'?'일치':a.status==='mismatch'?'불일치':'미확인'}</span><p>{a.reason}</p></div>)}<button className="wb-primary" onClick={()=>{setTab('omics');setMobilePanel('workspace')}}><Dna size={16}/>이 대비의 전체 결과</button></>}
          {link(source?.url)&&<a className="wb-source-link" href={link(source?.url)} target="_blank" rel="noreferrer"><ExternalLink size={15}/>원본 자료</a>}<details><summary>출처 버전·한계·원본 필드</summary><dl>{Object.entries(source||{}).filter(([k])=>!k.includes('path')).map(([k,v])=><div key={k}><dt>{k}</dt><dd>{f(v)}</dd></div>)}</dl><p>{f(selected.reg?.context?.limitations||selected.exposure?.limitations)}</p></details>
        </>:seed?<>
          <EntityBadge entity={seed}/><h2>{seed.label}</h2><p className="wb-identifier">{seed.id}</p><GeneIdentifiers detail={detail} onSelect={selectEntity} onError={setError}/><p className="ws-detail-hint">근거 행을 선택하면 연구 조건과 원문 출처가 여기에 표시됩니다.</p>
          {seed.kind==='food'&&<section className="ws-meal-card"><h3>성분 섭취량</h3><p><strong>{intake} g</strong> · 원본 함량 기준이 일치하는 성분을 계산합니다.</p><button className="wb-primary" disabled={offline||mealBusy||!mealFoodId} onClick={()=>void calculateMeal()}>{mealBusy?<LoaderCircle className="wb-loading" size={16}/>:<SlidersHorizontal size={16}/>} {mealBusy?'성분량 계산 중':'성분 섭취량 계산'}</button>{offline?<small>독립 HTML에서 새 섭취량은 계산하지 않습니다.</small>:detail&&!mealFoodId?<small>기준 음식 ID의 대응이 확인되지 않았습니다.</small>:null}{meal&&<MealSummary key={`${seed.id}:${intake}`} meal={meal}/>}</section>}
          <h3>선택 항목의 수록 범위</h3><dl><div><dt>직접 발현 결과</dt><dd>{detail?`${detail.effects?.length||0}개 표시`:'조회 중'}</dd></div><div><dt>외부·임상 관계</dt><dd>{detail?(offline?`원본 ${detail.total_regulatory_evidence||0}개 · 내장 ${detail.regulatory_evidence?.length||0}개`:`${detail.total_regulatory_evidence||0}개`):'조회 중'}</dd></div></dl>{detail?.gaps?.map(g=><p className="wb-muted" key={g}>{g}</p>)}
        </>:<div className="ws-inspector-empty"><BookOpen size={28}/><h3>근거의 맥락을 확인하는 곳</h3><p>항목 또는 근거를 선택하면 원본 식별자·연구 조건·출처가 표시됩니다.</p><div><span>01</span>항목 선택</div><div><span>02</span>연구·관계 확인</div><div><span>03</span>원본 출처 확인</div></div>}
      </aside>
    </div>
    <footer className="wb-footer"><span><Info size={13}/>출처 확인과 전문가 검수는 별도 상태입니다.</span><span>개인별 건강·질병 위험 예측 없음</span></footer>
  </div>;
}

function GitBranchMark(){return <Network size={22} strokeWidth={1.8}/>}
