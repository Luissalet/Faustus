// The serve command the Cookbook builds (studio/src/lib/cookbook/serve.ts).
//
// These pin BEHAVIOUR, not source text: build a command and read it. The
// interface this replaced was pinned by grepping its own JavaScript for
// exact substrings, which passed happily whenever the string moved and the
// behaviour changed. Every case below was a real bug once — a GPU flag on a
// CPU-only run, `python3` on Windows, `--swap-space` with nothing after it.
//
// Bundled with esbuild on the fly; run by tests/test_studio_serve_js.py, or
// by hand:
//   node studio/checks/serve.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-serve-')), 'serve.mjs');
await build({ entryPoints: [join(root, 'studio', 'src', 'lib', 'cookbook', 'serve.ts')], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
const s = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};

const ctx = (over = {}) => ({
  platform: 'linux',
  remoteHost: '',
  env: 'none',
  envPath: '',
  hwBackend: 'cuda',
  hostPlatform: 'linux',
  ...over,
});
const cmd = (fields, model, backend, over) => s.buildServeCmd(fields, model, backend, ctx(over));

// ── CPU-only drops every GPU-only flag ──
{
  // The gate is ngl === 0, however it got there: typed, or picked as "CPU".
  const cpu = cmd({ ngl: '0', flash_attn: true, unified_mem: true, _gguf_path: '/m.gguf' }, 'm', 'llamacpp');
  assert(!cpu.includes('--flash-attn'), 'CPU-only: no --flash-attn at all, not even auto');
  assert(!cpu.includes('GGML_CUDA_ENABLE_UNIFIED_MEMORY'), 'CPU-only: no CUDA unified-memory env');
  assert(!cpu.includes('CUDA_VISIBLE_DEVICES'), 'CPU-only: no CUDA_VISIBLE_DEVICES');
  assert(cpu.includes('-ngl 0'), 'CPU-only: the layer count really is zero');

  const viaMode = cmd({ llama_mode: 'cpu', flash_attn: true, unified_mem: true, _gguf_path: '/m.gguf' }, 'm', 'llamacpp');
  assert(!viaMode.includes('--flash-attn'), 'the CPU mode reaches the same gate as a typed ngl of 0');

  const gpu = cmd({ ngl: '99', flash_attn: true, unified_mem: true, _gguf_path: '/m.gguf' }, 'm', 'llamacpp');
  assert(gpu.includes('--flash-attn on'), 'with layers on the GPU, flash-attn is asked for');
  assert(gpu.includes('GGML_CUDA_ENABLE_UNIFIED_MEMORY=1'), 'and unified memory is set');

  const noFlash = cmd({ ngl: '99', _gguf_path: '/m.gguf' }, 'm', 'llamacpp');
  assert(noFlash.includes('--flash-attn auto'), 'unasked, flash-attn is left on auto');
}

// ── Windows runs python, not python3 ──
{
  const win = cmd({ port: '8100' }, 'stabilityai/x', 'diffusers', { platform: 'windows', hostPlatform: 'windows' });
  assert(/(^|\s)python scripts\/diffusion_server\.py/.test(win), 'diffusers on Windows: `python`');
  assert(!win.includes('python3 scripts/diffusion_server.py'), 'never `python3` there — it is not on PATH');
  const nix = cmd({ port: '8100' }, 'stabilityai/x', 'diffusers');
  assert(nix.includes('python3 scripts/diffusion_server.py'), 'and `python3` everywhere else');
}

// ── Windows llama.cpp: native server locally, the python module remotely ──
{
  const local = cmd({ ngl: '99', _gguf_path: '/m.gguf' }, 'm', 'llamacpp', { platform: 'windows', hostPlatform: 'windows' });
  assert(local.includes('llama-server --model'), 'local Windows: the native llama-server binary');
  assert(!local.includes('llama_cpp.server'), 'not the python module, which needs a build it will not have');

  const remote = cmd({ ngl: '99', _gguf_path: '/m.gguf' }, 'm', 'llamacpp', { platform: 'windows', hostPlatform: 'linux', remoteHost: 'box' });
  assert(remote.includes('-m llama_cpp.server'), 'a remote Windows target: the python module');
  assert(remote.includes('python -m'), 'and `python` there too');
}

// ── An empty swap-space is no flag, not an empty one ──
{
  const blank = cmd({ port: '8000' }, 'm', 'vllm');
  assert(!blank.includes('--swap-space'), 'no swap given: the flag is absent');
  for (const off of ['0', 'off', 'none', 'false', 'OFF']) {
    assert(!cmd({ swap: off }, 'm', 'vllm').includes('--swap-space'), `swap "${off}" is off, not a value`);
  }
  assert(cmd({ swap: '4' }, 'm', 'vllm').includes('--swap-space 4'), 'a real number is passed through');
}

// ── Gemma 4 thinking needs its chat template ──
{
  const g = cmd({ port: '8000' }, 'google/gemma-4-27b-it', 'vllm');
  assert(g.includes('--chat-template'), 'gemma 4: a chat template is supplied');
  assert(g.includes('<|think|>'), 'and it carries the thinking control token');
  assert(g.includes('<|channel>thought'), 'with the generation prompt opening the thought channel');
  const other = cmd({ port: '8000' }, 'Qwen/Qwen3-8B', 'vllm');
  assert(!other.includes('--chat-template'), 'and nothing else gets one it did not ask for');
}

// ── Vision uses the projector that was scanned, not one found at runtime ──
{
  const v = cmd({ ngl: '99', vision: true, _gguf_path: '/m.gguf', _mmproj_path: '/proj.gguf' }, 'm', 'llamacpp');
  assert(v.includes('--mmproj "/proj.gguf"'), 'the scanned projector path is passed');
  assert(!/--mmproj\s+"\$\(/.test(v), 'not a shell substitution resolved on the far side');
  const noV = cmd({ ngl: '99', _gguf_path: '/m.gguf' }, 'm', 'llamacpp');
  assert(!noV.includes('--mmproj'), 'and no projector when vision is off');
}

// ── The backend choices a target actually supports ──
{
  const remoteWin = s.backendChoices(ctx({ platform: 'windows', remoteHost: 'box', hostPlatform: 'linux' }), true);
  assert(!remoteWin.includes('diffusers'), 'diffusers is not offered for a remote Windows server');
  const localWin = s.backendChoices(ctx({ platform: 'windows', hostPlatform: 'windows' }), true);
  assert(localWin.includes('diffusers'), 'but it is offered on this machine, Windows included');
}

// ── Quoting, because a path with a space is normal ──
{
  assert(s.shellQuote("a b") === "'a b'", 'a space is quoted');
  assert(s.shellQuote("it's") === "'it'\\''s'", 'and an apostrophe survives the quoting');
  assert(s.psQuote("a 'b'") === "'a ''b'''", 'PowerShell doubles its own quote');
}

// ── Ports ──
{
  assert(s.portOf('llama-server --port 8081 --host 0.0.0.0') === '8081', 'the port is read back out of a command');
  assert(s.nextFreePort(['8000', 8001]) === '8002', 'the next free port skips what is taken');
  assert(s.replaceFlag('x --port 8000 --y 1', '--port', '9000').includes('--port 9000'), 'a flag can be replaced in place');
  assert(!s.removeFlag('x --enforce-eager --y 1', '--enforce-eager').includes('--enforce-eager'), 'and removed');
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
