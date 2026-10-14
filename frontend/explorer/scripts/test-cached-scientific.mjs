import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import Module from 'node:module';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

// Verify the actual saved-data artifact without opening blocked file URLs.
const project = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const root = path.resolve(project, '../..');
const artifact = path.join(root, 'outputs/explorer/scientific-20261003');
const snapshot = JSON.parse(await fs.readFile(path.join(artifact, 'NutriOmics_Explorer_scientific_cached.json'), 'utf8'));
const compiled = await build({ entryPoints: [path.join(project, 'src/api.ts')], bundle: true, write: false, platform: 'node', format: 'cjs' });
globalThis.window = { __NUTRIOMICS_SNAPSHOT__: snapshot };
let fetches = 0;
globalThis.fetch = async () => { ++fetches; throw new Error('Offline artifact must never fetch'); };
const filename = path.join(project, 'src/__cached_scientific_regression.cjs');
const instance = new Module(filename);
instance.filename = filename;
instance.paths = Module._nodeModulePaths(project);
instance._compile(compiled.outputFiles[0].text, filename);
const api = instance.exports;
const seed = 'FDC:2346394';
const result = await api.getScientificMatches({ seed_id: seed, intake_g: 800, reference_context: { tissue: 'PBMC', response_time_h: 2 } });
assert.equal(result.reference_context.intake_g, 28);
assert.equal(result.reference_context.frequency, 'single');
assert.equal(result.offline_context_frozen, true);
assert.equal(result.observed_evidence.length, 2);
for (const record of result.observed_evidence) {
  assert.equal(record.mapping_identity.status, 'related_food');
  assert.equal(record.mapping_identity.quantity_transfer_allowed, false);
  assert.equal(record.study_evidence.expert_review_status, 'pending');
  assert.equal(record.causal_inference, false);
  assert.equal(record.clinical_results.length, 0);
  for (const contrast of record.contrasts) {
    assert.equal(contrast.effects.length, 0);
    assert.equal(contrast.effect_lookup_status, 'cached_exact_key_count_only');
    assert.equal(contrast.analysis_context.version, contrast.analysis_version);
  }
}
const walnut = result.observed_evidence.find(record => record.study_id === 'N16');
assert.equal(walnut.exposure.duration_days, null);
assert.ok(walnut.contrasts.filter(contrast => contrast.contrast.startsWith('greenMED_vs_MED')).every(contrast => contrast.matched_exposure_role === 'shared_in_both_arms'));
assert.ok(walnut.contrasts.every(contrast => contrast.applicability.some(card => card.dimension === 'frequency' && card.status === 'mismatch')));
const rows = await api.getEntities('food', 'walnuts', { domain: 'all', tier: 'all', include_extracted: false }, 1, 20, 'label', 'asc');
assert.ok(rows.items.some(record => record.id === seed));
const html = await fs.readFile(path.join(artifact, 'NutriOmics_Explorer_scientific_cached.html'), 'utf8');
assert.ok(html.includes('window.__NUTRIOMICS_SNAPSHOT__='));
assert.ok(!html.includes('<script src='));
assert.equal(fetches, 0);
const receipt = { artifact: 'cached scientific selection; not production v5 export', node_count: snapshot.nodes.length, edge_count: snapshot.edges.length, studies: 2, contrasts: walnut.contrasts.length + result.observed_evidence.find(record => record.study_id === 'S04').contrasts.length, fetches, frozen_context: '28g single', runtime_api_status: 'pass', direct_file_browser_ui_status: 'not tested; file URL blocked' };
await fs.writeFile(path.join(artifact, 'cached-selection-verification.json'), JSON.stringify(receipt, null, 2));
console.log(JSON.stringify(receipt));
