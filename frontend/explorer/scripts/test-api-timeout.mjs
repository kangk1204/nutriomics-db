import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import Module from 'node:module';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

// Transport regression only: no browser, server, or scientific data is modified.
const project = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const compiled = await build({ entryPoints: [path.join(project, 'src/api.ts')], bundle: true, write: false, platform: 'node', format: 'cjs' });
function load(snapshot) {
  globalThis.window = snapshot ? { __NUTRIOMICS_SNAPSHOT__: snapshot } : {};
  const filename = path.join(project, 'src/__transport_regression.cjs');
  const instance = new Module(filename);
  instance.filename = filename;
  instance.paths = Module._nodeModulePaths(project);
  instance._compile(compiled.outputFiles[0].text, filename);
  return instance.exports;
}
const originalFetch = globalThis.fetch;
const originalSetTimeout = globalThis.setTimeout;
const originalClearTimeout = globalThis.clearTimeout;
const originalWindow = globalThis.window;
const scheduled = [];
const active = new Set();
const cases = [];
// Production 45/60-second deadlines are recorded exactly, then accelerated.
globalThis.setTimeout = (callback, delay, ...args) => {
  const handle = originalSetTimeout(() => { active.delete(handle); callback(...args); }, delay >= 30_000 ? 20 : delay);
  active.add(handle); scheduled.push(delay); return handle;
};
globalThis.clearTimeout = handle => { active.delete(handle); return originalClearTimeout(handle); };
const never = () => new Promise(() => {});
const wait = milliseconds => new Promise(resolve => originalSetTimeout(resolve, milliseconds));
async function check(name, run) { await run(); assert.equal(active.size, 0, `${name}: deadline cleanup`); cases.push({ name, status: 'pass' }); }
try {
  const api = load();
  assert.equal(api.REQUEST_TIMEOUT_MS, 45_000);
  assert.equal(api.EXPORT_TIMEOUT_MS, 60_000);
  const timedOut = error => error instanceof api.RequestTimeoutError && error.name !== 'AbortError' && /서버 연결.*다시 시도/.test(error.message);
  let transportSignal;
  await check('stalled_response_headers', async () => {
    globalThis.fetch = async (_, options) => { transportSignal = options.signal; return never(); };
    await assert.rejects(api.fetchWithTimeout('/test', {}, response => response.json(), 15), timedOut);
    assert.ok(transportSignal.aborted); assert.equal(transportSignal.reason.name, 'RequestTimeoutError');
  });
  await check('stalled_response_body', async () => {
    globalThis.fetch = async (_, options) => { transportSignal = options.signal; return { json: never }; };
    await assert.rejects(api.fetchWithTimeout('/test', {}, response => response.json(), 15), timedOut);
    assert.ok(transportSignal.aborted);
  });
  await check('one_deadline_includes_headers_and_body', async () => {
    globalThis.fetch = async () => { await wait(5); return { json: async () => { await wait(25); return {}; } }; };
    await assert.rejects(api.fetchWithTimeout('/test', {}, response => response.json(), 20), timedOut);
  });
  await check('external_cancel_before_headers', async () => {
    const external = new AbortController();
    globalThis.fetch = async (_, options) => { transportSignal = options.signal; return never(); };
    const pending = api.fetchWithTimeout('/test', { signal: external.signal }, response => response.json(), 1000);
    external.abort(); await assert.rejects(pending, error => error.name === 'AbortError');
    assert.ok(transportSignal.aborted); assert.equal(transportSignal.reason.name, 'AbortError');
  });
  await check('external_cancel_during_body', async () => {
    const external = new AbortController();
    globalThis.fetch = async () => ({ json: never });
    const pending = api.fetchWithTimeout('/test', { signal: external.signal }, response => response.json(), 1000);
    await wait(1); external.abort(); await assert.rejects(pending, error => error.name === 'AbortError');
  });
  await check('pre_cancelled_signal_never_fetches', async () => {
    const external = new AbortController(); external.abort(); let calls = 0;
    globalThis.fetch = async () => { ++calls; return new Response('{}'); };
    await assert.rejects(api.fetchWithTimeout('/test', { signal: external.signal }, response => response.json()), error => error.name === 'AbortError');
    assert.equal(calls, 0);
  });
  await check('fast_response_and_all_normal_routes_use_45_seconds', async () => {
    globalThis.fetch = async () => new Response(JSON.stringify({ release_id: 'transport-test', items: [], nodes: [], edges: [], total: 0 }), { headers: { 'Content-Type': 'application/json' } });
    const filters = { domain: 'all', tier: 'all', include_extracted: false };
    const before = scheduled.length;
    await api.getMeta(); await api.getNode('transport-test'); await api.getEdge('transport-test');
    await api.getEntities('food', '', filters, 1, 50, 'label', 'asc'); await api.getGraph('transport-test', filters, 80, 200);
    await api.getScientificMatches({ seed_id: 'transport-test', reference_context: {} }); await api.analyzeFood('transport-test', 100);
    assert.deepEqual(scheduled.slice(before), Array(7).fill(45_000));
  });
  await check('match_timeout_is_visible_and_retryable', async () => {
    globalThis.fetch = async () => never();
    await assert.rejects(api.getScientificMatches({ seed_id: 'transport-test', reference_context: {} }), error => timedOut(error) && /45초/.test(error.message));
  });
  await check('network_failure_has_korean_retry_message', async () => {
    globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
    await assert.rejects(api.getMeta(), error => error.name !== 'AbortError' && /서버에 연결하지 못했습니다.*다시 시도/.test(error.message));
  });
  await check('server_timeout_is_not_missing_release_or_zero_results', async () => {
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: { code: 'query_timeout' } }), { status: 503, headers: { 'Content-Type': 'application/json' } });
    await assert.rejects(api.getScientificMatches({ seed_id: 'transport-test', reference_context: {} }), error => /시간 한도/.test(error.message) && /0건을 의미하지 않습니다/.test(error.message) && !/아직 준비/.test(error.message));
    await assert.rejects(api.getMeta(), error => /0건을 의미하지 않습니다/.test(error.message));
    await assert.rejects(api.analyzeFood('transport-test', 100), error => /시간 한도/.test(error.message) && /0건을 의미하지 않습니다/.test(error.message) && !/기준을 확인할 수 없습니다/.test(error.message));
  });
  await check('meal_source_failure_is_not_a_food_composition_fact', async () => {
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: { code: 'source_unavailable' } }), { status: 503 });
    await assert.rejects(api.analyzeFood('transport-test', 100), error => /원본 자료를 현재 조회할 수 없습니다/.test(error.message) && !/이 식품의.*기준/.test(error.message));
    globalThis.fetch = async () => new Response('', { status: 503 });
    await assert.rejects(api.analyzeFood('transport-test', 100), error => /503/.test(error.message) && !/이 식품의.*기준/.test(error.message));
  });
  await check('changed_source_is_a_retryable_error', async () => {
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: { code: 'source_changed' } }), { status: 503 });
    await assert.rejects(api.getMeta(), error => /버전이 변경/.test(error.message) && /다시 조회/.test(error.message));
  });
  const exportPayload = { format: 'json', release_id: 'transport-test', node_ids: [], edge_ids: [], positions: {}, filters: { domain: 'all', tier: 'all', include_extracted: false } };
  await check('export_body_uses_60_second_deadline', async () => {
    globalThis.fetch = async () => ({ ok: true, headers: new Headers(), blob: never });
    const before = scheduled.length;
    await assert.rejects(api.exportGraph(exportPayload), error => timedOut(error) && /60초/.test(error.message));
    assert.deepEqual(scheduled.slice(before), [60_000]);
  });
  await check('fast_export_preserves_blob_and_filename', async () => {
    globalThis.fetch = async () => new Response('transport-only', { headers: { 'Content-Disposition': 'attachment; filename="test.json"' } });
    const result = await api.exportGraph(exportPayload);
    assert.equal(result.filename, 'test.json'); assert.equal(await result.blob.text(), 'transport-only');
  });
  await check('missing_route_gap_message_is_preserved', async () => {
    globalThis.fetch = async () => new Response('', { status: 404 });
    await assert.rejects(api.getScientificMatches({ seed_id: 'transport-test', reference_context: {} }), /과학적 조건 비교가 아직 준비되지/);
  });
  await check('untyped_503_does_not_assert_missing_scientific_evidence', async () => {
    globalThis.fetch = async () => new Response('', { status: 503 });
    await assert.rejects(api.getScientificMatches({ seed_id: 'transport-test', reference_context: {} }), error => /503/.test(error.message) && !/아직 준비/.test(error.message));
  });
  await check('offline_routes_never_fetch_or_start_deadline', async () => {
    const matches = { release_id: 'transport-test', seed_id: 'transport-test', rule_version: 'transport-test', reference_context: { intake_g: 100 }, observed_evidence: [], evidence_gaps: [] };
    const node = { id: 'transport-test', kind: 'food', label: '전송 회귀검증 항목' };
    const snapshot = { release_id: 'transport-test', offline: true, nodes: [node], edges: [], paths: [], gaps: [], node_details: { 'transport-test': { ...node, scientific_matches: matches } } };
    const offlineApi = load(snapshot); let calls = 0; const before = scheduled.length;
    globalThis.fetch = async () => { ++calls; throw new Error('Offline fetch'); };
    await offlineApi.getMeta(); await offlineApi.getNode(node.id); await offlineApi.getEdge('missing');
    await offlineApi.getEntities('food', '', exportPayload.filters, 1, 50, 'label', 'asc'); await offlineApi.getGraph(node.id, exportPayload.filters, 80, 200);
    assert.equal(await offlineApi.getScientificMatches({ seed_id: node.id, intake_g: 800, reference_context: { tissue: 'PBMC' } }), matches);
    await offlineApi.exportGraph(exportPayload); await offlineApi.exportGraph({ ...exportPayload, format: 'csv' });
    await assert.rejects(offlineApi.analyzeFood(node.id, 100), /독립 HTML/);
    assert.equal(calls, 0); assert.equal(scheduled.length, before);
  });
  const report = { status: 'pass', cases, request_timeout_ms: 45_000, export_timeout_ms: 60_000, deadline_timers_remaining: active.size, accelerated_deadlines: true, browser_verification: false, node: process.version, completed_at: new Date().toISOString() };
  const destination = path.join(project, '../../outputs/explorer/verification/frontend-api-timeout-regression.json');
  await fs.mkdir(path.dirname(destination), { recursive: true }); await fs.writeFile(destination, JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report));
} finally {
  globalThis.fetch = originalFetch; globalThis.setTimeout = originalSetTimeout; globalThis.clearTimeout = originalClearTimeout;
  if (originalWindow === undefined) delete globalThis.window; else globalThis.window = originalWindow;
}
