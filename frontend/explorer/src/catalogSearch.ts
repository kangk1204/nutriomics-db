import type { Entity, InterventionRecord, Kind, ScientificContext } from './types';

export type SearchKind = Kind | 'inputs' | 'exposure';
export const searchKinds: Record<SearchKind, string> = {inputs:'통합',food:'음식',component:'성분',exposure:'연구 노출',gene:'유전자',health:'건강',pathway:'경로',cpg:'CpG·프로브',regulatory_region:'조절 영역'};
export const HYPOTHESIS_LAYERS = new Set(['all','regulatory_predicted','literature_candidate']);
const normalize = (value: unknown) => String(value ?? '').normalize('NFKC').toLocaleLowerCase().replace(/\s+/g,' ').trim();
export function stringList(value: unknown): string[] {
  if(typeof value==='string'){try{return stringList(JSON.parse(value))}catch{return [value]}}
  return Array.isArray(value)?value.filter((item):item is string=>typeof item==='string'):[];
}
export function interventionMatches(record: InterventionRecord, query: string): boolean {
  const term=normalize(query);if(!term)return true;
  const values=[record.id,record.study_id,record.label,record.formulation,...stringList(record.food_terms),...stringList(record.component_keys)].map(normalize);
  return values.some(value=>value.includes(term)||value.replace(/\s/g,'').includes(term.replace(/\s/g,'')));
}
export function interventionEntity(record: InterventionRecord): Entity {
  if(!record.id||!record.study_id)throw new Error('연구 노출의 원본 ID가 없습니다.');
  return {id:`Exposure:${record.id}`,kind:'diet',label:record.label||record.id,aliases:stringList(record.food_terms),
    metadata:{entity_type:'exposure',study_id:record.study_id,intervention:record,composition_available:false}};
}
export function exposureRecord(entity: Entity | null | undefined): InterventionRecord | undefined {
  if(!entity||!['diet','exposure'].includes(entity.kind))return undefined;
  const value=entity.metadata?.intervention||entity.metadata?.original_exposure;
  if(!value||typeof value!=='object')return undefined;
  const row=value as Record<string,unknown>;
  let metadata=row.metadata;
  if(metadata==null&&typeof row.metadata_json==='string'){try{metadata=JSON.parse(row.metadata_json)}catch{/* Missing metadata remains unknown. */}}
  return {...row,metadata:metadata&&typeof metadata==='object'?metadata:undefined} as unknown as InterventionRecord;
}
export function searchKindOf(entity: Entity): SearchKind {
  if(['diet','exposure'].includes(entity.kind))return 'exposure';
  if(['nutrient','chemical','compound','component'].includes(entity.kind))return 'component';
  if(['disease','outcome','health'].includes(entity.kind))return 'health';
  if(['cpg','methylation_probe'].includes(entity.kind))return 'cpg';
  return ['food','gene','pathway','regulatory_region'].includes(entity.kind)?entity.kind as Kind:'gene';
}
export function mergeCatalogPreview(food: Entity[], components: Entity[], exposures: Entity[], query: string): Entity[] {
  const result=new Map<string,Entity>();
  const groups=query.trim()?[exposures,food,components]:[food,components,exposures];
  for(const group of groups)for(const entity of group)if(!result.has(entity.id))result.set(entity.id,entity);
  return [...result.values()];
}
export function scientificInput(seed: Entity, intake: number, tissue: string, hours: string, days: string, frequency: string): ScientificContext {
  return {seed_id:seed.id,...(seed.kind==='food'?{intake_g:intake}:{}),reference_context:{
    tissue:tissue||null,response_time_h:hours===''?null:Number(hours),regimen_duration_days:days===''?null:Number(days),
    frequency:frequency==='single'||frequency==='daily'?frequency:null,
  }};
}
export function reviewLabel(status: unknown): string {
  if(status==='approved')return '전문가 검수 승인';
  if(status==='pending'||status==='pending_expert_review')return '전문가 검수 대기';
  if(status==='source_checked'||status==='curator_checked')return '출처 대조 · 전문가 승인 미확인';
  return '전문가 검수 미확인';
}
export function safeLayer(layer: string, hypotheses: boolean): string {
  return !hypotheses&&HYPOTHESIS_LAYERS.has(layer)?'dietary':layer;
}
