// Signing in with a subscription (studio/src/adapters/deviceAuth.ts).
//
// The provider gives a short code and a page; the server polls until the
// person has typed it. Three things went wrong in the version this replaces:
// the "complete" verification URL (which carries the code, so the page lands
// on Authorise with nothing to type) was ignored, a backend error came back
// as a blank card instead of its own message, and an expired flow looked
// identical to a pending one.
//
// `fetch` is stubbed here, so this exercises the adapter and not the network.
// Run by tests/test_device_sign_in_ui.py, or by hand:
//   node studio/checks/device.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-device-')), 'device.mjs');
await build({ entryPoints: [join(root, 'studio', 'src', 'adapters', 'deviceAuth.ts')], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
const d = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};

const calls = [];
const reply = (body, ok = true, status = 200) => {
  globalThis.fetch = async (url, init) => {
    calls.push({ url, method: init?.method });
    return { ok, status, text: async () => JSON.stringify(body) };
  };
};

const copilot = d.deviceProvider('copilot');
const chatgpt = d.deviceProvider('chatgpt-subscription');

// ── Both providers are there, on their own prefixes ──
{
  assert(copilot && chatgpt, 'both subscription providers exist');
  assert(copilot.prefix === '/api/copilot', 'copilot has its own prefix');
  assert(chatgpt.prefix === '/api/chatgpt-subscription', 'and so does the ChatGPT plan');
  assert(d.deviceProvider('nope') === undefined, 'an unknown id is not invented');
}

// ── The complete URL is preferred: it carries the code ──
{
  reply({
    poll_id: 'p1',
    user_code: 'ABCD-1234',
    verification_uri: 'https://github.com/login/device',
    verification_uri_complete: 'https://github.com/login/device?user_code=ABCD-1234',
    interval: 5,
    expires_in: 900,
  });
  const s = await d.startDeviceFlow(copilot);
  assert(s.userCode === 'ABCD-1234', 'the code comes back');
  assert(s.verificationUriComplete.includes('user_code='), 'the complete URL is the one to open');
  assert(s.verificationUri === 'https://github.com/login/device', 'and the plain one is kept for the fallback line');
  assert(s.intervalMs === 5000 && s.expiresInMs === 900000, 'seconds become milliseconds');
  assert(calls.at(-1).url === '/api/copilot/device/start' && calls.at(-1).method === 'POST', 'it posts to the right route');
}

// ── A provider with no complete URL falls back to the plain one ──
{
  reply({ poll_id: 'p2', user_code: 'WXYZ', verification_uri: 'https://auth.openai.com/device' });
  const s = await d.startDeviceFlow(chatgpt);
  assert(s.verificationUriComplete === 'https://auth.openai.com/device', 'no complete URL: the plain one is used, never an empty href');
  assert(s.intervalMs === 5000 && s.expiresInMs === 900000, 'and the defaults fill in');
}

// ── The backend's own message survives ──
{
  reply({ detail: 'GitHub device-code request failed (HTTP 502)' }, false, 502);
  let msg = '';
  try {
    await d.startDeviceFlow(copilot);
  } catch (e) {
    msg = e.message;
  }
  assert(msg.includes('502'), `the server's own words reach the card: ${msg}`);
}

// ── A thrown fetch is not swallowed ──
{
  globalThis.fetch = async () => {
    throw new Error('network down');
  };
  let msg = '';
  try {
    await d.startDeviceFlow(copilot);
  } catch (e) {
    msg = e.message;
  }
  assert(msg === 'network down', 'a transport failure is reported as itself');
}

// ── Pending, authorised and failed are told apart ──
{
  reply({ status: 'pending' });
  assert((await d.pollDeviceFlow(copilot, 'p1')).status === 'pending', 'pending');

  reply({ status: 'authorized', endpoint: { id: 'e1', name: 'Copilot' } });
  const okr = await d.pollDeviceFlow(copilot, 'p1');
  assert(okr.status === 'authorized' && okr.endpoint.id === 'e1', 'authorised, with the endpoint the server made');

  reply({ status: 'failed', error: 'expired_token' });
  const bad = await d.pollDeviceFlow(copilot, 'p1');
  assert(bad.status === 'failed' && bad.error === 'expired_token', 'expired is a failure with a reason, not a silent pending');

  reply({ status: 'failed' });
  assert((await d.pollDeviceFlow(copilot, 'p1')).error === 'denied', 'a failure with no reason still has one');

  reply({});
  assert((await d.pollDeviceFlow(copilot, 'p1')).status === 'pending', 'an answer with no status is pending, never authorised');
}

// ── Cancelling never throws: it is cleanup ──
{
  globalThis.fetch = async () => {
    throw new Error('gone');
  };
  let threw = false;
  try {
    await d.cancelDeviceFlow(copilot, 'p1');
  } catch {
    threw = true;
  }
  assert(!threw, 'a failed cancel is not worth an error: the flow expires on its own');
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
