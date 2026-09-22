/**
 * A failed load must not be reported as an empty account.
 *
 * Found by using the app. Restarting the server invalidates the tab's
 * session, and the next load of the conversation list came back 401. The
 * screen then showed "No conversations yet. You start the first one below."
 * and an empty model picker -- the exact screen a brand-new install shows.
 * Nothing anywhere said the requests were being refused, so the only honest
 * reading available to the user was that their work was gone.
 *
 * The cause was one line: `.catch(() => setSessions([]))`. An empty array is
 * not a neutral fallback, it is a claim about the account.
 */
import { readFileSync } from 'node:fs';

const source = readFileSync(
  new URL('../src/screens/Studio.tsx', import.meta.url), 'utf8');

const call = source.slice(source.indexOf('listSessions(controller.signal)'));
const handler = call.slice(0, call.indexOf('listModels('));

if (/catch\s*\(\s*\)\s*=>\s*setSessions\(\s*\[\s*\]\s*\)/.test(handler)) {
  throw new Error(
    'the session list still claims "no conversations" when the load fails');
}

if (!handler.includes('ApiError')) {
  throw new Error('the failure path must distinguish an auth failure by status');
}

if (!/401/.test(handler)) {
  throw new Error('an expired session (401) must be named, not lumped in');
}

console.log('sessions-load-failure: ok');
