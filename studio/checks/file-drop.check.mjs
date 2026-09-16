// Composer file-drop highlight: `dragleave` bubbles, so a parent that
// clears "dragging" on every leave flickers as the pointer crosses the
// textarea/buttons, and Chrome/Electron can cancel the drop. The session
// in studio/src/lib/file-drop.ts is what Composer.tsx must use.
//
// Run by tests/test_studio_file_drop_js.py, or by hand:
//   node studio/checks/file-drop.check.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';
import { build } from 'esbuild';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
async function bundle(entry) {
  const result = await build({
    entryPoints: [join(root, entry)],
    bundle: true,
    format: 'esm',
    platform: 'node',
    write: false,
    logLevel: 'silent',
  });
  return import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);
}

const { isFileDrag, createFileDropSession } = await bundle('studio/src/lib/file-drop.ts');

assert.equal(isFileDrag(['Files']), true, 'OS file drags advertise the Files type');
assert.equal(isFileDrag(['text/plain']), false, 'text drags are not file drops');
assert.equal(isFileDrag(null), false, 'missing dataTransfer is not a file drag');

function fakeClock() {
  let id = 0;
  const pending = new Map();
  return {
    clock: {
      schedule: (fn) => { const n = ++id; pending.set(n, fn); return n; },
      cancel: (n) => pending.delete(n),
    },
    flush() {
      for (const [key, fn] of [...pending]) {
        pending.delete(key);
        fn();
      }
    },
    get size() { return pending.size; },
  };
}

{
  const seen = [];
  createFileDropSession((active) => seen.push(active)).enter(['text/uri-list']);
  assert.deepEqual(seen, [], 'non-file enter does not highlight');
}

{
  const seen = [];
  const timers = fakeClock();
  const drop = createFileDropSession((active) => seen.push(active), timers.clock);
  drop.enter(['Files']);
  assert.deepEqual(seen, [true], 'entering the composer highlights');
  // Crossing from the textarea onto a button: leave the old child, enter the
  // new one. Both bubble to the form. The delayed leave must not fire.
  drop.enter(['Files']);
  drop.leave();
  assert.equal(timers.size, 0, 'leaving a child for another child does not schedule a clear');
  assert.ok(!seen.includes(false), 'child crossings do not flicker the highlight off');
  drop.leave();
  assert.equal(timers.size, 1, 'leaving the composer schedules a clear');
  assert.ok(!seen.includes(false), 'the highlight stays until the leave delay elapses');
  timers.flush();
  assert.equal(seen.at(-1), false, 'the highlight clears after the pointer has actually left');
}

{
  const seen = [];
  const timers = fakeClock();
  const drop = createFileDropSession((active) => seen.push(active), timers.clock);
  drop.enter(['Files']);
  drop.leave();
  drop.enter(['Files']);
  assert.equal(timers.size, 0, 're-entering cancels the pending leave');
  timers.flush();
  assert.ok(!seen.includes(false), 'a cancelled leave never clears the highlight');
}

{
  const seen = [];
  const timers = fakeClock();
  const drop = createFileDropSession((active) => seen.push(active), timers.clock);
  drop.enter(['Files']);
  drop.enter(['Files']);
  drop.drop();
  assert.equal(timers.size, 0, 'a successful drop cancels any pending leave');
  assert.equal(seen.at(-1), false, 'a successful drop clears the highlight immediately');
  const n = seen.length;
  drop.leave();
  timers.flush();
  assert.ok(!seen.slice(n).includes(true), 'a leftover leave after drop cannot highlight again');
}

{
  const seen = [];
  const timers = fakeClock();
  const drop = createFileDropSession((active) => seen.push(active), timers.clock);
  drop.enter(['Files']);
  drop.leave();
  drop.dispose();
  assert.equal(timers.size, 0, 'dispose cancels a pending leave');
  timers.flush();
  assert.deepEqual(seen, [true], 'dispose does not toggle the highlight after unmount');
}

const composer = readFileSync(join(root, 'studio/src/screens/studio/Composer.tsx'), 'utf8');
assert.ok(composer.includes("from '../../lib/file-drop'"), 'Composer.tsx uses the drop session, not a raw dragleave toggle');
assert.ok(composer.includes('createFileDropSession'), 'Composer.tsx constructs the drop session');
assert.ok(composer.includes('dropSession.dispose'), 'Composer.tsx cancels a pending leave if the composer unmounts mid-drag');
assert.ok(!/onDragLeave=\{\(\)\s*=>\s*setDragging\(false\)\}/.test(composer), 'Composer.tsx must not clear dragging on every bubbling dragleave');
assert.ok(!composer.includes('{dragging && ('), 'the drop hint must stay mounted; inserting it mid-drag shifts layout and retriggers leave/enter');

const css = readFileSync(join(root, 'studio/src/screens/studio.css'), 'utf8');
assert.ok(css.includes('.fs-studio__drop-hint'), 'the drop hint is a dedicated overlay, not an in-flow paste line');
assert.ok(/\.fs-studio__drop-hint[\s\S]{0,400}pointer-events:\s*none/.test(css), 'the drop hint cannot steal the drag; pointer-events none keeps leave/enter on the form');
assert.ok(!/\.fs-studio__drop-hint\s*\{[^}]*--fs-glass/.test(css), 'drop hint must be an opaque surface; glass shows the attachment card and the empty right side as two colours');

const tsv = readFileSync(join(root, 'docs/ui/i18n/es.tsv'), 'utf8');
assert.ok(tsv.split('\n').some((line) => line.startsWith('Drop to attach\t')), 'docs/ui/i18n/es.tsv has a Spanish row for the drop hint');

console.log('ALL OK: file-drop session survives child crossings; composer overlay does not steal the drag');
