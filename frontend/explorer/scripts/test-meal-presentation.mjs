import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { transform } from 'esbuild';
const compiled = await transform(await readFile('src/mealPresentation.ts', 'utf8'), {loader:'ts',format:'esm'});
const {mealPresentation} = await import('data:text/javascript;base64,' + Buffer.from(compiled.code).toString('base64'));
const original = {
  status:'partial', components:[
    {component_id:1,name:'Measured zero',amount:0,unit:'mg',source_records:[{basis:'source_100g',release:'v1'}]},
    {component_id:2,name:'Missing amount',amount:null,unit:'g'},
    {component_id:3,name:'Unknown unit',amount:4,unit:null},
    {component_id:4,name:'Numeric text is not a measurement',amount:'12',unit:'ug'},
  ], unquantified:[{name:'Below limit',status:'below_detection',source_amount:null,exclusion_reasons:['not_quantifiable']}],
  observed_evidence:[{effect:5}], interpretation:'섭취량은 혈중 농도가 아닙니다.'
};
const result=mealPresentation(original);
assert.equal(result.components[0].amount,0);
assert.equal(result.components[1].amount,null);
assert.equal(result.components[2].unit,null);
assert.equal(result.components[3].amount,null);
assert.equal(result.unquantified[0].status,'below_detection');
assert.deepEqual(result.components[0].sources,original.components[0].source_records);
assert.equal(result.components.length,4);
assert.equal(result.interpretation,original.interpretation);
assert.deepEqual(mealPresentation({components:null,unquantified:[null,'invalid']}),{components:[],unquantified:[],interpretation:'',status:'unavailable'});
console.log('9 meal presentation checks passed; zeros, missingness, units and source basis retained');
