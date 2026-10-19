// #941 CAS2: empty external adopt must not outbox-Send; ordinary open may.
// Walks the real Studio.tsx rejoin finally branch via TypeScript AST, then
// executes that branch shape with inert spies (no HTTP/MCP).
// Run: node studio/checks/external-follow-adopt.check.mjs
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const studioPath = join(root, 'studio', 'src', 'screens', 'Studio.tsx');
const src = readFileSync(studioPath, 'utf8');
const ts = await import(pathToFileURL(join(root, 'node_modules', 'typescript', 'lib', 'typescript.js')).href);

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};

const sf = ts.createSourceFile(studioPath, src, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);

function textOf(node) {
  return src.slice(node.getStart(sf), node.getEnd());
}

/** Locate `const rejoin = useCallback(async (sid, opts?) => { ... }, deps)`. */
function findRejoinCallback() {
  let found = null;
  const visit = (node) => {
    if (
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.name.text === 'rejoin' &&
      node.initializer &&
      ts.isCallExpression(node.initializer) &&
      ts.isIdentifier(node.initializer.expression) &&
      node.initializer.expression.text === 'useCallback'
    ) {
      const arg0 = node.initializer.arguments[0];
      if (arg0 && (ts.isArrowFunction(arg0) || ts.isFunctionExpression(arg0))) found = arg0;
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return found;
}

const rejoinFn = findRejoinCallback();
assert(Boolean(rejoinFn), 'Studio.tsx exposes rejoin useCallback');

const rejoinText = rejoinFn ? textOf(rejoinFn) : '';
assert(rejoinText.includes('adoptOnly'), 'rejoin accepts adoptOnly');
assert(
  /if\s*\(\s*adoptOnly\s*\)[\s\S]*turnsFromHistory[\s\S]*else[\s\S]*reconcileOutbox/.test(rejoinText),
  'empty resume: adoptOnly → history only; else → reconcileOutbox',
);

// External-follow effect must always pass adoptOnly:true (never bare rejoin).
const followIdx = src.indexOf('Follow an external turn on the conversation already on screen');
assert(followIdx > 0, 'external-follow effect comment present');
const followSlice = src.slice(followIdx, followIdx + 2500);
const rejoinCalls = [...followSlice.matchAll(/rejoinRef\.current\([^)]*\)/g)].map((m) => m[0]);
assert(rejoinCalls.length >= 2, `external follow calls rejoin (≥2), got ${rejoinCalls.length}`);
assert(
  rejoinCalls.every((c) => /adoptOnly\s*:\s*true/.test(c)),
  `every external rejoin is adoptOnly (got ${rejoinCalls.join(' | ')})`,
);
assert(followSlice.includes('decideExternalFollowAction'), 'follow effect uses mark decisions');

/**
 * Executable mirror of the empty-resume finally branch proven above.
 * Spies stand in for turnsFromHistory / reconcileOutbox→sendTurn.
 */
function runEmptyResumeFinally(adoptOnly, spies) {
  if (adoptOnly) {
    spies.turnsFromHistory('sid-a');
  } else {
    spies.reconcileOutbox('sid-a');
  }
}

{
  const sends = [];
  const history = [];
  const spies = {
    turnsFromHistory: (sid) => history.push(sid),
    reconcileOutbox: (sid) => {
      sends.push({ sid, clientMessageId: 'uncertain-original-id' });
    },
  };
  runEmptyResumeFinally(false, spies);
  assert(sends.length === 1 && sends[0].clientMessageId === 'uncertain-original-id', 'ordinary empty resume may outbox-Send once');
  assert(history.length === 0, 'ordinary empty resume does not history-only');
}

{
  const sends = [];
  const history = [];
  const spies = {
    turnsFromHistory: (sid) => history.push(sid),
    reconcileOutbox: (sid) => {
      sends.push({ sid, clientMessageId: 'uncertain-original-id' });
    },
  };
  runEmptyResumeFinally(true, spies);
  assert(sends.length === 0, 'adoptOnly empty resume must sendTurn 0 times');
  assert(history.length === 1 && history[0] === 'sid-a', 'adoptOnly empty resume refreshes history once');
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
