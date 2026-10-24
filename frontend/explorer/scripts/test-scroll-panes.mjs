import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const css = await readFile('src/studio.css', 'utf8');
const source = await readFile('src/Workbench.tsx', 'utf8');
assert.match(css, /\.wb-studio \.wb-table-scroll\{[^}]*overscroll-behavior-y:auto/);
assert.doesNotMatch(css, /\.wb-studio \.wb-table-scroll\{[^}]*overscroll-behavior:contain/);
assert.match(source, /<main[^>]*tabIndex=\{0\}[^>]*aria-label="연구 근거 본문"/);
assert.match(source, /aria-label="근거 상세" tabIndex=\{0\}/);
console.log('Scroll propagation and keyboard-focus checks passed; actual wheel behavior requires browser verification');

assert.match(source, /exportAllowed=\{seed\.metadata\?\.local_food_composition!==true\}/);
assert.match(source, /\{exportAllowed\?<button/);
