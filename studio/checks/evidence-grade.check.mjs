import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createRequire } from 'node:module';
import { Window } from 'happy-dom';

const browser = new Window({ url: 'http://127.0.0.1/' });
globalThis.window = browser;
globalThis.document = browser.document;
globalThis.localStorage = browser.localStorage;
Object.defineProperty(globalThis, 'navigator', { value: browser.navigator, configurable: true });
const root = resolve(process.argv[2] || '.');
const out = join(mkdtempSync(join(tmpdir(), 'faustus-evidence-grade-')), 'render.cjs');
await build({ stdin: { resolveDir: root, contents: `
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { HarnessCard } from './studio/src/screens/studio/Harness';
export { summaryFrom, decode } from './studio/src/adapters/chat';
export { blankTurn, restoreFromMetadata } from './studio/src/screens/studio/model';
export const render = (summary, checks=[]) => renderToStaticMarkup(createElement(HarnessCard,
  { summary, checks, answer:'La letra r aparece 4 veces.', onNotice:()=>{} }));
` }, bundle: true, platform: 'node', format: 'cjs', outfile: out,
loader: { '.css': 'empty' }, logLevel: 'silent' });
const { summaryFrom, decode, blankTurn, restoreFromMetadata, render } = createRequire(import.meta.url)(out);
const answer = 'La letra r aparece 4 veces.';
const note = "evidence_notice: 'strawberry' contiene 3 veces la letra 'r'.";
const harness = { tool_calls:0, failed_calls:0, mutations:[], stop_reason:'complete',
  notes:[note], verification:{outcome:'unverified'}, round_count:1 };
const advisory = { status:'advisory', reasons:['wrong_calculation'] };
const live = decode({type:'harness_summary',data:harness},null);
assert.equal(live.type,'summary');
const restored = restoreFromMetadata(blankTurn('assistant',answer),{harness});
assert.equal(restored.text,answer,'Metadata must never rewrite the model answer');
function card(summary, checks=[]) {
  const holder=browser.document.createElement('div');holder.innerHTML=render(summary,checks);
  return holder.querySelector('[data-testid="studio-harness"]');
}
for (const summary of [live.summary,restored.summary]) {
  assert.equal(summary.verification?.outcome,'unverified','Existing grade survives live/history mapping');
  assert.deepEqual(summary.notes,[note]);
  const view=card(summary,[advisory]);
  assert.equal(view.dataset.verdict,'unverified');
  assert.equal(view.open,true,'Unsupported result is not hidden in a collapsed plain summary');
  assert.ok(view.textContent.includes("'strawberry' contiene 3 veces la letra 'r'."));
}
for (const outcome of ['verified','uncertain','unverified']) {
  const summary=summaryFrom({...harness,verification:{outcome},changeset:{evidence_verified:true}});
  assert.equal(summary.verification?.outcome,outcome);
  assert.equal(card(summary).dataset.verdict,outcome==='uncertain'?'plain':outcome);
}
for (const invalid of [undefined,null,true,'unverified',[],{}, {outcome:'invented'}, {outcome:1}]) {
  const summary=summaryFrom({...harness,verification:invalid});
  assert.equal(summary.verification,undefined);
  assert.equal(card(summary,[advisory]).dataset.verdict,'plain','Generic advisory is not an invented final grade');
}
const legacy=summaryFrom({...harness,verification:undefined,changeset:{evidence_verified:true}});
assert.equal(card(legacy).dataset.verdict,'verified','Older evidence receipts retain their presentation');
assert.equal(card(summaryFrom({...harness,verification:undefined,stop_reason:'complete_unverified'})).dataset.verdict,'unverified');
assert.equal(card(summaryFrom({...harness,verification:undefined}),[{status:'unverified'}]).dataset.verdict,'unverified');
console.log('ALL OK: emitted evidence grades survive live/history mapping and real HarnessCard rendering');
await browser.happyDOM.close();
