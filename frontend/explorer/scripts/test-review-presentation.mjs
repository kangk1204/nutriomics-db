// Synthetic declaration transitions test the UI contract; never human gold or actual approvals.
import assert from 'node:assert/strict';
import Module from 'node:module';
import path from 'node:path';
import { build } from 'esbuild';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const compiled=await build({entryPoints:['src/reviewPresentation.ts'],bundle:true,write:false,platform:'node',format:'cjs'});
const filename=path.resolve('src/__review_presentation.cjs'),instance=new Module(filename);
instance.filename=filename;instance._compile(compiled.outputFiles[0].text,filename);
const {evidenceReviewPresentation,exposureReviewPresentation}=instance.exports;
function context(kind,id,decision='pending'){
  return {canonical_record_sha256:'a'.repeat(64),scientific_approval:decision==='accept',
    human_review:{record_kind:kind,record_id:id,record_sha256:'a'.repeat(64),decision,
      reviewer_type:decision==='pending'?undefined:'human',reviewer_attestation:decision!=='pending'}};
}
const exposure={study_id:'S05',study_evidence:{expert_review_status:'pending'}};
const contrast={id:'contrast:1'};
const decisions=['accept','correct','exclude','unconfirmed','pending'];
for(const decision of decisions){
  const result=evidenceReviewPresentation({...exposure,science_review_context:context('study','S05',decision)},
    {...contrast,science_review_context:context('contrast','contrast:1',decision)});
  assert.equal(result.items.length,2);
  assert.ok(result.items.every(item=>item.state===decision));
  assert.ok(result.items.some(item=>item.label.startsWith('연구:')));
  assert.ok(result.items.some(item=>item.label.startsWith('대비:')));
  assert.equal(result.summary.includes('검수 완료'),false);
  assert.ok(result.boundary.includes('연구 승인만으로'));
}
const accepted={...exposure,science_review_context:context('study','S05','accept')};
const pending={...contrast,science_review_context:context('contrast','contrast:1','pending')};
const partial=evidenceReviewPresentation(accepted,pending);
assert.equal(partial.items[0].state,'pending');assert.equal(partial.items[1].state,'accept');
assert.ok(partial.summary.includes('대기'));
const missing=evidenceReviewPresentation(accepted,contrast);
assert.equal(missing.items[0].state,'missing');assert.ok(missing.summary.includes('미제공'));
const restricted=evidenceReviewPresentation(accepted,{...contrast,science_review_context:context('contrast','contrast:1','exclude')});
assert.equal(restricted.items[0].state,'exclude');assert.equal(restricted.items[0].scope,'대비');
assert.equal(restricted.restricted,true);
const sourceRestricted=evidenceReviewPresentation({...accepted,primary_dietary_evidence_eligible:false},
  {...contrast,science_review_context:context('contrast','contrast:1','accept')});
assert.equal(sourceRestricted.restricted,true,'accepted record cannot erase a source classification restriction');
const parentOnly=evidenceReviewPresentation(accepted);
assert.equal(parentOnly.items.length,1);assert.equal(parentOnly.items[0].scope,'연구');
for(const change of [
  {canonical_record_sha256:'b'.repeat(64)},
  {canonical_record_sha256:'not-a-hash'},
  {human_review:{...context('study','S05','accept').human_review,record_id:'S01'}},
  {human_review:{...context('study','S05','accept').human_review,record_kind:'contrast'}},
  {human_review:{...context('study','S05','accept').human_review,reviewer_type:'AI'}},
  {human_review:{...context('study','S05','accept').human_review,reviewer_attestation:false}},
  {human_review:{...context('study','S05','accept').human_review,decision:'approved'}},
  {scientific_approval:false},
]){
  const ctx={...context('study','S05','accept'),...change};
  const value=evidenceReviewPresentation({...exposure,science_review_context:ctx});
  assert.equal(value.items[0].state,'invalid');assert.equal(value.restricted,true);
}
const legacy=evidenceReviewPresentation(exposure,contrast);
assert.equal(legacy.items.length,1);assert.equal(legacy.items[0].state,'legacy');
assert.ok(legacy.items[0].label.includes('전문가 검수 대기'));
const sourceChecked=evidenceReviewPresentation({...exposure,science_review_context:{scientific_approval:true,
  source_classification:{source_checked:true,scientific_approval:true}}},contrast,'source_checked');
assert.equal(sourceChecked.items[0].state,'legacy');
assert.ok(sourceChecked.items[0].label.includes('승인 미확인'));

const record={id:'S03:source',study_id:'S03',original_record_sha256:'b'.repeat(64),metadata:{expert_review_status:'pending'}};
for(const [status,state] of Object.entries({accepted:'accept',correction_required:'correct',excluded:'exclude',unconfirmed:'unconfirmed',pending:'pending'})){
  const result=exposureReviewPresentation({...record,publication_review_status:status});
  assert.equal(result.items[0].state,state);assert.equal(result.items[0].scope,'노출 공개판정');
  assert.ok(result.boundary.includes('연구·대비의 사람 승인'));
}
assert.equal(exposureReviewPresentation({...record,original_record_sha256:undefined,publication_review_status:'accepted'}).items[0].state,'invalid');
assert.equal(exposureReviewPresentation({...record,publication_review_status:'source_checked'}).items[0].state,'legacy');
assert.equal(exposureReviewPresentation(record).items[0].state,'legacy');
const liveRecord={...record,science_review_context:context('exposure','S03:source','accept')};
assert.equal(exposureReviewPresentation(record,liveRecord).items[0].state,'accept');
assert.equal(exposureReviewPresentation(record,{...liveRecord,id:'other'}).items[0].state,'legacy','never transfer a decision from a different exposure');
assert.equal(exposureReviewPresentation({...record,science_review_context:context('study','S03','accept')}).items[0].state,'invalid');
const before=JSON.stringify([accepted,pending,record,liveRecord]);
evidenceReviewPresentation(accepted,pending);exposureReviewPresentation(record,liveRecord);
assert.equal(JSON.stringify([accepted,pending,record,liveRecord]),before,'presentation must not mutate decisions or canonical records');

const originalWindow=globalThis.window;
try{
  globalThis.window={};
  const ui=await build({entryPoints:['src/Workbench.tsx'],bundle:true,write:false,platform:'node',format:'cjs',loader:{'.css':'empty'},external:['react','react-dom']});
  const uiName=path.resolve('src/__review_scope_render.cjs'),view=new Module(uiName);
  view.filename=uiName;view.paths=Module._nodeModulePaths(process.cwd());view._compile(ui.outputFiles[0].text,uiName);
  const render=compact=>renderToStaticMarkup(React.createElement(view.exports.ReviewDecisionBadges,{presentation:restricted,compact}));
  const row=render(true),detail=render(false);
  for(const label of restricted.items.map(item=>item.label)){assert.ok(row.includes(label));assert.ok(detail.includes(label))}
  assert.ok(row.indexOf('대비: 제외')<row.indexOf('연구: 승인'));
  assert.ok(detail.includes(restricted.boundary));
}finally{globalThis.window=originalWindow}
console.log('Scoped review presentation/SSR checks passed: all declaration transitions, exact record hash/type/ID, human attestation, restrictive precedence, partial pending, legacy/source-check fallback, exposure publication status, and unchanged source values. Synthetic contract cases are not human approvals or scientific gold.');
