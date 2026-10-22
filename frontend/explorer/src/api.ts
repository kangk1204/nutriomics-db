import type { Entity, EvidenceEdge, EvidencePage, ExportRequest, Filters, GraphData, InterventionRecord, Kind, Locus, Meta, NodeDetail, ScientificContext, ScientificMatchResult, Snapshot, WorkbenchEvidence } from './types';
import { recordGraphReceipt } from './renderMetrics';
import { exposureRecord, interventionEntity, interventionMatches, mergeCatalogPreview, type SearchKind } from './catalogSearch';

export const offline = Boolean(window.__NUTRIOMICS_SNAPSHOT__);
const snapshot = window.__NUTRIOMICS_SNAPSHOT__;
export const frozenScientificContext = snapshot?.filters?.scientific_context;
export const scientificSnapshotSeed = frozenScientificContext ? snapshot?.nodes.find(node => node.id === frozenScientificContext.seed_id) : undefined;
const ROOT = '/api/v1/explorer';
export const REQUEST_TIMEOUT_MS = 30_000;
export const EXPORT_TIMEOUT_MS = 60_000;

export class RequestTimeoutError extends Error {
  constructor(timeoutMs: number) {
    super(`서버 요청이 ${Math.ceil(timeoutMs / 1000)}초 이내 완료되지 않았습니다. 서버 연결을 확인한 뒤 다시 시도해주세요.`);
    this.name = 'RequestTimeoutError';
  }
}

/** One deadline covers both response headers and the complete decoded body. */
export async function fetchWithTimeout<T>(url: string, options: RequestInit, read: (response: Response) => Promise<T>, timeoutMs = REQUEST_TIMEOUT_MS): Promise<T> {
  const external = options.signal;
  const cancelled = () => new DOMException('요청이 취소되었습니다.', 'AbortError');
  if (external?.aborted) throw cancelled();
  const controller = new AbortController();
  let rejectInterrupted!: (reason: Error) => void;
  const interrupted = new Promise<never>((_, reject) => { rejectInterrupted = reject; });
  const onAbort = () => {
    const error = cancelled();
    rejectInterrupted(error);
    controller.abort(error);
  };
  external?.addEventListener('abort', onAbort, { once: true });
  const timer = setTimeout(() => {
    const error = new RequestTimeoutError(timeoutMs);
    rejectInterrupted(error);
    controller.abort(error);
  }, timeoutMs);
  const work = (async () => {
    let response: Response;
    try {
      response = await fetch(url, { ...options, signal: controller.signal });
    } catch (failure) {
      if (controller.signal.aborted) throw controller.signal.reason;
      if (failure instanceof TypeError) throw new Error('서버에 연결하지 못했습니다. 연결 상태를 확인한 뒤 다시 시도해주세요.');
      throw failure;
    }
    if (controller.signal.aborted) throw controller.signal.reason;
    return read(response);
  })();
  try {
    return await Promise.race([work, interrupted]);
  } finally {
    clearTimeout(timer);
    external?.removeEventListener('abort', onAbort);
  }
}
export const normalizeKind = (kind: string): Kind =>
  ['cpg', 'methylation_probe'].includes(kind) ? 'cpg' : kind === 'regulatory_region' ? 'regulatory_region' :
  ['food', 'diet', 'exposure'].includes(kind) ? 'food' :
  ['chemical', 'nutrient', 'component', 'compound'].includes(kind) ? 'component' :
  ['health', 'disease', 'outcome', 'clinical_endpoint'].includes(kind) ? 'health' :
  ['pathway', 'omics'].includes(kind) ? 'pathway' : 'gene';

async function request<T>(path: string, signal?: AbortSignal): Promise<T> {
  return fetchWithTimeout(`${ROOT}${path}`, { signal }, async response => {
    if (!response.ok) throw await serverError(response);
    return response.json() as Promise<T>;
  });
}

async function serverError(response: Response): Promise<Error> {
  let code: string | undefined;
  try { code = (await response.json())?.detail?.code; } catch { /* Non-JSON error responses keep the HTTP status. */ }
  const messages: Record<string, string> = {
    query_timeout: '조회 시간 한도를 초과했습니다. 결과 0건을 의미하지 않습니다. 잠시 뒤 다시 시도해주세요.',
    source_unavailable: '원본 자료를 현재 조회할 수 없습니다. 근거 부재나 결과 0건을 의미하지 않습니다.',
    analysis_key_mismatch: '원본 분석과 연결 정보가 일치하지 않습니다. 결과를 확인할 때까지 표시하지 않습니다.',
    source_changed: '조회 중 데이터 버전이 변경되었습니다. 다시 조회해주세요.',
  };
  return new Error((code && messages[code]) || `자료를 불러오지 못했습니다 (${response.status}). 연결과 release를 확인해주세요.`);
}
export async function getWorkbenchEvidence(seedId: string, layer: string, tissue: string, page: number, signal?: AbortSignal): Promise<EvidencePage> {
  if (snapshot) {
    const all = snapshot.node_details?.[seedId]?.regulatory_evidence || [];
    const rows = all.filter(r => (layer === 'all' || r.layer === layer) && (!tissue || r.tissue === tissue));
    return {release_id: snapshot.release_id, items: rows.slice((page-1)*25,page*25), total: rows.length, page, page_size: 25, status: 'snapshot'};
  }
  return request<EvidencePage>(`/evidence?${new URLSearchParams({seed_id:seedId,layer,tissue,page:String(page),page_size:'25'})}`, signal);
}
export async function getWorkbenchRecord(id:string):Promise<WorkbenchEvidence> {
  if (snapshot) {
    for (const detail of Object.values(snapshot.node_details||{})) {
      const record=detail.regulatory_evidence?.find(r=>r.id===id);
      if (record) return record;
    }
    throw new Error('이 snapshot에는 해당 근거의 허용된 상세 값이 없습니다.');
  }
  return request<WorkbenchEvidence>(`/evidence/${encodeURIComponent(id)}`);
}
export async function getLoci(seedId: string): Promise<{items:Locus[];truncated?:boolean;unverified_or_unlocated_in_page?:number}> {
  if (snapshot) {
    const items: Locus[] = [];
    for (const r of snapshot.node_details?.[seedId]?.regulatory_evidence || []) {
      const match = String(r.context?.source_interval || '').match(/^(chr[0-9XYM]+)_(\d+)_(\d+)$/);
      if (match && r.context?.genome_build === 'GRCh38/hg38') items.push({evidence_id:r.id,subject_id:r.subject_id,object_id:r.object_id,chromosome:match[1],start:Number(match[2]),end:Number(match[3]),layer:r.layer,tissue:r.tissue,source:r.source,coordinate_convention:'source_native'});
    }
    return {items};
  }
  return request(`/loci?${new URLSearchParams({seed_id:seedId})}`);
}
export async function getOmicsResults(c: {series_accession?:string;contrast?:string;analysis_version?:string}, featureType:string,page:number) {
  if (snapshot) return {items:[],total:null,status:'not_embedded',feature_type:undefined,available_feature_types:[] as string[]};
  return request<{items:Record<string,unknown>[];total:number|null;status:string;feature_type?:string;available_feature_types?:string[]}>(`/omics/results?${new URLSearchParams({series:c.series_accession||'',feature_type:featureType,contrast:c.contrast||'',analysis_version:c.analysis_version||'',page:String(page),page_size:'25'})}`);
}
export async function getMeta(): Promise<Meta> {
  if (snapshot) return {
    ...snapshot.meta,
    release_id: snapshot.release_id, created_at: snapshot.created_at || '',
    counts: { nodes: snapshot.nodes.length, edges: snapshot.edges.length,
      by_kind: snapshot.nodes.reduce<Record<string, number>>((counts, node) => ({ ...counts, [node.kind]: (counts[node.kind] || 0) + 1 }), {}) },
    domains: [ { id: 'blood_pressure', label: '혈압' }, { id: 'lipids', label: '지질' }, { id: 'obesity', label: '비만' } ],
    gaps: snapshot.gaps,
  };
  return request<Meta>('/meta');
}
function eligibleEdge(edge: EvidenceEdge, filters: Filters) {
  const tiers: Record<string, string[]> = {
    observed: ['observed', 'observed_composition', 'observed_intervention', 'human_randomized', 'human_nonrandomized', 'human_observational', 'composition'],
    curated: ['curated', 'curated_mechanism'], candidate: ['candidate', 'inferred', 'predicted', 'regulatory_identity'], extracted: ['extracted'],
  };
  return (filters.domain === 'all' || !edge.domains?.length || edge.domains.includes(filters.domain)) &&
    (filters.tier === 'all' || edge.tier === 'restricted' || tiers[filters.tier]?.includes(edge.tier)) &&
    (filters.include_extracted || filters.tier === 'extracted' || edge.tier !== 'extracted');
}
export async function getEntities(kind: Kind | 'food_record' | 'exposure', q: string, filters: Filters, page: number, pageSize: number, sort: string, order: string, signal?: AbortSignal) {
  if (snapshot) {
    const query = q.trim().toLocaleLowerCase();
    const allowedEdges = snapshot.edges.filter(edge => eligibleEdge(edge, filters));
    const allowedIds = new Set(allowedEdges.flatMap(edge => [edge.subject, edge.object]));
    const extractedIds = new Set(snapshot.edges.filter(edge => edge.tier === 'extracted').flatMap(edge => [edge.subject, edge.object]));
    const nonExtractedIds = new Set(snapshot.edges.filter(edge => edge.tier !== 'extracted').flatMap(edge => [edge.subject, edge.object]));
    const filtered = snapshot.nodes.filter(node => (kind==='food_record'?node.kind==='food':kind==='exposure'?['diet','exposure'].includes(node.kind):normalizeKind(node.kind) === kind) &&
      (!query || [node.label, node.label_en || '', node.id, ...(node.aliases || [])].some(value => value.toLocaleLowerCase().includes(query))) &&
      (filters.domain === 'all' || node.domains?.includes(filters.domain) || allowedIds.has(node.id)) &&
      (filters.tier === 'all' || allowedIds.has(node.id)) &&
      (filters.include_extracted || filters.tier === 'extracted' || !extractedIds.has(node.id) || nonExtractedIds.has(node.id)));
    filtered.sort((a, b) => (sort === 'degree' ? (a.degree || 0) - (b.degree || 0) : a.label.localeCompare(b.label, 'ko')) * (order === 'desc' ? -1 : 1));
    return { items: filtered.slice((page - 1) * pageSize, page * pageSize), total: filtered.length, page, page_size: pageSize, release_id: snapshot.release_id };
  }
  const params = new URLSearchParams({ kind, q, domain: filters.domain, tier: filters.tier, include_extracted: String(filters.include_extracted), page: String(page), page_size: String(pageSize), sort, order });
  return request<{ items: Entity[]; total: number; page: number; page_size: number; release_id: string }>(`/entities?${params}`, signal);
}
export async function getInterventions(q: string, page=1, pageSize=25, signal?: AbortSignal) {
  if(snapshot){
    const nodes=snapshot.nodes.filter(node=>['diet','exposure'].includes(node.kind));
    const items=nodes.filter(node=>{const record=exposureRecord(snapshot.node_details?.[node.id]||node);return record?interventionMatches(record,q):[node.label,node.id,...(node.aliases||[])].join(' ').toLocaleLowerCase().includes(q.trim().toLocaleLowerCase())});
    return {items:items.slice((page-1)*pageSize,page*pageSize),total:items.length,page,page_size:pageSize,release_id:snapshot.release_id};
  }
  return fetchWithTimeout('/api/v1/interventions?limit=1000&offset=0',{signal},async response=>{
    if(!response.ok)throw await serverError(response);
    const result=await response.json() as {items:InterventionRecord[];release_id?:string;total?:number};
    if(typeof result.total==='number'&&result.total>result.items.length)throw new Error('연구 노출 목록이 일부만 반환됐습니다. 전체 검색을 확인할 수 없습니다.');
    const items=result.items.filter(row=>interventionMatches(row,q)).map(interventionEntity).sort((a,b)=>a.label.localeCompare(b.label,'ko'));
    return {items:items.slice((page-1)*pageSize,page*pageSize),total:items.length,page,page_size:pageSize,release_id:result.release_id};
  });
}
export interface CatalogPage {items:Entity[];total:number;page:number;page_size:number;release_id?:string;preview?:boolean;groups?:{kind:'food'|'component'|'exposure';total:number|null;shown:number}[];warnings?:string[]}
export async function getCatalogSearch(kind:SearchKind,q:string,filters:Filters,page:number,signal?:AbortSignal):Promise<CatalogPage>{
  if(kind==='exposure')return getInterventions(q,page,25,signal);
  if(kind!=='inputs')return getEntities(kind==='food'?'food_record':kind,q,filters,page,25,'label','asc',signal);
  const calls=await Promise.allSettled([getEntities('food_record',q,filters,1,8,'label','asc',signal),getEntities('component',q,filters,1,8,'label','asc',signal),getInterventions(q,1,8,signal)]);
  if(signal?.aborted)throw new DOMException('검색이 취소되었습니다.','AbortError');
  if(calls.every(result=>result.status==='rejected'))throw new Error('음식·성분·연구 노출을 조회하지 못했습니다. 검색 결과 0건을 뜻하지 않습니다.');
  const kinds=['food','component','exposure'] as const;
  const groups=calls.map((result,index)=>({kind:kinds[index],total:result.status==='fulfilled'?result.value.total:null,shown:result.status==='fulfilled'?result.value.items.length:0}));
  const values=calls.map(result=>result.status==='fulfilled'?result.value.items:[]);
  const releases=calls.flatMap(result=>result.status==='fulfilled'&&result.value.release_id?[result.value.release_id]:[]);
  if(new Set(releases).size>1)throw new Error('검색 중 자료 release가 바뀌었습니다. 같은 버전으로 다시 조회해주세요.');
  return {items:mergeCatalogPreview(values[0],values[1],values[2],q),total:groups.reduce((sum,group)=>sum+(group.total||0),0),page:1,page_size:24,release_id:releases[0],preview:true,groups,
    warnings:calls.flatMap((result,index)=>result.status==='rejected'?[`${{food:'음식',component:'성분',exposure:'연구 노출'}[kinds[index]]} 조회 실패 · 결과 수 미확인`]:[])};
}
export async function getNode(id: string): Promise<NodeDetail> {
  if (snapshot) return snapshot.node_details?.[id] || { ...snapshot.nodes.find(node => node.id === id)!, gaps: ['독립 HTML에 포함된 자료만 표시합니다.'] };
  return request<NodeDetail>(`/nodes/${encodeURIComponent(id)}`);
}
export async function getEdge(id: string): Promise<EvidenceEdge> {
  if (snapshot) return snapshot.edge_details?.[id] || snapshot.edges.find(edge => edge.id === id)!;
  return request<EvidenceEdge>(`/edges/${encodeURIComponent(id)}`);
}
export async function getScientificMatches(payload: ScientificContext, signal?: AbortSignal): Promise<ScientificMatchResult> {
  if (snapshot) {
    const frozen = snapshot.node_details?.[payload.seed_id]?.scientific_matches;
    return frozen || { release_id: snapshot.release_id, rule_version: 'not_embedded', seed_id: payload.seed_id,
      reference_context: {}, observed_evidence: [], evidence_gaps: ['이 snapshot에 해당 항목의 과학적 대응 결과가 포함되지 않았습니다. 원본 서비스에서 확인해주세요.'], note: '독립 HTML은 저장 시점의 결과만 표시합니다.' };
  }
  return fetchWithTimeout(`${ROOT}/match`, { method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }, async response => {
    if (!response.ok) {
      if (response.status === 404) throw new Error('이 release에서는 과학적 조건 비교가 아직 준비되지 않았습니다. 연결 가설과 원본 출처는 탐색할 수 있습니다.');
      if (response.status === 503) throw await serverError(response);
      throw new Error(`연구 조건 비교를 완료하지 못했습니다 (${response.status}). 입력 단위와 연결을 확인해주세요.`);
    }
    return response.json() as Promise<ScientificMatchResult>;
  });
}
export async function getGraph(seedId: string, filters: Filters, maxNodes: number, maxEdges: number, signal?: AbortSignal): Promise<GraphData> {
  if (snapshot) {
    const edges = snapshot.edges.filter(edge => eligibleEdge(edge, filters));
    const selected = new Set([seedId]); let frontier = [seedId];
    for (let level = 0; level < 4 && selected.size < maxNodes; level++) {
      const next: string[] = [];
      for (const edge of edges) {
        if (!frontier.includes(edge.subject) && !frontier.includes(edge.object)) continue;
        for (const id of [edge.subject, edge.object]) if (!selected.has(id) && selected.size < maxNodes) { selected.add(id); next.push(id); }
      }
      frontier = next;
    }
    const allEdges = edges.filter(edge => selected.has(edge.subject) && selected.has(edge.object));
    const shownEdges = allEdges.slice(0, maxEdges);
    const edgeIds = new Set(shownEdges.map(edge => edge.id));
    return recordGraphReceipt({ nodes: snapshot.nodes.filter(node => selected.has(node.id)), edges: shownEdges,
      paths: snapshot.paths.filter(path => path.node_ids.every(id => selected.has(id)) && path.edge_ids.every(id => edgeIds.has(id))),
      gaps: snapshot.gaps, release_id: snapshot.release_id,
      truncated: allEdges.length > maxEdges || selected.size < snapshot.nodes.length,
      omitted: { nodes: Math.max(0, snapshot.nodes.length - selected.size), edges: Math.max(0, edges.length - shownEdges.length) } });
  }
  return request<GraphData>(`/graph?${new URLSearchParams({ seed_id: seedId, ...Object.fromEntries(Object.entries(filters).map(([key, value]) => [key, String(value)])), depth: '4', max_nodes: String(maxNodes), max_edges: String(maxEdges) })}`, signal).then(recordGraphReceipt);
}
function csvCell(value: unknown) { return `"${String(value ?? '').replaceAll('"', '""')}"`; }
export async function exportGraph(payload: ExportRequest): Promise<{ blob: Blob; filename: string }> {
  if (snapshot) {
    const selection: Snapshot = { ...snapshot, nodes: snapshot.nodes.filter(node => payload.node_ids.includes(node.id)),
      edges: snapshot.edges.filter(edge => payload.edge_ids.includes(edge.id)), positions: payload.positions, filters: payload.filters };
    if (payload.format === 'json') return { blob: new Blob([JSON.stringify(selection, null, 2)], { type: 'application/json' }), filename: 'nutriomics-snapshot.json' };
    if (payload.format === 'csv') return { blob: new Blob(['\uFEFF' + ['id,subject,object,relation,tier,review_status,source_ids,pmids', ...selection.edges.map(edge => [edge.id, edge.subject, edge.object, edge.relation, edge.tier, edge.review_status, edge.source_ids?.join(';'), edge.pmids?.join(';')].map(csvCell).join(','))].join('\r\n')], { type: 'text/csv;charset=utf-8' }), filename: 'nutriomics-relations.csv' };
    const clone = document.documentElement.cloneNode(true) as HTMLElement;
    const data = JSON.stringify(selection).replaceAll('<', '\\u003c').replaceAll('\u2028', '\\u2028').replaceAll('\u2029', '\\u2029');
    const existing = [...clone.querySelectorAll('script')].find(script => script.textContent?.includes('window.__NUTRIOMICS_SNAPSHOT__'));
    if (existing) existing.textContent = `window.__NUTRIOMICS_SNAPSHOT__=${data};`;
    else { const script = document.createElement('script'); script.textContent = `window.__NUTRIOMICS_SNAPSHOT__=${data};`; clone.querySelector('head')!.append(script); }
    clone.querySelector('#root')!.innerHTML = '';
    return { blob: new Blob(['<!doctype html>\n' + clone.outerHTML], { type: 'text/html;charset=utf-8' }), filename: 'nutriomics-explorer.html' };
  }
  return fetchWithTimeout(`${ROOT}/export`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }, async response => {
    if (!response.ok) throw await serverError(response);
    const filename = response.headers.get('Content-Disposition')?.match(/filename\*?=(?:UTF-8'')?"?([^";]+)/)?.[1];
    return { blob: await response.blob(), filename: filename ? decodeURIComponent(filename) : `nutriomics-explorer.${payload.format}` };
  }, EXPORT_TIMEOUT_MS);
}
export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = filename; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export async function analyzeFood(foodId: string, amount: number) {
  if (offline) throw new Error('독립 HTML에는 식사 계산 서버가 연결되지 않습니다. 저장된 함량 기준을 확인해주세요.');
  return fetchWithTimeout('/api/v1/meals/analyze', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ items: [{ food_id: foodId, amount_g: amount }] }) }, async response => {
    if (!response.ok) throw new Error('이 식품의 섭취량 계산에 필요한 기준을 확인할 수 없습니다.');
    return response.json() as Promise<Record<string, unknown>>;
  });
}
