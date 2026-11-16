// Review, ratify and galaxy adapters + the galaxy camera maths
// (studio/src/adapters/brainReview.ts, studio/src/screens/brain/galaxyMath.ts).
// Run by tests/test_brain_review_js.py, or by hand:
//   node studio/checks/brain-review.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-brain-review-'));

async function load(entry, name) {
  const outfile = join(dir, `${name}.mjs`);
  await build({ entryPoints: [join(root, 'studio', 'src', entry)], bundle: true, format: 'esm', platform: 'node', outfile, logLevel: 'silent' });
  return import(pathToFileURL(outfile).href);
}

const review = await load('adapters/brainReview.ts', 'review');
const math = await load('screens/brain/galaxyMath.ts', 'math');

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};

// ── review parsing ──
const parsed = review.reviewFrom({
  contradictions: [{
    id: 'e1:works_at', rel: 'works_at', entity: { id: 'e1', name: 'Ada', type: 'person' },
    settled_by_ratification: true, ratified_ref: 'rel:r1',
    options: [{ ref: 'rel:r1', relation_id: 'r1', value: 'Cordera Labs', ratified: true, method: 'rule', confidence: 0.9 }, { ref: 'rel:r2', value: 'Bluehaven', method: 'llm' }],
  }],
  expired: [{ id: 'note:a.md', kind: 'note', ref: 'note:a.md', state: 'soon', expires_at: '2027-01-31', title: 'Promo', path: 'a.md' }, { id: 'rel:r9', kind: 'fact', state: 'expired', entity: { id: 'e1' } }],
  counts: { contradictions: 1, unsettled: 0, expired: 1, soon: 1, ratified: 1 },
});
assert(parsed.contradictions[0].options[0].ratified === true && parsed.contradictions[0].options[1].ratified === false, 'ratified flag per option');
assert(parsed.contradictions[0].settledByRatification && parsed.contradictions[0].ratifiedRef === 'rel:r1', 'settled by ratification');
assert(parsed.expired[0].kind === 'note' && parsed.expired[0].state === 'soon' && parsed.expired[1].entityId === 'e1', 'expiry rows');
assert(parsed.counts.ratified === 1 && parsed.counts.soon === 1, 'counts');
const empty = review.reviewFrom(null);
assert(empty.contradictions.length === 0 && empty.expired.length === 0 && empty.counts.unsettled === 0, 'tolerates a missing body');

// ── requests ──
const calls = [];
function reply(body, ok = true, status = 200) {
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return { ok, status, json: async () => body, clone() { return this; }, text: async () => JSON.stringify(body) };
  };
}
reply({ changed: ['rel:r1', 'rel:r2'] });
const out = await review.resolveReview({ action: 'keep', ref: 'rel:r1' });
assert(calls[0].url === '/api/brain/review/resolve' && JSON.parse(calls[0].init.body).action === 'keep', 'resolve posts action + ref');
assert(out.changed.length === 2, 'resolve returns the changed refs');
await review.resolveReview({ action: 'renew', ref: 'rel:r1', until: '' });
assert('until' in JSON.parse(calls[1].init.body) && JSON.parse(calls[1].init.body).until === '', 'an empty until is sent, to clear the expiry');
reply({});
await review.setRatified('ent:e1', true, 'ok');
await review.setRatified('ent:e1', false);
assert(JSON.parse(calls[2].init.body).note === 'ok' && JSON.parse(calls[3].init.body).off === true, 'ratify on and off');
reply({ detail: 'fact not found: x' }, false, 400);
let message = '';
try { await review.resolveReview({ action: 'archive', ref: 'rel:x' }); } catch (e) { message = String(e.message); }
assert(message.includes('fact not found'), 'a 400 carries the server detail');

// ── galaxy parsing ──
const galaxy = review.galaxyFrom({
  nodes: [
    { id: 'project:p1', kind: 'project', label: 'Alpha', pos: [1, 0, 0], size: 7 },
    { id: 'session:s1', kind: 'session', label: 'Chat', pos: [1.1, 0.1, 0], size: 3 },
    { id: 'entity:e1', kind: 'entity', label: 'Ada', pos: [0, 2, 0], size: 3, ratified: true, ref: 'ent:e1' },
  ],
  links: [['session:s1', 'project:p1'], ['session:s1', 'ghost:1']],
  counts: { project: 1, session: 1, entity: 1 },
  radius: 2.2,
});
assert(galaxy.nodes.length === 3 && galaxy.nodes[2].ratified && galaxy.nodes[2].ref === 'ent:e1', 'galaxy nodes');
assert(galaxy.links.length === 1 && galaxy.links[0][0] === 1 && galaxy.links[0][1] === 0, 'links become indexes and unknown ids are dropped');
assert(galaxy.counts.agent === 0 && galaxy.counts.project === 1, 'counts default to zero');

// ── camera maths ──
const cam = math.fitCamera(2);
const vp = math.viewProjection(cam, 1.5);
const centre = math.project(vp, [0, 0, 0], 600, 400);
assert(Math.abs(centre.x - 300) < 0.5 && Math.abs(centre.y - 200) < 0.5 && centre.w > 0, 'the target projects to the middle of the canvas');
const eye = math.eyeOf(cam);
const behind = math.project(vp, [eye[0] * 3, eye[1] * 3, eye[2] * 3], 600, 400);
assert(!(behind.w > 0) && Number.isNaN(behind.x), 'a point behind the camera is not drawn');
const positions = [[0, 0, 0], [0.5, 0, 0]];
const spot = math.project(vp, positions[1], 600, 400);
assert(math.pick(positions, [4, 4], [true, true], vp, 600, 400, spot.x, spot.y) === 1, 'picking finds the point under the cursor');
assert(math.pick(positions, [4, 4], [true, false], vp, 600, 400, spot.x, spot.y) === -1, 'a hidden cloud cannot be picked');
assert(math.pick(positions, [4, 4], [true, true], vp, 600, 400, 5, 5) === -1, 'empty space picks nothing');
const clamped = math.clampCamera({ yaw: 0, pitch: 9, distance: 99, target: [0, 0, 0] });
assert(clamped.pitch < 1.5 && clamped.distance === 12, 'the camera is clamped');

if (failed) {
  console.error(`${failed} check(s) failed`);
  process.exit(1);
}
console.log('ok brain-review');
