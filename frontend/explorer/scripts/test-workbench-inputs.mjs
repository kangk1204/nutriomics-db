import assert from 'node:assert/strict';
import Module from 'node:module';
import path from 'node:path';
import { build } from 'esbuild';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { readFile } from 'node:fs/promises';

const compiled=await build({entryPoints:['src/Workbench.tsx'],bundle:true,write:false,platform:'node',format:'cjs',loader:{'.css':'empty'},external:['react','react-dom']});
const originalWindow=globalThis.window;
let componentExports;
function render(seed){
  globalThis.window={__NUTRIOMICS_SNAPSHOT__:seed?{release_id:'test-frozen',nodes:[seed],edges:[],paths:[],gaps:[],filters:{scientific_context:{seed_id:seed.id,reference_context:{}}},node_details:{},offline:true}:undefined};
  const filename=path.resolve('src/__workbench_render.cjs'),instance=new Module(filename);
  instance.filename=filename;instance.paths=Module._nodeModulePaths(path.resolve('.'));instance._compile(compiled.outputFiles[0].text,filename);
  componentExports=instance.exports;
  return renderToStaticMarkup(React.createElement(instance.exports.default,{onLegacy:()=>{}}));
}
try{
  const record={id:'S03:original',study_id:'S03',label:'Source extract',amount:200,unit:'mg',duration_days:56,dose_basis:'assigned per day',metadata_json:'{"expert_review_status":"pending"}'};
  const exposure=render({id:'Exposure:S03:original',kind:'diet',label:'포도씨 추출물',metadata:{original_exposure:record}});
  assert.ok(exposure.includes('원 연구의 중재 노출'));
  assert.ok(exposure.includes('200')&&exposure.includes('mg')&&exposure.includes('56일'));
  assert.ok(exposure.includes('전문가 검수 대기'));
  assert.equal(exposure.includes('aria-label="섭취량 g"'),false);
  assert.equal(exposure.includes('성분 섭취량 계산'),false);
  assert.ok(exposure.includes('섭취 기간 일')&&exposure.includes('섭취 빈도'));
  const food=render({id:'USDA:source',kind:'food',label:'Yogurt'});
  assert.ok(food.includes('aria-label="섭취량 g"'));
  assert.ok(food.includes('성분 섭취량 계산'));
  const start=render();
  assert.ok(start.includes('연구 노출')&&start.includes('포도씨 추출물'));
  assert.ok(start.includes('예측·자동 후보 포함'));
  assert.equal(/type="checkbox"[^>]*checked/.test(start),false,'hypotheses must be opt-in');
  assert.equal(start.includes('<option value="regulatory_predicted"'),false);
  assert.equal(start.includes('<option value="literature_candidate"'),false);
  assert.equal(start.includes('<option value="all">전체 수록 근거'),false);
  const withheld=render({id:'Exposure:restricted',kind:'diet',label:'Restricted exposure',metadata:{exposure_export_status:'withheld',exposure_withheld_reason:'exposure_source_redistribution_not_permitted'}});
  assert.ok(withheld.includes('노출 상세 제한')&&withheld.includes('이용조건'));
  assert.equal(withheld.includes('원본 노출 상세 조회 중'),false);
  assert.equal(withheld.includes('성분 섭취량 계산'),false);
  const missing=render({id:'Exposure:not-embedded',kind:'diet',label:'Snapshot without record',metadata:{}});
  assert.ok(missing.includes('노출 상세가 내장되지 않았습니다'));
  assert.equal(missing.includes('원본 노출 상세 조회 중'),false);
  const shapes=JSON.parse(await readFile('scripts/fixtures/research-classification-shapes.json','utf8'));
  const s05=shapes.items.find(item=>item.study_id==='S05'),s01=shapes.items.find(item=>item.study_id==='S01');
  const compact=renderToStaticMarkup(React.createElement(componentExports.SourceClassificationNote,{exposure:s05,compact:true}));
  assert.ok(compact.includes('탐색 관찰')&&compact.includes('식이 주근거 제외'));
  const full=renderToStaticMarkup(React.createElement(componentExports.SourceClassificationNote,{exposure:s01}));
  assert.ok(full.includes('보충제별')&&full.includes('귀속 미확인'));
  assert.ok(full.includes(s01.source_classification.reason));
  assert.ok(full.includes('전문가 승인 상태와 별개'));
  assert.equal(full.includes('전문가 검수 승인'),false);
}finally{globalThis.window=originalWindow}
console.log('Workbench server-render checks passed: permitted/withheld/missing exposure states, no invented grams, explicit hypotheses, source-classification badges/details and pending review. This is not browser visual or offline execution QA.');
