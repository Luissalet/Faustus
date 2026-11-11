// The whiteboard's pure logic (studio/src/lib/pizarraModel.ts): the patch algebra the person's undo history and
// the live merge with the agent rest on, the geometry (align, snap, connector anchors) and the pen.
// Bundled with esbuild on the fly; run by tests/test_surfaces_studio_js.py, or by hand:
//   node studio/checks/surfaces.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-pizarra-'));
const out = join(dir, 'model.mjs');
await build({ entryPoints: [join(root, 'studio', 'src', 'lib', 'pizarraModel.ts')], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
const m = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (c, msg) => { if (!c) { failed += 1; console.error('FAIL:', msg); } else console.log('ok:', msg); };
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const a = m.makeNode('sticky', 0, 0, { text: 'A' });
const b = m.makeNode('rect', 300, 0, { text: 'B' });
const c = m.makeNode('ellipse', 600, 100, { text: 'C' });
const doc = { nodes: [a, b, c], edges: [{ id: 'e1', fromNode: a.id, toNode: b.id, toEnd: 'arrow' }] };

{
  const moved = { ...doc, nodes: doc.nodes.map((n) => (n.id === a.id ? { ...n, x: 50 } : n)) };
  const d = m.diffDocs(doc, moved);
  assert(d && Object.keys(d.after.nodes).length === 1 && d.after.nodes[a.id].x === 50, 'diffDocs lists only the item that changed');
  assert(d.before.nodes[a.id].x === 0, 'the before side keeps the old values');
  assert(eq(m.applyPatch(moved, d.before), doc), 'applying `before` undoes the change');
  assert(eq(m.applyPatch(doc, d.after), moved), 'applying `after` redoes it');
  assert(m.diffDocs(doc, doc) === null, 'no change gives no patch');
}
{
  const removed = { nodes: [b, c], edges: [] };
  const d = m.diffDocs(doc, removed);
  assert(d.after.nodes[a.id] === null && d.after.edges.e1 === null, 'a deleted item and its connector are null in `after`');
  assert(eq(m.applyPatch(removed, d.before), doc), 'undoing a delete brings the item and its connector back');
}
{
  const patch = { nodes: { [c.id]: { ...c, x: 999 }, ghost: null } };
  const out2 = m.applyPatch(doc, patch, new Set([c.id]));
  assert(out2.nodes.find((n) => n.id === c.id).x === 600, 'an item the person is dragging is left alone by a remote patch');
  const out3 = m.applyPatch(doc, patch);
  assert(out3.nodes.find((n) => n.id === c.id).x === 999 && out3.nodes.length === 3, 'otherwise the remote write lands (the last write wins)');
}
{
  const orphan = m.applyPatch(doc, { nodes: { [b.id]: null } });
  assert(orphan.edges.length === 0, 'a connector whose end was deleted goes with it');
  const merged = m.mergePatches({ nodes: { x: a } }, { nodes: { x: { ...a, x: 7 }, y: b } });
  assert(merged.nodes.x.x === 7 && Object.keys(merged.nodes).length === 2, 'merged patches keep the last write per item');
  assert(m.patchSize(merged) === 2, 'patchSize counts items');
}
{
  const items = [{ id: 'a', x: 0, y: 0, width: 100, height: 50 }, { id: 'b', x: 200, y: 30, width: 100, height: 50 }, { id: 'c', x: 700, y: 90, width: 100, height: 50 }];
  const top = m.alignRects(items, 'top');
  assert(Object.keys(top).includes('c') && Object.values(top).every((p) => p.y === 0), 'align top puts every item on the same top edge');
  const dist = m.alignRects(items, 'distribute-h');
  assert(dist.b.x === 350, `distribute spaces the gaps evenly (b.x = ${dist.b && dist.b.x})`);
  const mid = m.alignRects(items, 'center');
  assert(new Set(Object.values(mid).map((p) => p.x + 50)).size === 1, 'align centres shares one vertical axis');
}
{
  const snap = m.snapBox({ x: 203, y: 100, width: 100, height: 50 }, [{ x: 0, y: 0, width: 200, height: 50 }], 6);
  assert(snap.dx === -3 && snap.guides.length >= 1, 'snap pulls an edge onto a nearby edge and reports a guide');
  const far = m.snapBox({ x: 400, y: 400, width: 10, height: 10 }, [{ x: 0, y: 0, width: 20, height: 20 }], 6);
  assert(far.dx === 0 && far.dy === 0 && far.guides.length === 0, 'nothing snaps beyond the threshold');
}
{
  const p = m.borderPoint({ x: 0, y: 0, width: 100, height: 50 }, 300, 25, 'rect');
  assert(p.x === 100 && p.y === 25, 'a connector leaves a box on the side that faces its target');
  const q = m.borderPoint({ x: 0, y: 0, width: 100, height: 100 }, 100, 50, 'ellipse');
  assert(Math.abs(q.x - 100) < 0.01 && Math.abs(q.y - 50) < 0.01, 'on an ellipse it leaves on the curve');
  const diamond = m.borderPoint({ x: 0, y: 0, width: 100, height: 100 }, 150, 50, 'diamond');
  assert(Math.abs(diamond.x - 100) < 0.01, 'on a diamond it leaves at the point');
}
{
  const line = Array.from({ length: 50 }, (_, i) => [i, i * 2]);
  assert(m.simplifyStroke(line).length === 2, 'a straight pen line collapses to its two ends');
  const stroke = m.strokeNode([[10, 10], [20, 40], [50, 12]], '1');
  assert(stroke && stroke.fx.k === 'stroke' && stroke.width >= 40 && stroke.color === '1', 'a pen stroke becomes one item sized to its points');
  assert(m.strokeNode([[1, 1]], undefined) === null, 'a single tap draws nothing');
}
{
  const dup = m.duplicateItems(doc, new Set([a.id, b.id]));
  assert(dup.nodes.length === 2 && dup.edges.length === 1 && dup.edges[0].fromNode !== a.id, 'duplicate copies the items and the connector between them with new ids');
  assert(dup.nodes[0].x === a.x + 32, 'the copies are shifted');
  const f = m.frameAround([a, b], 'Plan');
  assert(f && f.type === 'group' && f.x < a.x && f.x + f.width > b.x + b.width, 'a frame wraps what it groups');
  assert(eq(m.framesHolding({ nodes: [f, a, b, c], edges: [] }, f).sort(), [a.id, b.id].sort()), 'moving a frame carries the items inside it, not the ones outside');
}
{
  assert(m.kindOf(a) === 'sticky' && m.kindOf(m.makeNode('frame', 0, 0)) === 'frame' && m.kindOf({ id: 'x', type: 'link', url: 'u', x: 0, y: 0, width: 1, height: 1 }) === 'link', 'kinds come from fx.k or the JSON Canvas type');
  assert(m.colorHex('3') === '#e2b714' && m.colorHex('#abc') === '#abc' && m.colorHex(undefined) === undefined, 'preset colours map to hex, hex stays');
  const t = m.withText(a, 'hello');
  assert(m.textOf(t) === 'hello', 'withText / textOf round-trip');
}

console.log(failed === 0 ? 'ok pizarra' : `FAIL: ${failed} assertion(s) failed`);
process.exit(failed === 0 ? 0 : 1);
