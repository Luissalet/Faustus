// The Effective Config inspector's own display logic
// (studio/src/screens/settings/EffectiveConfig.tsx): showValue() is what
// turns one resolved field's value into the table cell text. Getting it
// wrong is a silent misread of the exact thing this screen exists to make
// legible: `false`/`0` collapsing into "(unset)" would hide a real,
// deliberately-set value behind the same text an absent one shows.
//
// Bundled with esbuild on the fly; run by hand:
//   node studio/checks/effective-config.check.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['studio/src/screens/settings/EffectiveConfig.tsx'],
  bundle: true,
  platform: 'node',
  format: 'esm',
  write: false,
  jsx: 'automatic',
});
const { showValue } = await import(
  'data:text/javascript;base64,' + Buffer.from(bundle.outputFiles[0].text).toString('base64')
);

// A meaningful `false` or `0` must NEVER read the same as "nothing resolved
// this field" — that is exactly the ARCH-03 failure mode (a real value
// silently standing in for another) turned into a display bug.
assert.notEqual(showValue(false), showValue(null));
assert.notEqual(showValue(0), showValue(null));
assert.equal(showValue(null), showValue(undefined));

// Booleans render as words, not "true"/"false" JSON tokens.
assert.equal(showValue(true).toLowerCase().includes('true'), false);
assert.notEqual(showValue(true), showValue(false));

// Numbers and small objects render as their JSON form.
assert.equal(showValue(42), '42');
assert.equal(showValue({ a: 1 }), JSON.stringify({ a: 1 }));

// A long string (e.g. a full AGENTS.md block) is truncated with an ellipsis
// rather than blowing out the table row — but a short one is shown whole.
const long = 'x'.repeat(500);
assert.ok(showValue(long).length < long.length);
assert.ok(showValue(long).endsWith('…'));
assert.equal(showValue('short'), 'short');

console.log('Effective config showValue(): unset vs false/0, booleans, truncation — all passed');
