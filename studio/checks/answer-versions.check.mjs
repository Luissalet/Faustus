// Per-answer versions (‹ 1/3 ›): the adapter reads GET /api/session/{id}/alternatives,
// Studio refetches it when a turn ends, the Transcript hands each assistant
// turn the versions of the question before it, the pager is always visible
// (outside the hover-only actions) and "Use this version" goes through the
// existing restore. Static source assertions, no bundling.
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const read = (p) => readFileSync(new URL(p, import.meta.url), 'utf8');
const sessions = read('../src/adapters/sessions.ts');
assert.match(sessions, /export interface AnswerVersion/, 'AnswerVersion type');
assert.match(sessions, /export async function listAlternatives/, 'listAlternatives()');
assert.match(sessions, /\/alternatives`/, 'calls GET /api/session/{id}/alternatives');

const studio = read('../src/screens/Studio.tsx');
assert.match(studio, /listAlternatives\(sessionId, controller\.signal\)/, 'Studio fetches the versions');
assert.match(studio, /\[sessionId, busy, turnCount, knobs\.incognito\]/, 'refetched when a turn ends or the chat changes');
assert.match(studio, /await restoreVersion\(sessionId, versionId\)/, 'using a version is the existing restore');
assert.match(studio, /answerVersions=\{answerVersions\}/, 'passed to the Transcript');

const tr = read('../src/screens/studio/Transcript.tsx');
assert.match(tr, /function versionsFor\(/, 'versions are looked up by the question before the answer');
assert.match(tr, /data-testid="turn-versions"/, 'the pager');
assert.match(tr, /<span className="fs-turn__foot-left">\s*\{pager\}/, 'the pager sits outside the hover-only actions');
assert.match(tr, /testId="turn-version-use"/, 'Use this version');
assert.match(tr, /turn\.streaming \? \[\] : \(alternatives \?\? \[\]\)/, 'no pager while the answer streams');
console.log('answer-versions: ALL OK');
