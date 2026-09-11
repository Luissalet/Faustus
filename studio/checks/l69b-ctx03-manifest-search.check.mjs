// Lote 69b — CTX-03: `manifestItemMatches` (studio/src/screens/Context.tsx),
// the client-side "search within this packet" filter ManifestPane's search
// box uses over the manifest rows already on the wire — matches across
// section/source type/reference/lanes/transformation/reason, case-insensitively.
//
// Run by tests/test_l69b_studio_checks_js.py, or by hand:
//   node studio/checks/l69b-ctx03-manifest-search.check.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';

async function bundle(entry) {
  const result = await build({ entryPoints: [entry], bundle: true, format: 'esm', platform: 'node', write: false, logLevel: 'silent', loader: { '.css': 'empty' }, jsx: 'automatic' });
  return import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);
}

const { manifestItemMatches } = await bundle('studio/src/screens/Context.tsx');

let failed = 0;
const check = (condition, message) => {
  if (!condition) { failed += 1; console.error('FAIL:', message); }
  else console.log('ok', message);
};

const item = {
  itemId: 'i1', section: 'retrieved_documents', sourceType: 'expert', sourceRef: 'expert:faustus-style#3',
  lanes: ['primary', 'safety'], transformation: 'summarised', generated: true, tokens: 120, reason: 'Matched the active goal',
};

check(manifestItemMatches(item, 'expert'), 'matches on sourceType');
check(manifestItemMatches(item, 'faustus-style'), 'matches on sourceRef, including the chunk suffix');
check(manifestItemMatches(item, 'safety'), 'matches on a lane, not just the joined section/source fields');
check(manifestItemMatches(item, 'summaris'), 'matches on transformation');
check(manifestItemMatches(item, 'active goal'), 'matches on the reason ("why") text');
check(manifestItemMatches(item, 'RETRIEVED_documents'), 'matching is case-insensitive');
check(!manifestItemMatches(item, 'render'), 'an unrelated needle does not match');
check(manifestItemMatches(item, ''), 'an empty needle matches everything (the caller only filters when there is a query)');

if (failed) {
  console.error(`${failed} check(s) FAILED`);
  process.exit(1);
}
console.log('ALL OK: Context.tsx\'s manifestItemMatches filters a packet\'s rows across every visible field');
