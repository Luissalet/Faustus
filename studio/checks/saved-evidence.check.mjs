import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from 'esbuild';

const root = resolve(import.meta.dirname, '../..');
const output = join(mkdtempSync(join(tmpdir(), 'faustus-evidence-')), 'fixture.mjs');
await build({ stdin: { contents: `
  import React from 'react';
  import { renderToStaticMarkup } from 'react-dom/server';
  import { summaryFrom } from './studio/src/adapters/chat';
  import { blankTurn, restoreFromMetadata } from './studio/src/screens/studio/model';
  import { SavedEvidence } from './studio/src/screens/studio/SavedEvidence';
  export { summaryFrom, blankTurn, restoreFromMetadata };
  export const render = (evidence) => renderToStaticMarkup(React.createElement(SavedEvidence, {evidence}));
`, resolveDir: root, loader: 'tsx' }, bundle: true, platform: 'node', format: 'esm',
  jsx: 'automatic', outfile: output, logLevel: 'silent',
  banner: { js: "import { createRequire } from 'node:module'; const require = createRequire(import.meta.url);" } });
const { summaryFrom, render, blankTurn, restoreFromMetadata } = await import(pathToFileURL(output).href);
const data = { changeset: { id: 'chg_safe', stored: true, verdict: 'proved' } };
const cs = summaryFrom(data).changeset;
assert.equal(cs.id, 'chg_safe');
assert.equal(cs.stored, true);
assert.equal(cs.verified, false, 'A proof word or successful tool call is not a verified run');
assert.equal(summaryFrom({changeset: {evidence_verified: true}}).changeset.verified, true);
assert.equal(summaryFrom({changeset: {evidence_verified: 'true'}}).changeset.verified, false);
assert.match(render(cs), /Read saved evidence/);
assert.match(render(cs), /aria-expanded="false"/);
assert.equal(render(summaryFrom({ changeset: { id: 'old' } }).changeset), '');
assert.equal(summaryFrom({ changeset: { stored: 'true', id: 123 } }).changeset.stored, undefined);
assert.equal(summaryFrom({ changeset: { stored: 'true', id: 123 } }).changeset.id, undefined);
assert.match(render({ ...cs, stored: false, storageReason: 'incognito' }), /incognito/);
assert.match(render({ ...cs, stored: false, storageReason: 'unavailable' }), /could not be saved/);
const restored = restoreFromMetadata(blankTurn('assistant', 'Done'), {harness: {...data, round_count: 3}});
assert.equal(restored.rounds, 3);
assert.equal(restored.summary.changeset.id, 'chg_safe');
assert.match(render(restored.summary.changeset), /Read saved evidence/);
console.log('Saved evidence: ALL OK');
