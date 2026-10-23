import assert from 'node:assert/strict';
import path from 'node:path';
import Module from 'node:module';
import {build} from 'esbuild';

const compiled=await build({entryPoints:['src/api.ts'],bundle:true,write:false,platform:'node',format:'cjs'});
const originalWindow=globalThis.window,originalFetch=globalThis.fetch;
const gene={id:'NCBIGene:348',kind:'gene',label:'APOE',gene_names:{approved_symbol:'APOE',hgnc_id:'HGNC:613',terms:[{kind:'approved_symbol',name:'APOE'},{kind:'approved_name',name:'apolipoprotein E'},{kind:'authored_korean_name',name:'아포지단백 E'},{kind:'alias_symbol',name:'shared alias'}]}};
const other={id:'NCBIGene:1',kind:'gene',label:'Other',gene_names:{terms:[{name:'shared alias',kind:'alias_symbol'}]}};
function load(snapshot){
 globalThis.window=snapshot?{__NUTRIOMICS_SNAPSHOT__:snapshot}:{};
 const filename=path.resolve('src/__gene_test.cjs'),m=new Module(filename);m.filename=filename;m._compile(compiled.outputFiles[0].text,filename);return m.exports;
}
const filters={domain:'all',tier:'all',include_extracted:false};
try{
 const local=load({release_id:'fixture',nodes:[gene,other,{id:'food:1',kind:'food',label:'apolipoprotein E'}],edges:[],node_details:{},paths:[],gaps:[]});
 globalThis.fetch=()=>{throw new Error('Standalone snapshot attempted a server request')};
 for(const q of ['APOE','apoe','apolipoprotein E','아포지단백E']){
  const r=await local.getEntities('gene',q,filters,1,25,'label','asc');assert.deepEqual(r.items.map(n=>n.id),[gene.id]);
 }
 const ambiguous=await local.getEntities('gene','shared alias',filters,1,25,'label','asc');assert.equal(ambiguous.items.length,2);
 const live=load();let seen;
 globalThis.fetch=async url=>{seen=url;return new Response(JSON.stringify({items:[],total:0,status:'available'}))};
 const c={series_accession:'GSE1',contrast:'A-B',analysis_version:'v1',feature_id:'348'};
 await live.getOmicsResults(c,'gene_expression',1);assert.equal(new URLSearchParams(seen.split('?')[1]).get('feature_id'),'348');
 await live.getOmicsResults({...c,feature_id:undefined},'gene_expression',1);assert.equal(new URLSearchParams(seen.split('?')[1]).has('feature_id'),false);
}finally{globalThis.window=originalWindow;globalThis.fetch=originalFetch}
console.log('Gene search checks passed: symbol, full name, Korean spacing, ambiguous aliases, strict entity type, offline scope, and exact native feature filter.');
