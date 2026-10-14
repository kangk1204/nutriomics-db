import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { createRequire } from 'node:module';
import { rm } from 'node:fs/promises';

const output=new URL('../src/__render_metrics_test.cjs',import.meta.url);
try {
  await build({entryPoints:['src/renderMetrics.ts'],bundle:true,platform:'node',format:'cjs',outfile:output.pathname.replace(/^\/(\w:)/,'$1'),logLevel:'silent'});
  const {recordGraphReceipt,graphTiming}=createRequire(import.meta.url)(output.pathname.replace(/^\/(\w:)/,'$1'));
  const graph={nodes:Array(80).fill({id:'fixture'})};
  const before=JSON.stringify(graph);
  recordGraphReceipt(graph,1000);
  assert.equal(graphTiming(graph,1005,1025,8,80).data_to_frame_ms,25);
  recordGraphReceipt({nodes:[]},9000);
  assert.equal(graphTiming(graph,1005,1030,8,80).data_to_frame_ms,30);
  const preloaded=graphTiming(graph,5000,5020,8,80);
  assert.equal(preloaded.preloaded_before_render_ms,4000);
  assert.equal(preloaded.data_to_frame_ms,4020);
  assert.equal(preloaded.render_to_frame_ms,20);
  assert.equal(graphTiming({nodes:[]},5000,5020,8,0).data_to_frame_ms,null);
  assert.equal(JSON.stringify(graph),before);
  console.log(JSON.stringify({status:'pass',cases:5,actual_browser_measurement:false}));
} finally {
  await rm(output,{force:true});
}
