// The drawing surface of a note (studio/src/lib/paint.ts): pointer maths,
// the undo stack, the `bg:` colour sentinel and what counts as a safe
// picture. Bundled with esbuild on the fly; run by
// tests/test_studio_paint_js.py, or by hand:
//   node studio/checks/paint.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-paint-')), 'paint.mjs');
await build({ entryPoints: [join(root, 'studio', 'src', 'lib', 'paint.ts')], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
const p = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};

// ── Where the ink lands ──
{
  // The canvas is 600×320 but displayed at whatever width it gets: get the
  // scaling wrong and the stroke misses the finger by the ratio.
  const rect = { left: 100, top: 50, width: 300, height: 160 };
  const middle = p.pointIn(rect, 250, 130);
  assert(middle.x === 300 && middle.y === 160, `the middle of a half-size canvas is the middle: ${middle.x},${middle.y}`);
  const corner = p.pointIn(rect, 100, 50);
  assert(corner.x === 0 && corner.y === 0, 'the top-left corner is the origin');
  const full = p.pointIn({ left: 0, top: 0, width: 600, height: 320 }, 123, 45);
  assert(full.x === 123 && full.y === 45, 'at full size the coordinates pass through');
  const zero = p.pointIn({ left: 0, top: 0, width: 0, height: 0 }, 10, 10);
  assert(Number.isFinite(zero.x) && Number.isFinite(zero.y), 'a canvas with no box does not divide by zero');
}

// ── Shapes ──
{
  assert(Math.round(p.radius({ x: 0, y: 0 }, { x: 3, y: 4 })) === 5, 'the radius of a drag');
  assert(p.isDrag({ x: 0, y: 0 }, { x: 10, y: 0 }), 'a drag is a drag');
  assert(!p.isDrag({ x: 0, y: 0 }, { x: 1, y: 1 }), 'a tap is not a drag, so a shape tool leaves nothing behind');
}

// ── Undo ──
{
  let stack = [];
  for (let i = 0; i < 30; i += 1) stack = p.pushUndo(stack, i, 24);
  assert(stack.length === 24, 'the undo stack is bounded');
  assert(stack[0] === 6 && stack[23] === 29, 'and it drops the oldest, not the newest');
}

// ── Blank paper ──
{
  const white = new Uint8ClampedArray([255, 255, 255, 255, 255, 255, 255, 255]);
  const inked = new Uint8ClampedArray([255, 255, 255, 255, 10, 10, 10, 255]);
  assert(p.isBlank(white), 'white paper is blank');
  assert(!p.isBlank(inked), 'one stroke is not');
}

// ── The colour field doubles as a background ──
{
  assert(p.backgroundOf('bg:/api/upload/x.png') === '/api/upload/x.png', 'bg: carries a url');
  assert(p.backgroundOf('red') === null, 'a colour name is not a background');
  assert(p.asBackground('/x.png') === 'bg:/x.png', 'and back again');
}

// ── What is allowed to be a picture ──
{
  assert(p.safeImage('/api/upload/a.png') === '/api/upload/a.png', 'an upload');
  assert(p.safeImage('data:image/png;base64,AAA') !== null, 'a data image');
  assert(p.safeImage('javascript:alert(1)') === null, 'not javascript:');
  assert(p.safeImage('data:text/html,<script>') === null, 'not a data document');
  assert(p.safeImage('') === null && p.safeImage(null) === null && p.safeImage(undefined) === null, 'nothing is nothing');
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
