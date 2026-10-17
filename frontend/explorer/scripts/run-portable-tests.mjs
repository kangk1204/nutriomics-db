import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const directory = path.dirname(fileURLToPath(import.meta.url));
const integration = new Set(['test-cached-scientific.mjs', 'test-production-snapshot.mjs']);
const names = (await fs.readdir(directory)).filter(n => /^test-.*\.mjs$/.test(n)).sort();
const passed = [];
for (const name of names) {
  if (integration.has(name)) continue;
  execFileSync(process.execPath, [path.join(directory, name)], {
    stdio: 'inherit', cwd: path.resolve(directory, '..'),
  });
  passed.push(name);
}
console.log(JSON.stringify({scope:'portable code tests', passed,
  separately_required:[...integration],
  integration_reason:'Require versioned real-data snapshots; these files are not distributed in the code archive.',
  actual_browser_verified:false}));
