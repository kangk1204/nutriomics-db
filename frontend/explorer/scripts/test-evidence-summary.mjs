import assert from 'node:assert/strict';
import { build } from 'esbuild';
import Module from 'node:module';

const compiled=await build({entryPoints:['src/evidenceSummary.ts'],bundle:true,write:false,platform:'node',format:'cjs'});
const instance=new Module('evidence-summary-test.cjs');instance._compile(compiled.outputFiles[0].text,'evidence-summary-test.cjs');
const {summarizeScientificEvidence:summary}=instance.exports;
assert.deepEqual(summary(null),{studies:null,contrasts:null});
assert.deepEqual(summary({observed_evidence:[]}),{studies:0,contrasts:0});
assert.deepEqual(summary({rule_version:'not_embedded',observed_evidence:[]}),{studies:null,contrasts:null});
const contrast={id:'paired_change',series_accession:'GSE_A',analysis_version:'v1',n_people:7,n_samples:56};
// Active and control exposures in one crossover study describe one contrast.
const exposed=[{study_id:'study-A',contrasts:[contrast]},{study_id:'study-A',contrasts:[contrast]}];
assert.deepEqual(summary({observed_evidence:exposed}),{studies:1,contrasts:1});
// A distinct study or analysis version must not be merged under the same label.
assert.deepEqual(summary({observed_evidence:[...exposed,{study_id:'study-B',contrasts:[contrast]},{study_id:'study-A',contrasts:[{...contrast,analysis_version:'v2'}]}]}),{studies:2,contrasts:3});
console.log('5 evidence-grain checks passed; repeated exposure records are not independent contrasts or subjects');
