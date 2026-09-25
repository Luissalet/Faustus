// routeForSession (adapters/chat.ts): which model route a reopened
// conversation lands on. Bundled with esbuild on the fly; run by
// tests/test_studio_session_route_js.py, or by hand:
//   node studio/checks/session-route.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-route-'));
const out = join(dir, 'chat.mjs');
await build({ entryPoints: [join(root, 'studio', 'src', 'adapters', 'chat.ts')], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
const { routeForSession } = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (c, msg) => { if (!c) { failed += 1; console.error('FAIL:', msg); } else console.log('ok:', msg); };

const route = (endpointId, endpointUrl, model) => ({ id: `${endpointId}::${model}`, model, endpointId, endpointName: endpointId, endpointUrl, kind: '' });
const routes = [
  route('ollama', 'http://127.0.0.1:11434/v1/chat/completions', 'qwen3.8:27b-q4_K_M'),
  route('ollama', 'http://127.0.0.1:11434/v1/chat/completions', 'shared-name'),
  route('llama', 'http://127.0.0.1:8081/v1/chat/completions', 'qwen3.8-27b-q8-llamacpp'),
  route('llama', 'http://127.0.0.1:8081/v1/chat/completions', 'shared-name'),
];

assert(routeForSession(routes, 'qwen3.8-27b-q8-llamacpp', 'http://127.0.0.1:8081/v1')?.id === 'llama::qwen3.8-27b-q8-llamacpp',
  'a conversation on the llama-server lands on that route, whatever URL tail the session stored');
assert(routeForSession(routes, 'shared-name', 'http://127.0.0.1:8081/v1')?.endpointId === 'llama',
  'the same model name on two servers: the conversation keeps its own server');
assert(routeForSession(routes, 'shared-name', '')?.endpointId === 'ollama',
  'no stored endpoint: the first route with that model');
assert(routeForSession(routes, 'shared-name', 'http://10.0.0.9:9999/v1')?.endpointId === 'ollama',
  'a stored endpoint no longer listed: the model anywhere');
assert(routeForSession(routes, 'gone-model', 'http://127.0.0.1:8081/v1') === null, 'a model no longer listed: null');
assert(routeForSession(routes, '', '') === null, 'no model: null');
assert(routeForSession([], 'qwen3.8-27b-q8-llamacpp', '') === null, 'routes still loading: null (the screen retries when they arrive)');

if (failed) { console.error(`${failed} FAILED`); process.exit(1); }
console.log('ALL OK');
