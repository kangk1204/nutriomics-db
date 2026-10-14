import assert from 'node:assert/strict';
import Module from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const project = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const compiled = await build({ entryPoints: [path.join(project, 'src/graphFocus.ts')], bundle: true, write: false, platform: 'node', format: 'cjs' });
const filename = path.join(project, 'src/__reading_order.cjs');
const module = new Module(filename);
module.filename = filename;
module._compile(compiled.outputFiles[0].text, filename);
const { measurementsForReading, graphForDisplay } = module.exports;
const rows = [
  { id: 'unmeasured', component_label: 'Ash', amount: null, basis: 'source_100mL', status: 'not_measured', unit: 'g' },
  { id: 'zero', component_label: 'Copper, Cu', amount: 0, status: 'measured_zero', unit: 'mg' },
  { id: 'energy-specific', component_label: 'Energy (Atwater Specific Factors)', amount: 50.1, unit: 'kcal' },
  { id: 'protein', component_label: 'Protein', amount: 4.23, unit: 'g' },
  { id: 'energy-general', component_label: 'Energy (Atwater General Factors)', amount: 50, unit: 'kcal' },
];
const before = JSON.stringify(rows);
const sorted = measurementsForReading(rows);
assert.equal(JSON.stringify(rows), before, 'input order/values must remain untouched');
assert.deepEqual(sorted.map(row => row.id), ['energy-specific', 'energy-general', 'protein', 'unmeasured', 'zero']);
assert.equal(sorted.find(row => row.id === 'zero').amount, 0);
assert.equal(sorted.find(row => row.id === 'unmeasured').amount, null);
assert.equal(sorted.find(row => row.id === 'unmeasured').basis, 'source_100mL');
assert.ok(sorted.every(row => rows.includes(row)), 'records remain identical references');

// A small graph projection must contain only existing nodes/edges and no fabricated path.
const nodes = [{ id: 'food', label: 'Food', kind: 'food' }, ...rows.map(row => ({ id: row.id, label: row.component_label, kind: 'nutrient' }))];
const edges = rows.map(row => ({ id: `edge-${row.id}`, subject: 'food', object: row.id, tier: 'observed_composition', relation: 'contains_nutrient' }));
const graph = { nodes, edges, paths: [], gaps: [], release_id: 'unit-fixture' };
const focused = graphForDisplay(graph, 'food', 'focus');
assert.ok(focused.nodes.length <= 5);
assert.equal(focused.paths.length, 0);
assert.ok(focused.nodes.every(node => graph.nodes.includes(node)));
assert.ok(focused.edges.every(edge => graph.edges.includes(edge)));
const ids = new Set(focused.nodes.map(node => node.id));
assert.ok(focused.edges.every(edge => ids.has(edge.subject) && ids.has(edge.object)));
assert.ok(ids.has('energy-general') && ids.has('energy-specific') && ids.has('protein'));
assert.equal(graphForDisplay(graph, 'food', 'all'), graph);
console.log(JSON.stringify({ passed: true, cases: ['reading_order_preserves_values_units_and_missingness', 'separate_analytical_energy_records', 'focus_uses_only_existing_relationships', 'all_view_preserves_graph'] }));
