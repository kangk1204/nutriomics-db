import assert from 'node:assert/strict';
import Module from 'node:module';
import path from 'node:path';
import {build} from 'esbuild';
import {JSDOM} from 'jsdom';

// Real React state/effects in a DOM fixture, with deliberately out-of-order HTTP.
const dom=new JSDOM('<!doctype html><div id="root"></div>',{url:'http://localhost/'});
dom.window.HTMLElement.prototype.scrollTo=function(){}; // Layout/scrolling is verified separately in a real browser.
const saved=new Map();
for(const [k,v] of Object.entries({window:dom.window,document:dom.window.document,navigator:dom.window.navigator,HTMLElement:dom.window.HTMLElement,IS_REACT_ACT_ENVIRONMENT:true})){
 saved.set(k,Object.getOwnPropertyDescriptor(globalThis,k));Object.defineProperty(globalThis,k,{value:v,writable:true,configurable:true});
}
const originalFetch=globalThis.fetch;
const React=await import('react'),{createRoot}=await import('react-dom/client');
const compiled=await build({entryPoints:['src/Workbench.tsx'],bundle:true,write:false,platform:'node',format:'cjs',loader:{'.css':'empty'},external:['react','react-dom','react-dom/client']});
const filename=path.resolve('src/__search_lifecycle.cjs'),m=new Module(filename);m.filename=filename;m.paths=Module._nodeModulePaths(path.resolve('.'));m._compile(compiled.outputFiles[0].text,filename);
const genes=['APOE','NFE2L2','GABPA'].map((label,i)=>({id:'NCBIGene:'+i,kind:'gene',label,label_en:label,aliases:[],metadata:{},domains:[],degree:0,gene_names:{hgnc_id:'HGNC:'+i,approved_symbol:label,approved_name:'Full '+label,terms:[{name:label,kind:'approved_symbol'}],expert_review_status:'pending'}}));
const pending=new Map(),calls=[];
const response=(v,status=200)=>new Response(JSON.stringify(v),{status,headers:{'content-type':'application/json'}});
globalThis.fetch=async(url,options={})=>{
 const u=new URL(url,'http://localhost');calls.push(u.pathname+u.search);
 if(u.pathname.endsWith('/meta'))return response({release_id:'lifecycle-fixture',available_filters:{},counts:{},domains:[]});
 if(u.pathname==='/api/v1/interventions')return response({release_id:'lifecycle-fixture',items:[]});
 if(u.pathname.endsWith('/entities')){
  const q=u.searchParams.get('q');if(q==='network-failure')return response({detail:{code:'source_unavailable'}},503);
  const items=u.searchParams.get('kind')==='gene'?(q==='NRF2'?genes.slice(1):genes.filter(g=>q===g.label)):[];
  return response({items,total:items.length,page:1,page_size:8,release_id:'lifecycle-fixture',...(q==='NRF2'?{gene_search_context:{ambiguous:true,exact_hgnc_candidates:2}}:{})});
 }
 if(u.pathname.includes('/nodes/')){const id=decodeURIComponent(u.pathname.split('/nodes/')[1]);return response({...genes.find(g=>g.id===id),effects:[],measurements:[],gaps:[],studies:[],regulatory_evidence:[],total_regulatory_evidence:0});}
 if(u.pathname.endsWith('/graph'))return response({release_id:'lifecycle-fixture',nodes:[],edges:[],paths:[],gaps:[]});
 if(u.pathname.endsWith('/evidence'))return response({items:[],total:0,page:1,page_size:25,status:'available'});
 if(u.pathname.endsWith('/match')){
  const id=JSON.parse(options.body).seed_id;
  return new Promise(resolve=>pending.set(id,resolve)); // Ignores abort on purpose.
 }
 throw new Error('Unexpected fixture route '+u.pathname);
};
const root=createRoot(document.getElementById('root'));
const tick=async(ms=300)=>React.act(async()=>{await new Promise(r=>setTimeout(r,ms));});
const text=()=>document.body.textContent;
async function query(value){
 const field=document.querySelector('[aria-label="항목 검색"]');
 await React.act(async()=>{Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype,'value').set.call(field,value);field.dispatchEvent(new dom.window.Event('input',{bubbles:true}));await Promise.resolve();});
 await tick();assert.equal(field.value,value);
}
async function clickGene(symbol){
 const button=[...document.querySelectorAll('.wb-entity-list button')].find(b=>b.textContent.includes('Full '+symbol));assert.ok(button,'Gene candidate '+symbol);
 await React.act(async()=>{button.click();await Promise.resolve();});await tick();
}
async function release(symbol,marker,status=200){
 const gene=genes.find(g=>g.label===symbol),resolve=pending.get(gene.id);assert.ok(resolve,'Pending '+symbol);
 await React.act(async()=>{resolve(status===200?response({seed_id:gene.id,rule_version:'fixture',reference_context:{},observed_evidence:[],evidence_gaps:[marker],gene_query:{hgnc_id:gene.gene_names.hgnc_id,status:'available_no_results'}}):response({detail:{code:'source_unavailable'}},status));await new Promise(r=>setTimeout(r,0));});pending.delete(gene.id);
}
try{
 await React.act(async()=>{root.render(React.createElement(m.exports.default,{onLegacy:()=>{}}));await Promise.resolve();});await tick();
 await query('APOE');await clickGene('APOE');assert.equal(document.querySelector('h1').textContent,'APOE');
 await query('자동차 보험 견적');assert.notEqual(document.querySelector('h1').textContent,'APOE');
 assert.ok(text().includes('이전 선택을 해제했습니다: APOE.'));await release('APOE','STALE_APOE');await tick();
 assert.ok(!text().includes('STALE_APOE'));assert.ok(text().includes('일치하는 항목이 없습니다'));
 assert.equal(document.querySelector('[aria-label="내보내기 형식"]').disabled,true);assert.equal(document.querySelector('.wb-omics'),null);
 await query('AP0EXXZ');assert.equal(document.querySelectorAll('.wb-entity-list button').length,1,'Only clear-query action remains');assert.ok(!document.querySelector('h1').textContent.includes('APOE'));
 await query('network-failure');assert.ok(text().includes('검색 결과를 확인할 수 없습니다'));assert.ok(!text().includes('일치하는 항목이 없습니다'));
 await query('NRF2');assert.ok(text().includes('공식 유전자 후보 2개'));await clickGene('NFE2L2');
 await query('GABPA');await clickGene('GABPA');await release('NFE2L2','STALE_NFE_AFTER_GABPA');await release('GABPA','CURRENT_GABPA');await tick();
 assert.equal(document.querySelector('h1').textContent,'GABPA');assert.ok(!text().includes('STALE_NFE_AFTER_GABPA'));assert.ok(text().includes('CURRENT_GABPA'));
 await query('NFE2L2');await clickGene('NFE2L2');await query('전혀 무관한 검색어');await release('NFE2L2','',503);await tick();assert.equal(document.querySelector('.wb-error'),null,'Late errors from old selection must not replace the new search state');
 console.log('Search lifecycle passed: clears old selection/results on query changes; ignores late responses; typos do not select entities; shared aliases require selection; failures are not empty results; later gene retains its own context.');
}finally{
 await React.act(()=>root.unmount());globalThis.fetch=originalFetch;dom.window.close();
 for(const [k,d] of saved){if(d)Object.defineProperty(globalThis,k,d);else delete globalThis[k];}
}
