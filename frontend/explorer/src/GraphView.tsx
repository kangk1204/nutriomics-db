import { useEffect, useMemo, useRef, useState } from 'react';
import cytoscape, { type Core, type StylesheetJson } from 'cytoscape';
import dagre from 'cytoscape-dagre';
import { Focus, LockKeyhole, Minus, Plus, UnlockKeyhole, Network, Expand, LoaderCircle, GitBranch, Maximize2, Minimize2, Search, ArrowUpRight, Download, X, ChevronRight } from 'lucide-react';
import { normalizeKind } from './api';
import { graphTiming } from './renderMetrics';
import type { Entity, EvidenceEdge, GraphData, GraphPath } from './types';
import { weakestLinkLabel } from './graphFocus';
import { bundleEdges, projectNetwork, semanticPositions, focusPositions, restorePinned, entityType, tierClass, tierLabel, laneOf, LANE_LABELS, edgeContext, topologyKey, type NetworkMode } from './networkModel';
import './network.css';

cytoscape.use(dagre);
export const relationLabel = (value: string) => ({
  contains_nutrient: '영양성분 함유', contains_analytical_flavonoid: '분석된 flavonoid',
  chemical_gene_influence: '화합물–유전자 관계', chemical_disease_marker_mechanism: '질환 관련 기전',
  chemical_disease_therapeutic: '질환 관련 연구', gene_disease_marker_mechanism: '질환 관련성',
  gene_disease_therapeutic: '질환 관련 연구', annotated_to_pathway: '경로 주석',
  source_identifier_cross_reference: '식별자 대응', restricted_source_reference: '상세 내보내기 제한',
  exposure_gene_effect: '중재 발현 관찰', exposure_outcome: '건강지표 관찰',
  candidate_name_correspondence: '후보 이름 대응', component_of_intervention: '중재 구성 성분',
  eqtm_association:'CpG–발현 연관', enhancer_gene_prediction:'enhancer–gene 예측',
  predicted_ccre_gene_link:'cCRE–유전자 계산 예측',
  experimentally_tested_ccre_gene_pair:'조절 실험의 시험된 쌍',
  increases:'증가',decreases:'감소',associated_with:'연관',affects:'영향 관계',
}[value] || value.replaceAll('_', ' '));
const STYLES:StylesheetJson=[
  {selector:'node',style:{width:34,height:34,shape:'ellipse','background-color':'#edf1f7','border-width':1.6,'border-color':'#62758c',label:'data(label)',color:'#263b51','font-family':'Segoe UI, Malgun Gothic, sans-serif','font-size':16,'font-weight':550,'text-valign':'bottom','text-margin-y':9,'text-wrap':'wrap','text-max-width':'130px','text-background-color':'#fbfcf9','text-background-opacity':0.96,'text-background-padding':'3px','overlay-opacity':0}},
  {selector:'node[kind="food"]',style:{shape:'round-rectangle','background-color':'#fbecdb','border-color':'#a66d26'}},
  {selector:'node[kind="component"]',style:{'background-color':'#dceee9','border-color':'#377e70'}},
  {selector:'node[kind="gene"]',style:{shape:'round-rectangle',width:42,height:30,'background-color':'#e9e2f2','border-color':'#796195'}},
  {selector:'node[kind="health"]',style:{shape:'round-diamond',width:42,height:42,'background-color':'#dfebfa','border-color':'#4e75a2'}},
  {selector:'node[kind="pathway"]',style:{shape:'hexagon','background-color':'#e6eaf0','border-color':'#708096'}},
  {selector:'node[kind="cpg"]',style:{shape:'diamond','background-color':'#eee3f2','border-color':'#8c7297'}},
  {selector:'node[kind="regulatory_region"]',style:{shape:'hexagon','background-color':'#deebee','border-color':'#647d8b'}},
  {selector:'node.anchor',style:{'border-width':2.5,width:58,height:44,'font-size':20,'font-weight':700,'text-margin-y':13}},
  {selector:'edge',style:{width:1.35,'line-color':'#b2becb','curve-style':'bezier','control-point-step-size':38,'target-arrow-shape':'none',label:'data(displayLabel)','font-family':'Segoe UI, Malgun Gothic, sans-serif','font-size':11,color:'#53677b','text-wrap':'wrap','text-max-width':'95px','text-background-color':'#fff','text-background-opacity':0.96,'text-background-padding':'3px','overlay-opacity':0}},
  {selector:'edge[stroke="indirect"]',style:{'line-style':'dashed','line-dash-pattern':[5,4],'line-color':'#aa9fba'}},
  {selector:'edge[stroke="extracted"]',style:{'line-style':'dotted','line-color':'#a899b8'}},
  {selector:'edge[relation="candidate_name_correspondence"]',style:{'text-margin-x':-58,'text-margin-y':3}},
  {selector:'.muted',style:{opacity:0.22}},
  {selector:'node.path-member',style:{'border-width':2.4,'font-weight':600}},
  {selector:'edge.path-member',style:{width:2,'line-color':'#6d7e93'}},
  {selector:'node:selected',style:{'border-width':3.5,'border-color':'#173e64','background-blacken':-0.04,'underlay-color':'#bfd2e6','underlay-opacity':0.3,'underlay-padding':7,'font-weight':700,'z-index':30}},
  {selector:'edge:selected',style:{width:3,'line-color':'#3d5f83',label:'data(detailLabel)','z-index':30}},
  {selector:'.hovered',style:{opacity:1,'z-index':40}},
  {selector:'edge.hovered',style:{width:2.8,label:'data(detailLabel)','line-color':'#4c6b8c'}},
  {selector:'node.pinned',style:{'border-style':'double','border-width':4}},
];
interface Props {
  graph:GraphData|null; loading:boolean; error?:string; seedId:string; selectedId?:string; selectedPath?:GraphPath;
  onNode:(node:Entity)=>void; onEdge:(edge:EvidenceEdge,groupedIds:string[])=>void;
  onExpand:()=>void|Promise<void>; canExpand:boolean; onCenter?:(node:Entity)=>void; onRetry?:()=>void; onExport?:(graph:GraphData)=>void;
}
export default function GraphView({graph,loading,error,seedId,selectedId,selectedPath,onNode,onEdge,onExpand,canExpand,onCenter,onRetry,onExport}:Props){
  const host=useRef<HTMLDivElement>(null),cyRef=useRef<Core|null>(null);
  const callbacks=useRef({onNode,onEdge});callbacks.current={onNode,onEdge};
  const positions=useRef<Record<string,{x:number;y:number}>>(window.__NUTRIOMICS_SNAPSHOT__?.positions||{});
  const pinned=useRef(new Set<string>()),geometryKey=useRef(''),[pinActive,setPinActive]=useState(false);
  const graphIds=useRef<string[]>([]);graphIds.current=graph?.nodes.map(n=>n.id)||[];
  const publishPositions=()=>{window.__NUTRIOMICS_POSITIONS__=Object.fromEntries(graphIds.current.filter(id=>positions.current[id]).map(id=>[id,positions.current[id]]))};
  const [mode,setMode]=useState<NetworkMode>(window.__NUTRIOMICS_SNAPSHOT__?'all':'path'),[layout,setLayout]=useState('focus'),[pathId,setPathId]=useState('');
  const [localSelected,setLocalSelected]=useState(selectedId||seedId),[expanded,setExpanded]=useState(false),[query,setQuery]=useState(''),[labels,setLabels]=useState(false),[hidden,setHidden]=useState<Set<string>>(new Set());
  const [listOpen,setListOpen]=useState(false),[hovered,setHovered]=useState<{label:string;type:string}|null>(null);
  const [expanding,setExpanding]=useState(false),expandSequence=useRef(0);
  const atLimit=Boolean(graph&&(graph.nodes.length>=250||graph.edges.length>=750));
  const activePath=graph?.paths.find(p=>p.id===pathId)||selectedPath||graph?.paths[0];
  const shown=useMemo(()=>graph?projectNetwork(graph,seedId,mode,activePath,hidden):null,[graph,seedId,mode,activePath,hidden]);
  const bundles=useMemo(()=>bundleEdges(shown?.edges||[]),[shown]);
  const visiblePath=mode==='path'&&activePath&&shown?.paths.some(p=>p.id===activePath.id)?activePath:undefined;
  const selectedNode=shown?.nodes.find(n=>n.id===localSelected);
  const selectedBundle=bundles.find(b=>b.ids.includes(localSelected));
  const nodeNames=new Map(graph?.nodes.map(n=>[n.id,n.label])||[]);
  const matches=shown?.nodes.filter(n=>[n.label,n.id,...(n.aliases||[])].join(' ').toLowerCase().includes(query.toLowerCase()))||[];
  useEffect(()=>{setLocalSelected(selectedId||seedId)},[selectedId,seedId]);
  useEffect(()=>{expandSequence.current++;setExpanding(false);setPathId('');setQuery('');setHidden(new Set());setMode(window.__NUTRIOMICS_SNAPSHOT__?'all':'path')},[seedId]);
  useEffect(()=>{if(selectedPath){setPathId(selectedPath.id);setMode('path')}},[selectedPath]);
  useEffect(()=>{
    if(!expanded)return;
    const close=(event:KeyboardEvent)=>{if(event.key==='Escape')setExpanded(false)};
    document.addEventListener('keydown',close);return()=>document.removeEventListener('keydown',close);
  },[expanded]);
  useEffect(()=>{
    if(!host.current)return;
    const cy=cytoscape({container:host.current,style:STYLES,minZoom:0.04,maxZoom:4,boxSelectionEnabled:false});
    cyRef.current=cy;window.__NUTRIOMICS_CY__=cy;
    cy.on('tap','node',event=>{const node=event.target.data('entity') as Entity;setLocalSelected(node.id);callbacks.current.onNode(node)});
    cy.on('tap','edge',event=>{const edge=event.target.data('evidence') as EvidenceEdge;setLocalSelected(edge.id);callbacks.current.onEdge(edge,event.target.data('groupedIds'))});
    cy.on('mouseover','node',event=>{const n=event.target.data('entity') as Entity;event.target.closedNeighborhood().addClass('hovered');setHovered({label:n.label,type:`${entityType(n.kind)} · ${n.id}`})});
    cy.on('mouseover','edge',event=>{const e=event.target.data('evidence') as EvidenceEdge;event.target.addClass('hovered');setHovered({label:relationLabel(e.relation),type:tierLabel(e.tier)})});
    cy.on('mouseout','node,edge',()=>{cy.elements().removeClass('hovered');setHovered(null)});
    cy.on('dragfree','node',event=>{positions.current[event.target.id()]={...event.target.position()};publishPositions()});
    let previousWidth=host.current.clientWidth,previousHeight=host.current.clientHeight;
    const observer=new ResizeObserver(()=>{const w=host.current?.clientWidth||0,h=host.current?.clientHeight||0;cy.resize();if(w>0&&h>0&&(Math.abs(w-previousWidth)>40||Math.abs(h-previousHeight)>40)&&cy.nodes().length)cy.fit(undefined,24);previousWidth=w;previousHeight=h});observer.observe(host.current);
    return()=>{observer.disconnect();cy.destroy();cyRef.current=null;delete window.__NUTRIOMICS_CY__};
  },[]);
  useEffect(()=>{
    const cy=cyRef.current;if(!cy)return;if(!shown||!graph){cy.elements().remove();return}
    const start=performance.now();cy.nodes().forEach(n=>{positions.current[n.id()]={...n.position()}});
    const key=`${seedId}:${mode}:${layout}:${visiblePath?.id||''}:${topologyKey(graph)}`;
    const keepGeometry=geometryKey.current===key;geometryKey.current=key;
    cy.startBatch();cy.elements().remove();
    cy.add(shown.nodes.map(n=>({group:'nodes' as const,data:{id:n.id,label:n.display_name_ko||n.label,kind:normalizeKind(n.kind),subtype:n.kind,entity:n},classes:n.id===seedId?'anchor':''})));
    cy.add(bundles.map(b=>({group:'edges' as const,data:{id:b.id,source:b.edge.subject,target:b.edge.object,relation:b.edge.relation,stroke:tierClass(b.edge.tier),detailLabel:`${relationLabel(b.edge.relation)}${b.ids.length>1?` · ${b.ids.length} 기록`:''}`,displayLabel:'',evidence:b.edge,groupedIds:b.ids}})));
    const defaults=layout==='focus'&&!visiblePath?focusPositions(shown.nodes,shown.edges,seedId):semanticPositions(shown.nodes,visiblePath);
    const resolved=restorePinned(defaults,positions.current,pinned.current);
    cy.nodes().forEach(n=>{const saved=positions.current[n.id()];n.position(keepGeometry&&saved?saved:resolved[n.id()]);if(pinned.current.has(n.id()))n.lock().addClass('pinned')});
    cy.endBatch();
    if(layout==='cose'&&!keepGeometry)cy.layout({name:'cose',animate:false,fit:false,nodeRepulsion:()=>14000,idealEdgeLength:()=>150,nodeDimensionsIncludeLabels:true,padding:50} as cytoscape.LayoutOptions).run();
    if(window.__NUTRIOMICS_SNAPSHOT__?.positions&&!keepGeometry&&!geometryKey.current.includes(':cose:'))cy.nodes().forEach(n=>{const p=window.__NUTRIOMICS_SNAPSHOT__?.positions?.[n.id()];if(p)n.position(p)});
    cy.fit(undefined,24);if(cy.zoom()>1.15)cy.zoom(1.15);cy.center();
    cy.nodes().forEach(n=>{positions.current[n.id()]={...n.position()}});publishPositions();
    const layoutMs=performance.now()-start;
    let inner=0;const frame=requestAnimationFrame(()=>{inner=requestAnimationFrame(()=>{
      const timing=graphTiming(graph,start,performance.now(),layoutMs,shown.nodes.length);window.__NUTRIOMICS_RENDER_TIMING__=timing;
      if(host.current)host.current.dataset.renderTiming=JSON.stringify(timing);
    })});return()=>{cancelAnimationFrame(frame);cancelAnimationFrame(inner)};
  },[shown,bundles,seedId,mode,layout,visiblePath,graph]);
  useEffect(()=>{
    const cy=cyRef.current;if(!cy)return;cy.elements().unselect().removeClass('muted path-member');
    const pnodes=new Set(visiblePath?.node_ids||[]),pedges=new Set(visiblePath?.edge_ids||[]);
    cy.nodes().forEach(n=>{if(visiblePath)n.addClass(pnodes.has(n.id())?'path-member':'muted')});
    cy.edges().forEach(e=>{const active=(e.data('groupedIds') as string[]).some(id=>pedges.has(id));if(visiblePath)e.addClass(active?'path-member':'muted');e.data('displayLabel',labels||active?e.data('detailLabel'):'')});
    if(localSelected){cy.getElementById(localSelected).select();cy.edges().forEach(e=>{if((e.data('groupedIds') as string[]).includes(localSelected))e.select()})}
    if(!visiblePath&&localSelected&&localSelected!==seedId){const chosen=cy.getElementById(localSelected);if(chosen.isNode())cy.elements().difference(chosen.closedNeighborhood()).addClass('muted')}
    setPinActive(pinned.current.has(localSelected));
  },[shown,bundles,visiblePath,localSelected,labels,layout]);
  const chooseNode=(node:Entity)=>{setLocalSelected(node.id);callbacks.current.onNode(node);const cy=cyRef.current;if(cy){cy.center(cy.getElementById(node.id))}};
  const chooseEdge=(bundle:typeof bundles[number])=>{setLocalSelected(bundle.id);callbacks.current.onEdge(bundle.edge,bundle.ids)};
  const pin=()=>{const n=cyRef.current?.getElementById(localSelected);if(!n?.isNode())return;if(pinned.current.has(localSelected)){pinned.current.delete(localSelected);n.unlock().removeClass('pinned');setPinActive(false)}else{pinned.current.add(localSelected);positions.current[localSelected]={...n.position()};n.lock().addClass('pinned');setPinActive(true)}};
  const zoom=(factor:number)=>{const cy=cyRef.current;if(cy)cy.zoom({level:cy.zoom()*factor,renderedPosition:{x:cy.width()/2,y:cy.height()/2}})};
  const toggleTier=(tier:string)=>setHidden(old=>{const next=new Set(old);next.has(tier)?next.delete(tier):next.add(tier);return next});
  const expand=async()=>{if(expanding||atLimit)return;const sequence=++expandSequence.current;setExpanding(true);setMode('all');try{await onExpand()}finally{if(sequence===expandSequence.current)setExpanding(false)}};
  return <section className={`ng-workspace ${expanded?'ng-expanded':''}`} aria-label="연결망 시각화">
    <header className="ng-toolbar"><div className="ng-view-switch" role="group" aria-label="네트워크 표시 범위"><button aria-pressed={mode==='path'} onClick={()=>setMode('path')}><GitBranch size={15}/>경로 탐색</button><button aria-pressed={mode==='neighbors'} onClick={()=>setMode('neighbors')}><Focus size={15}/>인접 관계</button><button aria-pressed={mode==='all'} onClick={()=>setMode('all')}><Network size={15}/>전체 연결</button></div><div className="ng-toolbar-right"><select aria-label="연결망 배치" value={layout} onChange={e=>setLayout(e.target.value)}><option value="focus">중심 배치</option><option value="semantic">유형별 배치</option><option value="cose">자유 배치</option></select><button title={atLimit?'수신 상한 · 250 노드 또는 750 관계':'주변 확장 · 최대 250 노드'} disabled={!canExpand||loading||expanding||atLimit} onClick={()=>{void expand()}}>{expanding?<LoaderCircle className="spin" size={16}/>:<Expand size={16}/>}<span>{expanding?'수신 중':atLimit?'상한':'확장'}</span></button><button aria-label={expanded?'확대 작업공간 닫기':'네트워크 작업공간 확대'} onClick={()=>setExpanded(!expanded)}>{expanded?<Minimize2 size={17}/>:<Maximize2 size={17}/>}</button></div></header>
    <div className="ng-context"><span className="ng-context-title">관계 탐색</span><b>{shown?.nodes.length||0}</b><span>노드</span><b>{shown?.edges.length||0}</b><span>관계 기록</span><span className="ng-context-end">{graph?.release_id}</span></div>
    {graph?.edges.some(e=>e.tier==='restricted')&&<div className="ng-boundary">이용조건에 따라 {graph.edges.filter(e=>e.tier==='restricted').length}개 관계의 상세를 출처 링크로 대체했습니다.</div>}
    <div className="ng-body">
      <div className="ng-canvas-wrap"><div className="ng-type-legend">{[...new Set(shown?.nodes.map(laneOf)||[])].sort((a,b)=>a-b).map(lane=><span key={lane}><i className={`ng-glyph ng-lane-${lane}`}/>{LANE_LABELS[lane]}</span>)}</div>
        <div className="ng-search"><Search size={14}/><input aria-label="표시된 네트워크 검색" placeholder="표시된 노드 검색" value={query} onChange={e=>setQuery(e.target.value)}/>{query&&<button aria-label="네트워크 검색 지우기" onClick={()=>setQuery('')}><X size={13}/></button>}{query&&<div className="ng-search-results">{matches.length?matches.slice(0,12).map(n=><button key={n.id} onClick={()=>{chooseNode(n);setQuery('')}}><span>{n.label}<small>{entityType(n.kind)} · {n.id}</small></span><ChevronRight size={13}/></button>):<p>표시 범위에 일치하는 노드 없음</p>}</div>}</div>
        <div ref={host} className="ng-canvas" role="img" aria-label="선택한 항목과 관련 근거의 네트워크. 표와 상세 패널에서도 같은 자료를 확인할 수 있습니다."/>
        {loading&&<div className="ng-empty" role="status"><LoaderCircle className="spin" size={20}/><b>연결망을 불러오는 중</b></div>}
        {!loading&&!shown?.nodes.length&&<div className="ng-empty"><Network size={26}/><b>{error?'연결 자료를 불러오지 못했습니다':'표시할 연결 근거 없음'}</b><span>{error||'항목을 선택하거나 근거 필터를 확인하세요.'}</span>{error&&onRetry&&<button onClick={onRetry}>연결망 다시 불러오기</button>}</div>}
        <div className="ng-canvas-tools"><button aria-label="연결망 확대" onClick={()=>zoom(1.25)}><Plus size={16}/></button><button aria-label="연결망 축소" onClick={()=>zoom(0.8)}><Minus size={16}/></button><button aria-label="연결망 화면 맞춤" onClick={()=>cyRef.current?.fit(undefined,24)}><Focus size={16}/></button><button aria-label={pinActive?'위치 고정 해제':'위치 고정'} disabled={!selectedNode} aria-pressed={pinActive} onClick={pin}>{pinActive?<LockKeyhole size={16}/>:<UnlockKeyhole size={16}/>}</button><button aria-label="표시된 네트워크 HTML 저장" title="표시된 관계와 허용된 근거를 HTML로 저장" disabled={!shown?.nodes.length||!onExport} onClick={()=>{if(shown)onExport?.(shown)}}><Download size={16}/></button></div>
        <div className="ng-hover" aria-live="polite">{hovered?<><b>{hovered.label}</b><span>{hovered.type}</span></>:<span>노드 선택 · 관계선에서 근거 확인 · 드래그로 위치 변경</span>}</div>
      </div>
      <aside className="ng-paths" aria-label="후보 경로와 선택 상세"><div className="ng-rail-heading"><span>{mode==='path'?'후보 경로':'선택한 항목'}</span><small>{graph?.paths.length||0} 경로</small></div>
        {mode==='path'&&graph?.paths.length? <div className="ng-path-list">{graph.paths.map((p,i)=>{const valid=shown?.paths.some(v=>v.id===p.id);return <button key={p.id} aria-pressed={activePath?.id===p.id} disabled={!valid} onClick={()=>setPathId(p.id)}><span className="ng-path-number">{String(i+1).padStart(2,'0')}</span><span><b>{nodeNames.get(p.node_ids.at(-1)||'')||'건강 관련 경로'}</b><small>{p.edge_ids.length} 단계 · {nodeNames.get(p.node_ids.find(id=>graph.nodes.find(n=>n.id===id)?.kind==='gene')||'')||'유전자 경유 없음'}</small></span></button>})}</div>:mode==='path'?<p className="ng-rail-note">현재 범위에 완전한 건강 경로가 없습니다. 실제 인접 관계를 표시합니다.</p>:null}
        {visiblePath&&<div className="ng-path-summary"><span className="ng-kicker">조합한 후보 경로</span><ol>{visiblePath.node_ids.map(id=><li key={id}><button onClick={()=>{const n=graph?.nodes.find(n=>n.id===id);if(n)chooseNode(n)}}>{nodeNames.get(id)||id}<small className="ng-step-type">{entityType(graph?.nodes.find(n=>n.id===id)?.kind||'')}</small></button></li>)}</ol><p>{weakestLinkLabel(visiblePath,graph)}</p><small>연결 전체의 인체 효과는 검증되지 않았습니다.</small></div>}
        {selectedNode&&<div className="ng-selection"><span className="ng-kicker">{entityType(selectedNode.kind)}</span><h3>{selectedNode.label}</h3><code>{selectedNode.id}</code><p>표시 범위 내 {shown?.edges.filter(e=>e.subject===selectedNode.id||e.object===selectedNode.id).length||0}개 관계 기록</p>{onCenter&&selectedNode.id!==seedId&&<button className="ng-center" onClick={()=>onCenter(selectedNode)}>이 노드를 중심으로 탐색 <ArrowUpRight size={13}/></button>}</div>}
        {selectedBundle&&<div className="ng-selection"><span className="ng-kicker">{tierLabel(selectedBundle.edge.tier)}</span><h3>{relationLabel(selectedBundle.edge.relation)}</h3><p className="ng-endpoints">{nodeNames.get(selectedBundle.edge.subject)}<br/><span>↔</span><br/>{nodeNames.get(selectedBundle.edge.object)}</p><dl>{edgeContext(selectedBundle.edge).map(r=><div key={r.label}><dt>{r.label}</dt><dd>{r.value}</dd></div>)}</dl><p>{selectedBundle.ids.length}개 기록 · 고유 PMID {selectedBundle.pmids.length}개</p><small>집계 단위: 관계 기록 · 출처와 원문은 근거 상세에서 확인</small>{selectedBundle.ids.length>1&&<div className="ng-records">{selectedBundle.records.map(e=><button key={e.id} onClick={()=>{setLocalSelected(e.id);callbacks.current.onEdge(e,selectedBundle.ids)}}>{e.id}<ChevronRight size={12}/></button>)}</div>}</div>}
        {!visiblePath&&!selectedNode&&!selectedBundle&&<p className="ng-rail-note">노드 또는 관계를 선택해 맥락을 확인하세요.</p>}
      </aside>
    </div>
    <div className="ng-evidence-legend"><span>관계 표시</span>{[['recorded','출처 수록'],['indirect','간접·후보·예측'],['extracted','자동 추출']].map(([key,label])=><button key={key} aria-pressed={!hidden.has(key)} onClick={()=>toggleTier(key)}><i className={`ng-line ng-line-${key}`}/>{label}</button>)}<label><input type="checkbox" checked={labels} onChange={e=>setLabels(e.target.checked)}/>관계명</label><button className="ng-list-toggle" aria-expanded={listOpen} onClick={()=>setListOpen(!listOpen)}>노드·관계 목록</button></div>
    {listOpen&&<div className="ng-accessible-list"><div><h4>노드 {shown?.nodes.length||0}</h4>{shown?.nodes.map(n=><button key={n.id} aria-pressed={localSelected===n.id} onClick={()=>chooseNode(n)}>{n.label}<small>{entityType(n.kind)} · {n.id}</small></button>)}</div><div><h4>관계 묶음 {bundles.length}</h4>{bundles.map(b=><button key={b.id} aria-pressed={b.ids.includes(localSelected)} onClick={()=>chooseEdge(b)}>{nodeNames.get(b.edge.subject)} ↔ {nodeNames.get(b.edge.object)}<small>{relationLabel(b.edge.relation)} · {tierLabel(b.edge.tier)} · {b.ids.length} 기록</small></button>)}</div></div>}
    <footer className="ng-footer"><span>관계 유형과 출처는 근거 상세에서 확인</span><span>{shown?.nodes.length||0}/{graph?.nodes.length||0} 수신 노드{graph?.truncated?` · 추가 생략 ≥${graph.omitted?.nodes||0} 노드 / ≥${graph.omitted?.edges||0} 관계`:''}</span></footer>
  </section>;
}
