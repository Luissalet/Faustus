/**
 * The Cookbook's launch logic, ported from the previous interface's
 * cookbook.js: which engine a model wants, which parsers and MoE tricks a
 * model family needs, and the exact command a serve launches with. Pure:
 * every fact about the target (platform, venv, detected GPU backend) comes
 * in through `ServeCtx`, never from a global.
 */

export type Backend = 'vllm' | 'sglang' | 'llamacpp' | 'ollama' | 'mlx' | 'mlx_image' | 'diffusers';

export const BACKEND_LABEL: Record<Backend, string> = {
  vllm: 'vLLM',
  sglang: 'SGLang',
  llamacpp: 'llama.cpp',
  ollama: 'Ollama',
  mlx: 'MLX',
  mlx_image: 'MLX Image',
  diffusers: 'Diffusers',
};

/** Which pip package each engine needs (the Dependencies row it maps to). */
export const BACKEND_PKG: Record<Backend, string> = { vllm: 'vllm', sglang: 'sglang', llamacpp: 'llama_cpp', mlx: 'mlx_lm', mlx_image: 'mflux', diffusers: 'diffusers', ollama: '' };

export interface ServeCtx {
  /** The target's platform: 'windows' | 'linux' | 'termux' | 'darwin' | ''. */
  platform: string;
  /** '' means this machine. */
  remoteHost: string;
  env: 'none' | 'venv' | 'conda' | string;
  envPath: string;
  hfToken?: string;
  /** Pinned GPU ids, e.g. "0,1". */
  gpus?: string;
  /** The hardware scan's backend for THIS target ('cuda', 'rocm', 'metal'…) or '' when unknown. */
  hwBackend: string;
  /** The local host's platform, for a serve that targets this machine. */
  hostPlatform: string;
}

export type ServeFields = Record<string, string | boolean | undefined | null>;

/** The scanned model rows (`/api/model/cached`, `/api/hwfit/models`) as the builders see them. */
export interface ModelLike {
  repo_id?: string;
  name?: string;
  id?: string;
  path?: string;
  quant?: string;
  backend?: string;
  endpoint_kind?: string;
  provider?: string;
  source?: string;
  is_ollama?: boolean;
  is_gguf?: boolean;
  is_image_gen?: boolean;
  is_diffusion?: boolean;
  is_video?: boolean;
  is_adapter?: boolean;
  mlx_only?: boolean;
  gguf_files?: { rel_path?: string; size_bytes?: number }[];
  gguf_sources?: { repo?: string; file?: string; provider?: string }[];
  quant_repo?: string | null;
  required_gb?: number;
  _tag?: string;
}

const isWindows = (ctx: ServeCtx) => ctx.platform === 'windows' || (!ctx.remoteHost && ctx.hostPlatform === 'windows');
const isMetal = (ctx: ServeCtx) => ['metal', 'mps', 'apple'].includes(ctx.hwBackend.toLowerCase());

export function shellQuote(value: unknown): string {
  return "'" + String(value ?? '').replace(/'/g, "'\\''") + "'";
}
export function psQuote(value: unknown): string {
  return "'" + String(value ?? '').replace(/'/g, "''") + "'";
}
const listField = (v: unknown): string[] =>
  String(v || '')
    .split(/[\n,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
const numField = (v: unknown): string => {
  const s = String(v || '').trim();
  return /^-?\d+(?:\.\d+)?$/.test(s) ? s : '';
};

/* ── model families ── */

const isStepFun = (n: string) => n.includes('stepfun') || n.includes('step-3') || n.includes('step3') || n.includes('step_3');
const isDeepSeekV4 = (n: string) => n.includes('deepseek') && /\bv[-_]?4\b/.test(n);
const isGemma4 = (n: string) => n.includes('gemma-4') || n.includes('gemma4');

const GEMMA4_THINKING_CHAT_TEMPLATE = `{% for message in messages %}{% if message['role'] == 'system' %}<|turn>system\n<|think|>{{ message['content'] }}<turn|>\n{% elif message['role'] == 'user' %}<|turn>user\n{{ message['content'] }}<turn|>\n{% elif message['role'] == 'assistant' %}<|turn>model\n{{ message['content'] }}<turn|>\n{% endif %}{% endfor %}{% if add_generation_prompt %}<|turn>model\n<|channel>thought{% endif %}`;

/** The vLLM `--reasoning-parser` a model family needs, or null. */
export function detectReasoningParser(modelName: string): string | null {
  const n = (modelName || '').toLowerCase();
  if (isStepFun(n)) return 'step3p5';
  if (n.includes('minimax') && /\bm3\b/.test(n)) return 'minimax_m3';
  if (n.includes('minimax') && /\bm2(?:\.\d)?\b/.test(n)) return 'minimax_m2';
  if (isDeepSeekV4(n)) return 'deepseek-v4';
  if (n.includes('deepseek') && (n.includes('r1') || n.includes('thinking'))) return 'deepseek_r1';
  if (n.includes('qwen3') && !n.includes('coder') && !n.includes('instruct')) return 'qwen3';
  if (n.includes('glm-4') || n.includes('glm-5')) return 'glm45';
  if (n.includes('gpt-oss')) return 'gpt_oss';
  if (n.includes('hunyuan') && n.includes('a13b')) return 'hunyuan_a13b';
  if (n.includes('granite') && (n.includes('reason') || n.includes('think'))) return 'granite';
  if (n.includes('internlm')) return 'internlm';
  return null;
}

/** The vLLM `--tool-call-parser` for a model family. */
export function detectToolParser(modelName: string): string {
  const n = (modelName || '').toLowerCase();
  if (isStepFun(n)) return 'step3p5';
  if (n.includes('qwen3') && n.includes('coder')) return 'qwen3_coder';
  if (n.includes('qwen3')) return 'qwen3_xml';
  if (n.includes('qwen')) return 'hermes';
  if (n.includes('llama-4') || n.includes('llama4')) return 'llama4_json';
  if (n.includes('llama') || n.includes('nemotron')) return 'llama3_json';
  if (n.includes('mistral') || n.includes('mixtral')) return 'mistral';
  if (isDeepSeekV4(n)) return 'deepseekv4';
  if (n.includes('deepseek')) return 'deepseek_v3';
  if (n.includes('minimax') && /\bm3\b/.test(n)) return 'minimax_m3';
  if (n.includes('minimax') && n.includes('m2')) return 'minimax_m2';
  if (n.includes('minimax')) return 'minimax';
  if (n.includes('gemma')) return 'pythonic';
  if (n.includes('glm-4')) return 'glm45';
  if (n.includes('internlm')) return 'internlm';
  if (n.includes('granite')) return 'granite';
  return 'hermes';
}

export interface ModelOptimizations {
  envVars: string[];
  flags: string[];
  tips: string[];
  kvCacheDtype?: string;
  spec?: { method: string; tokens: number };
}

/** MoE env vars, expert parallel, reasoning parser and speculative decoding per family. */
export function detectModelOptimizations(modelName: string): ModelOptimizations {
  const n = (modelName || '').toLowerCase();
  const opts: ModelOptimizations = { envVars: [], flags: [], tips: [] };
  if (isStepFun(n)) {
    opts.flags.push('--enable-expert-parallel');
    opts.tips.push('StepFun Step-3 MoE: expert parallel', 'StepFun parser: step3p5 for native tool calls and reasoning tags');
  } else if (n.includes('qwen3.5') || (n.includes('qwen3-') && (n.includes('a10b') || n.includes('a22b') || n.includes('a3b')))) {
    opts.envVars.push('VLLM_USE_DEEP_GEMM=0', 'VLLM_USE_FLASHINFER_MOE_FP16=1', 'VLLM_USE_FLASHINFER_SAMPLER=0', 'OMP_NUM_THREADS=4');
    opts.flags.push('--enable-expert-parallel');
    opts.tips.push('MoE optimizations: expert parallel + flashinfer MoE kernels');
  } else if (n.includes('qwen3') && (n.includes('a10b') || n.includes('a22b') || n.includes('a3b'))) {
    opts.envVars.push('VLLM_USE_DEEP_GEMM=0', 'VLLM_USE_FLASHINFER_MOE_FP16=1');
    opts.flags.push('--enable-expert-parallel');
    opts.tips.push('MoE optimizations: expert parallel');
  } else if (n.includes('deepseek') && /\b(v[3-9]|v\d{2,}|r[1-9])\b/.test(n)) {
    opts.flags.push('--enable-expert-parallel');
    opts.tips.push('MoE expert parallel for DeepSeek');
    opts.kvCacheDtype = 'fp8';
    opts.tips.push('fp8 KV cache required — bf16 OOMs at usable context');
  } else if (n.includes('minimax')) {
    opts.flags.push('--enable-expert-parallel');
    opts.tips.push('MoE expert parallel for MiniMax');
    if (/\bm3\b/.test(n)) {
      opts.kvCacheDtype = 'fp8';
      opts.tips.push('MiniMax M3 defaults: fp8 KV cache, block-size 128, TRITON attention');
    }
  }
  const rp = detectReasoningParser(modelName);
  if (rp) {
    opts.flags.push(`--reasoning-parser ${rp}`);
    opts.tips.push(`Reasoning parser (${rp}): splits <think> tokens into a separate channel`);
  }
  let spec: ModelOptimizations['spec'] | null = null;
  if (n.includes('qwen3-next') || (n.includes('qwen3.5') && (n.includes('a10b') || n.includes('a22b')))) spec = { method: 'qwen3_next_mtp', tokens: 2 };
  else if ((n.includes('deepseek') && /\b(v[3-9]|v\d{2,}|r[1-9])\b/.test(n)) || n.includes('kimi-k2') || n.includes('kimi_k2') || n.includes('glm-4.5') || n.includes('glm4.5') || n.includes('minimax-m1') || n.includes('minimax_m1')) spec = { method: 'mtp', tokens: 3 };
  if (spec) {
    opts.spec = spec;
    opts.flags.push(`--speculative-config '{"method":"${spec.method}","num_speculative_tokens":${spec.tokens}}'`);
    opts.tips.push(`Speculative decoding (${spec.method}, ${spec.tokens} tokens): ~1.5-2x faster generation`);
  }
  return opts;
}

/** Which engine a scanned model wants on this target. */
export function detectBackend(model: ModelLike, ctx: ServeCtx): { backend: Backend; label: string } {
  const name = String(model.repo_id || model.name || model.id || '').trim();
  const meta = `${model.backend || ''} ${model.endpoint_kind || ''} ${model.provider || ''} ${model.source || ''}`.toLowerCase();
  const looksOllamaTag = /^[A-Za-z0-9][A-Za-z0-9._-]*(?::[A-Za-z0-9][A-Za-z0-9._-]*)$/.test(name);
  if (model.backend === 'ollama' || model.is_ollama || meta.includes('ollama') || looksOllamaTag) return { backend: 'ollama', label: 'Ollama' };
  const q = (model.quant || '').toUpperCase();
  const sys = ctx.hwBackend.toLowerCase();
  const nm = `${model.repo_id || ''} ${model.path || ''} ${model.name || ''}`.toLowerCase();
  const isImage = Boolean(model.is_image_gen || model.is_diffusion || model._tag === 'image');
  if (isImage) {
    if (/\bmlx\b|mlx-|_mlx|mlx-community\//i.test(nm) || q.startsWith('MLX') || model.mlx_only) return { backend: 'mlx_image', label: 'MLX Image' };
    return { backend: 'diffusers', label: 'Diffusers' };
  }
  if (/\bmlx\b|mlx-|_mlx/i.test(nm) || q.startsWith('MLX')) return { backend: 'mlx', label: 'MLX' };
  const isAwqLike = /^AWQ|^GPTQ|^NVFP4/.test(q) || ['FP8', 'FP4', 'MXFP4', 'NF4', 'INT4', 'INT8', 'W4A16', 'W8A8', 'W8A16'].includes(q) || /\b(awq|gptq|fp8|fp4|nvfp4|mxfp4|nf4|int4|int8|w4a16|w8a8|w8a16)\b/i.test(nm);
  const hasGguf = Array.isArray(model.gguf_files) && model.gguf_files.some((f) => f && typeof f.rel_path === 'string' && /\.gguf$/i.test(f.rel_path));
  const isGgufLike = model.is_gguf || hasGguf || /^Q[2-8]/.test(q) || /^IQ/.test(q) || q === 'GGUF' || nm.includes('gguf');
  if (isAwqLike) return { backend: 'vllm', label: 'vLLM' };
  if (isGgufLike) return { backend: 'llamacpp', label: 'llama.cpp' };
  if (isWindows(ctx)) return { backend: 'llamacpp', label: 'llama.cpp' };
  if (isMetal(ctx)) return { backend: 'mlx', label: 'MLX' };
  if (sys === 'rocm') return { backend: 'sglang', label: 'SGLang' };
  return { backend: 'vllm', label: 'vLLM' };
}

/** Which engines this target can run at all (the picker in the serve form). */
/**
 * Diffusers does not serve on a Windows box reached over SSH: the launcher
 * script assumes a POSIX shell on the far side. On THIS machine, Windows
 * included, it is fine. Offering it for a remote Windows target produces a
 * command that cannot run, which is worse than not offering it.
 */
export function remoteWindowsDiffusers(ctx: ServeCtx): boolean {
  return Boolean(ctx.remoteHost) && ctx.platform.toLowerCase() === 'windows';
}

export function backendChoices(ctx: ServeCtx, image = false): Backend[] {
  if (image) {
    if (remoteWindowsDiffusers(ctx)) return ['llamacpp'];
    return isMetal(ctx) ? ['mlx_image', 'diffusers'] : ['diffusers'];
  }
  if (isWindows(ctx)) return ['llamacpp', 'ollama'];
  if (isMetal(ctx)) return ['mlx', 'llamacpp', 'ollama'];
  return ['vllm', 'sglang', 'llamacpp', 'ollama'];
}

/* ── env prefix ── */

function gpuEnvVarName(ctx: ServeCtx): string {
  const sb = ctx.hwBackend.toLowerCase();
  if (sb === 'cuda') return 'CUDA_VISIBLE_DEVICES';
  if (sb === 'rocm') return 'HIP_VISIBLE_DEVICES';
  return '';
}

function gpuEnvPrefix(ctx: ServeCtx, gpuId: string, windows = false): string {
  const id = String(gpuId || '').trim();
  if (!id) return '';
  const name = gpuEnvVarName(ctx);
  if (!name) return '';
  return windows ? `$env:${name}="${id}"; ` : `${name}=${id} `;
}

/** The `env_prefix` the serve/download routes prepend (venv/conda activation, token, GPU pin). */
export function buildEnvPrefix(ctx: ServeCtx): string {
  if (isWindows(ctx)) {
    const parts: string[] = [];
    if (ctx.env === 'venv' && ctx.envPath) parts.push('& ' + psQuote(ctx.envPath.endsWith('\\Scripts\\Activate.ps1') ? ctx.envPath : ctx.envPath + '\\Scripts\\Activate.ps1'));
    else if (ctx.env === 'conda' && ctx.envPath) parts.push('conda activate ' + psQuote(ctx.envPath));
    if (ctx.hfToken) parts.push('$env:HF_TOKEN=' + psQuote(ctx.hfToken));
    const g = gpuEnvVarName(ctx);
    if (ctx.gpus && g) parts.push(`$env:${g}=` + psQuote(ctx.gpus));
    return parts.length ? parts.join('; ') + ';' : '';
  }
  const parts: string[] = [];
  if (ctx.env === 'venv' && ctx.envPath) {
    const p = ctx.envPath;
    parts.push('source ' + shellQuote(p.endsWith('/bin/activate') ? p : p + '/bin/activate'));
  } else if (ctx.env === 'conda' && ctx.envPath) parts.push('eval "$(conda shell.bash hook)" && conda activate ' + shellQuote(ctx.envPath));
  const vars: string[] = [];
  if (ctx.hfToken) vars.push('export HF_TOKEN=' + shellQuote(ctx.hfToken));
  const g = gpuEnvVarName(ctx);
  if (ctx.gpus && g) vars.push(`export ${g}=` + shellQuote(ctx.gpus));
  if (vars.length) parts.push(vars.join(' && '));
  return parts.length ? parts.join(' && ') + ' &&' : '';
}

/** The activation-only prefix the serve route wants (`env_prefix`); no token, no GPU pin. */
export function activationPrefix(ctx: ServeCtx): string {
  if (isWindows(ctx)) {
    if (ctx.env === 'venv' && ctx.envPath) return '& ' + psQuote(ctx.envPath.endsWith('\\Scripts\\Activate.ps1') ? ctx.envPath : ctx.envPath + '\\Scripts\\Activate.ps1');
    if (ctx.env === 'conda' && ctx.envPath) return 'conda activate ' + ctx.envPath;
    return '';
  }
  if (ctx.env === 'venv' && ctx.envPath) {
    const p = venvRoot(ctx.envPath);
    return 'source ' + (p.endsWith('/bin/activate') ? p : p + '/bin/activate');
  }
  if (ctx.env === 'conda' && ctx.envPath) return 'eval "$(conda shell.bash hook)" && conda activate ' + ctx.envPath;
  return '';
}

export function venvRoot(path: string): string {
  let p = (path || '').trim().replace(/\/+$/, '');
  if (!p) return '';
  p = p.replace(/\/bin\/(?:activate|python(?:3(?:\.\d+)?)?|vllm|pip(?:3)?)$/i, '');
  return p;
}

function venvLooksWrong(path: string, platform: string): boolean {
  const p = (path || '').trim();
  const plat = (platform || '').toLowerCase();
  if (!p || !plat) return false;
  if ((plat === 'darwin' || plat === 'macos') && /^\/(?:home|usr\/local\/cuda|opt\/conda)\//.test(p)) return true;
  if ((plat === 'linux' || plat === 'termux') && /^\/(?:Users|opt\/homebrew)\//.test(p)) return true;
  return false;
}

const envHasKey = (envText: string, key: string) => envText.split(/\s+/).some((part) => part.startsWith(`${key}=`));

/** The venv's python3 by absolute path (SSH sessions put user-site first otherwise). */
export function venvPython(ctx: ServeCtx): string {
  return ctx.env === 'venv' && ctx.envPath ? `${venvRoot(ctx.envPath)}/bin/python3` : 'python3';
}

/* ── the command ── */

/**
 * The serve command for a backend. `f` is the form (strings and booleans
 * keyed like the previous interface's panel fields); the target facts come
 * from `ctx`.
 */
export function buildServeCmd(f: ServeFields, modelName: string, backend: Backend, ctx: ServeCtx): string {
  const s = (k: string) => String(f[k] ?? '').trim();
  const b = (k: string) => Boolean(f[k]);
  let formVenv = s('venv');
  if (venvLooksWrong(formVenv, ctx.platform)) formVenv = '';
  const activeVenv = venvRoot(formVenv || (ctx.env === 'venv' ? ctx.envPath : ''));
  const venvBin = activeVenv ? `${activeVenv}/bin/` : '';
  const vllmBin = venvBin ? `${venvBin}vllm` : 'vllm';
  const py3 = venvBin ? `${venvBin}python3` : 'python3';
  const win = isWindows(ctx);
  let cmd = '';

  if (backend === 'vllm') {
    cmd += gpuEnvPrefix(ctx, s('gpus') || s('gpu_id'));
    if (b('moe_env')) {
      const o = detectModelOptimizations(modelName);
      cmd += o.envVars.length ? o.envVars.join(' ') + ' ' : 'VLLM_USE_DEEP_GEMM=0 VLLM_USE_FLASHINFER_MOE_FP16=1 OMP_NUM_THREADS=4 ';
    }
    const extraEnv = s('extra_env').replace(/\s+/g, ' ').trim();
    if (extraEnv) cmd += extraEnv + ' ';
    cmd += `${vllmBin} serve ${modelName} --host 0.0.0.0 --port ${s('port') || '8000'}`;
    if (s('served_model_name')) cmd += ` --served-model-name ${s('served_model_name')}`;
    if (s('vllm_attn_backend')) cmd += ` --attention-backend ${s('vllm_attn_backend')}`;
    if (isGemma4(modelName.toLowerCase())) cmd += ` --chat-template ${shellQuote(GEMMA4_THINKING_CHAT_TEMPLATE)}`;
    cmd += ` --tensor-parallel-size ${s('tp') || '1'}`;
    if (/^\d+$/.test(s('vllm_block_size'))) cmd += ` --block-size ${s('vllm_block_size')}`;
    cmd += ` --max-model-len ${s('ctx') || '8192'}`;
    cmd += ` --gpu-memory-utilization ${s('gpu_mem') || '0.90'}`;
    const swap = s('swap').toLowerCase();
    if (swap && !['0', 'off', 'none', 'false'].includes(swap)) cmd += ` --swap-space ${swap}`;
    cmd += ` --dtype ${s('dtype') || 'auto'}`;
    if (s('vllm_kv_cache_dtype') === 'fp8') cmd += ' --kv-cache-dtype fp8';
    if (s('max_seqs')) cmd += ` --max-num-seqs ${s('max_seqs')}`;
    const loras = listField(f.vllm_lora_modules);
    if (loras.length) cmd += ` --enable-lora --lora-modules ${loras.map(shellQuote).join(' ')}`;
    if (b('enforce_eager')) cmd += ' --enforce-eager';
    if (b('trust_remote')) cmd += ' --trust-remote-code';
    if (b('prefix_cache')) cmd += ' --enable-prefix-caching';
    if (b('auto_tool')) cmd += ` --enable-auto-tool-choice --tool-call-parser ${detectToolParser(modelName)}`;
    if (b('expert_parallel')) cmd += ' --enable-expert-parallel';
    if (b('language_model_only')) cmd += ' --language-model-only';
    if (b('disable_custom_all_reduce')) cmd += ' --disable-custom-all-reduce';
    if (f.reasoning_parser) {
      const rp = typeof f.reasoning_parser === 'string' && f.reasoning_parser !== 'true' ? f.reasoning_parser : detectReasoningParser(modelName) || '';
      if (rp) cmd += ` --reasoning-parser ${rp}`;
    }
    if (b('speculative')) {
      const method = s('spec_method') || 'mtp';
      const toks = parseInt(s('spec_tokens'), 10);
      cmd += ` --speculative-config '{"method":"${method}","num_speculative_tokens":${Number.isFinite(toks) && toks > 0 ? toks : 3}}'`;
    }
  } else if (backend === 'sglang') {
    cmd += gpuEnvPrefix(ctx, s('gpus') || s('gpu_id'));
    const dsv4 = isDeepSeekV4(modelName.toLowerCase());
    let extraEnv = s('extra_env').replace(/\s+/g, ' ').trim();
    if (dsv4 && !envHasKey(extraEnv, 'SGLANG_DSV4_COMPRESS_STATE_DTYPE')) extraEnv = `SGLANG_DSV4_COMPRESS_STATE_DTYPE=bf16 ${extraEnv}`.trim();
    if (extraEnv) cmd += extraEnv + ' ';
    cmd += `${py3} -m sglang.launch_server --model-path ${modelName} --host 0.0.0.0 --port ${s('port') || '30000'}`;
    if (isGemma4(modelName.toLowerCase())) cmd += ` --chat-template ${shellQuote(GEMMA4_THINKING_CHAT_TEMPLATE)}`;
    if (s('tp') && s('tp') !== '1') cmd += ` --tp ${s('tp')}`;
    if (s('ctx')) cmd += ` --context-length ${s('ctx')}`;
    const mem = dsv4 && (!s('gpu_mem') || s('gpu_mem') === '0.90') ? '0.80' : s('gpu_mem');
    if (mem && mem !== '0.90') cmd += ` --mem-fraction-static ${mem}`;
    if (s('dtype') && s('dtype') !== 'auto') cmd += ` --dtype ${s('dtype')}`;
    if (s('max_seqs')) cmd += ` --max-running-requests ${s('max_seqs')}`;
    if (b('trust_remote')) cmd += ' --trust-remote-code';
    if (b('auto_tool')) cmd += ` --enable-auto-tool-choice --tool-call-parser ${detectToolParser(modelName)}`;
    if (b('expert_parallel')) cmd += ' --enable-expert-parallel';
    if (f.reasoning_parser) {
      const rp = typeof f.reasoning_parser === 'string' && f.reasoning_parser !== 'true' ? f.reasoning_parser : detectReasoningParser(modelName) || '';
      if (rp) cmd += ` --reasoning-parser ${rp}`;
    }
    if (!b('prefix_cache')) cmd += ' --disable-radix-cache';
    if (b('enforce_eager')) cmd += ' --disable-cuda-graph';
    const decode = s('sglang_decode_graph');
    if (!b('enforce_eager') && decode === 'disabled') cmd += ' --cuda-graph-backend-decode disabled';
    else if (!b('enforce_eager') && decode === 'bs16') cmd += ' --cuda-graph-max-bs-decode 16';
    else if (!b('enforce_eager') && dsv4 && !/\s--cuda-graph-max-bs-decode\b/.test(cmd) && !/\s--cuda-graph-backend-decode\b/.test(cmd)) cmd += ' --cuda-graph-backend-decode disabled';
  } else if (backend === 'llamacpp') {
    const ggufPath = s('_gguf_path') || 'model.gguf';
    const gpuId = s('gpus') || s('gpu_id');
    const localWindows = win && !ctx.remoteHost;
    const py = win ? 'python' : 'python3';
    const mode = s('llama_mode').toLowerCase();
    let ngl = s('ngl');
    let unified = b('unified_mem');
    if (mode === 'unified') unified = true;
    if (mode === 'cpu') ngl = '0';
    else if (['gpu', 'unified'].includes(mode) && (!ngl || ngl === '0')) ngl = '99';
    const cpuOnly = ngl.trim() === '0';
    const cudaTarget = ctx.hwBackend.toLowerCase() === 'cuda';
    let lcPrefix = '';
    if (unified && !cpuOnly && (!win || localWindows) && cudaTarget) lcPrefix += 'GGML_CUDA_ENABLE_UNIFIED_MEMORY=1 ';
    if ((!win || localWindows) && !cpuOnly) lcPrefix += gpuEnvPrefix(ctx, gpuId);
    if (unified && !cpuOnly && win && !localWindows && cudaTarget) cmd += '$env:GGML_CUDA_ENABLE_UNIFIED_MEMORY="1"; ';
    if (win && !localWindows && !cpuOnly) cmd += gpuEnvPrefix(ctx, gpuId, true);
    const needsPrelude = /^\$\(\{\s*find\s/.test(ggufPath);
    const modelArg = needsPrelude ? '"$MODEL_FILE"' : `"${ggufPath}"`;
    const num = (v: string) => (/^\d+$/.test(v) ? v : '');
    const csv = (v: string) => {
      const x = v.replace(/\s+/g, '');
      return /^\d+(?:\.\d+)?(?:,\d+(?:\.\d+)?)*$/.test(x) ? x : '';
    };
    let lc = '';
    let lcp = '';
    const ncm = s('n_cpu_moe');
    if (ncm !== '' && Number(ncm) > 0) {
      lc += ` --n-cpu-moe ${ncm}`;
      lcp += ` --n_cpu_moe ${ncm}`;
    }
    if (b('flash_attn') && !cpuOnly) {
      lc += ' --flash-attn on';
      lcp += ' --flash_attn true';
    } else if (!cpuOnly) lc += ' --flash-attn auto';
    const kv = s('cache_type');
    if (kv) {
      lc += ` --cache-type-k ${kv} --cache-type-v ${kv}`;
      lcp += ` --type_k ${kv} --type_v ${kv}`;
    }
    if (['on', 'off'].includes(s('llama_fit'))) lc += ` --fit ${s('llama_fit')}`;
    if (b('llama_no_mmap')) lc += ' --no-mmap';
    if (b('llama_no_warmup')) lc += ' --no-warmup';
    if (['none', 'layer', 'row', 'tensor'].includes(s('llama_split_mode'))) lc += ` --split-mode ${s('llama_split_mode')}`;
    if (csv(s('llama_tensor_split'))) lc += ` --tensor-split ${csv(s('llama_tensor_split'))}`;
    if (num(s('llama_main_gpu'))) lc += ` --main-gpu ${s('llama_main_gpu')}`;
    if (num(s('llama_parallel'))) lc += ` --parallel ${s('llama_parallel')}`;
    if (num(s('llama_batch_size'))) lc += ` --batch-size ${s('llama_batch_size')}`;
    if (num(s('llama_ubatch_size'))) lc += ` --ubatch-size ${s('llama_ubatch_size')}`;
    if (b('llama_speculative_mtp')) {
      const n = parseInt(s('llama_spec_tokens'), 10);
      lc += ` --spec-type draft-mtp --spec-draft-n-max ${Number.isFinite(n) && n > 0 ? n : 3}`;
    }
    if (b('vision') && s('_mmproj_path')) {
      lc += ` --mmproj "${s('_mmproj_path')}" --image-max-tokens 1024`;
      lcp += ` --clip_model_path "${s('_mmproj_path')}"`;
    }
    const port = s('port') || '8080';
    const ctxLen = s('ctx') || '8192';
    const server = `${lcPrefix}llama-server --model ${modelArg} --host 0.0.0.0 --port ${port} -ngl ${ngl || '99'} -c ${ctxLen}${lc}`;
    const pyServer = `${lcPrefix}${py} -m llama_cpp.server --model ${modelArg} --host 0.0.0.0 --port ${port} --n_gpu_layers ${ngl || '99'} --n_ctx ${ctxLen}${lcp}`;
    cmd += localWindows ? server : win ? pyServer : server;
    if (needsPrelude) cmd = `MODEL_FILE=${ggufPath} && { [ -n "$MODEL_FILE" ] && [ -f "$MODEL_FILE" ]; } || { echo "ERROR: No GGUF found on this host"; exit 1; } && ${cmd}`;
  } else if (backend === 'ollama') {
    const port = s('port') || '11434';
    if (modelName.includes('/') && (s('gguf_file') || /-GGUF$/i.test(modelName))) {
      const name = (modelName.split('/').pop() || modelName)
        .replace(/-GGUF$/i, '')
        .toLowerCase()
        .replace(/[^a-z0-9._:-]+/g, '-')
        .replace(/^-+|-+$/g, '');
      const file = s('gguf_file').split('/').pop() || '';
      cmd = `docker exec ollama-test ollama-import ${modelName} ${name} ${s('ctx') || '8192'}${file ? ' ' + file : ''}`;
    } else if (!modelName.includes('/') && modelName) {
      // An already-pulled tag: the runtime is Ollama itself; the backend
      // registers its endpoint once the tag answers.
      cmd = ctx.remoteHost ? `docker exec ollama-rocm ollama show ${modelName}` : `ollama show ${modelName}`;
    } else {
      const bind = ctx.remoteHost ? '0.0.0.0' : '127.0.0.1';
      cmd = `${port !== '11434' ? `OLLAMA_HOST=${bind}:${port} ` : ''}ollama serve`;
    }
  } else if (backend === 'diffusers') {
    cmd += gpuEnvPrefix(ctx, s('gpus'));
    const dpy = win ? 'python' : py3;
    const host = s('host');
    cmd += `${dpy} scripts/diffusion_server.py --model ${modelName} --host ${host ? '0.0.0.0' : '127.0.0.1'} --port ${s('port') || '8100'}`;
    if (host) {
      const allowed = host.split('@').pop()!.split(':')[0].trim();
      if (allowed) cmd += ` --allowed-host ${allowed}`;
    }
    if (s('diff_dtype') && s('diff_dtype') !== 'bfloat16') cmd += ` --dtype ${s('diff_dtype')}`;
    if (s('diff_device_map') && s('diff_device_map') !== 'balanced') cmd += ` --device-map ${s('diff_device_map')}`;
    if (s('diff_steps')) cmd += ` --steps ${s('diff_steps')}`;
    if (s('diff_guidance_scale')) cmd += ` --guidance-scale ${numField(s('diff_guidance_scale')) || s('diff_guidance_scale')}`;
    if (s('diff_negative_prompt')) cmd += ` --negative-prompt ${shellQuote(s('diff_negative_prompt'))}`;
    if (s('diff_width')) cmd += ` --width ${s('diff_width')}`;
    if (s('diff_height')) cmd += ` --height ${s('diff_height')}`;
    const loras = listField(f.diff_lora);
    if (loras.length) cmd += ` --lora ${shellQuote(loras.join(','))}`;
    if (numField(s('diff_lora_scale'))) cmd += ` --lora-scale ${numField(s('diff_lora_scale'))}`;
    if (b('diff_offload')) cmd += ' --cpu-offload';
    if (b('diff_attention_slicing')) cmd += ' --attention-slicing';
    if (b('diff_vae_slicing')) cmd += ' --vae-slicing';
    if (s('diff_harmonize_gpu')) cmd += ` --harmonize-gpu ${s('diff_harmonize_gpu')}`;
  } else if (backend === 'mlx_image') {
    const mpy = win ? 'python' : py3;
    cmd += `${mpy} scripts/mlx_image_server.py --model ${shellQuote(modelName)} --host ${s('host') ? '0.0.0.0' : '127.0.0.1'} --port ${s('port') || '8100'}`;
    if (s('diff_steps')) cmd += ` --steps ${s('diff_steps')}`;
    if (s('diff_width')) cmd += ` --width ${s('diff_width')}`;
    if (s('diff_height')) cmd += ` --height ${s('diff_height')}`;
    if (s('mlx_base_model')) cmd += ` --base-model ${shellQuote(s('mlx_base_model'))}`;
    if (s('mlx_lora_style')) cmd += ` --lora-style ${shellQuote(s('mlx_lora_style'))}`;
    const paths = listField(f.mlx_lora_paths);
    if (paths.length) cmd += ` --lora-paths ${paths.map(shellQuote).join(' ')}`;
    const scales = listField(f.mlx_lora_scales).filter((x) => /^-?\d+(?:\.\d+)?$/.test(x));
    if (scales.length) cmd += ` --lora-scales ${scales.map(shellQuote).join(' ')}`;
  } else if (backend === 'mlx') {
    const mpy = win ? 'python' : py3;
    cmd += `${mpy} -m mlx_lm.server --model ${shellQuote(modelName)} --host ${s('host') ? '0.0.0.0' : '127.0.0.1'} --port ${s('port') || '8080'}`;
    const max = s('ctx');
    if (/minimax|mini-max/i.test(modelName)) cmd += ` --temp 0.7 --top-p 0.9 --max-tokens ${max || '2048'}`;
    else if (/^\d+$/.test(max)) cmd += ` --max-tokens ${max}`;
  }
  return cmd;
}

/** Default port per engine (the form's placeholder). */
export const DEFAULT_PORT: Record<Backend, string> = { vllm: '8000', sglang: '30000', llamacpp: '8080', ollama: '11434', mlx: '8080', mlx_image: '8100', diffusers: '8100' };

/** Read the port out of a launch command; '' when none. */
export function portOf(cmd: string): string {
  const m = cmd.match(/--port[=\s]+(\d+)/) || cmd.match(/(?:^|\s)-p[=\s]+(\d+)/) || cmd.match(/OLLAMA_HOST=[^\s:]+:(\d+)/);
  return m ? m[1] : '';
}

/** Lowest free port ≥ start not in `used`. */
export function nextFreePort(used: Iterable<string | number>, start = 8000): string {
  const taken = new Set([...used].map((p) => parseInt(String(p), 10)));
  let port = start;
  while (taken.has(port)) port += 1;
  return String(port);
}

/** Replace `--flag value` (or add it) in a launch command — the "retry with X" fixes. */
export function replaceFlag(cmd: string, flag: string, value: string): string {
  if (!flag) return `${value}${cmd}`;
  const re = new RegExp(`(^|\\s)${flag.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?:[=\\s]+\\S+)?`);
  if (re.test(cmd)) return cmd.replace(re, `$1${flag} ${value}`.trimEnd());
  return `${cmd} ${flag} ${value}`.trim();
}

export function addFlag(cmd: string, flag: string): string {
  const head = flag.split(/\s+/)[0];
  return cmd.includes(head) ? cmd : `${cmd} ${flag}`;
}

export function removeFlag(cmd: string, flag: string): string {
  const re = new RegExp(`\\s${flag.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?:[=\\s]+\\S+)?`, 'g');
  return cmd.replace(re, '');
}

/** A GGUF file's quant, read from its name: Q4_K_M, IQ2_XXS, UD-Q3_K_XL… */
export function ggufQuant(path: string): string {
  const clean = String(path || '').replace(/\*/g, '');
  const parts = clean.split('/').filter(Boolean);
  const file = parts[parts.length - 1] || clean;
  const dir = parts.length > 1 ? parts[parts.length - 2] : '';
  const m = `${dir} ${file}`.match(/\b(?:UD-)?(?:IQ[1-8]_[A-Z0-9]+|Q[2-8]_K_[MLS]|Q[2-8]_[0-9A-Z]+|Q[2-8])\b/i);
  if (m) return m[0].toUpperCase().replace(/^UD-/, '');
  return file.replace(/\.gguf$/i, '').replace(/-\d{5}-of-\d{5}$/i, '');
}

export function bytesLabel(n: number | undefined): string {
  if (!n || !Number.isFinite(n)) return '';
  if (n >= 1073741824) return `${(n / 1073741824).toFixed(1)} GB`;
  if (n >= 1048576) return `${(n / 1048576).toFixed(0)} MB`;
  return `${Math.round(n / 1024)} KB`;
}

/* ── GGUF paths on the target ── */

function shellPathExpr(path: string): string {
  if (path === '~') return '${HOME}';
  if (path.startsWith('~/')) return '${HOME}' + shellQuote(path.slice(1));
  return shellQuote(path);
}

/** The exact GGUF file, as a shell expression the serve validator accepts. */
export function ggufFileExpr(model: { path?: string; is_local_dir?: boolean }, repo: string, relPath: string): string {
  const rel = relPath.replace(/^\/+/, '');
  if (!rel) return '';
  const base = (model.path || '').replace(/\/+$/, '');
  if (model.is_local_dir && base) return `$(printf %s ${shellPathExpr(`${base}/${repo}/${rel}`)})`;
  if (base) return `$(printf %s ${shellPathExpr(`${base}/models--${repo.replace(/\//g, '--')}/snapshots/${rel}`)})`;
  return `$(printf %s \${HOME}${shellQuote(`/.cache/huggingface/hub/models--${repo.replace(/\//g, '--')}/snapshots/${rel}`)})`;
}

/** "The first GGUF in the repo's folder" — the prelude the validator recognises. */
export function ggufFindExpr(model: { path?: string; is_local_dir?: boolean }, repo: string): string {
  const base = (model.path || '').replace(/\/+$/, '');
  const dir = model.is_local_dir && base ? shellQuote(`${base}/${repo}`) : base ? shellQuote(`${base}/models--${repo.replace(/\//g, '--')}/snapshots`) : `"$HOME/.cache/huggingface/hub/models--${repo.replace(/\//g, '--')}/snapshots"`;
  return `$({ find ${dir} -name '*-00001-of-*.gguf' 2>/dev/null | sort; find ${dir} -name '*.gguf' 2>/dev/null | sort; } | head -1)`;
}

/** The GGUF files a serve can run: shards after the first and projectors are not entry points. */
export function runnableGguf(files: { rel_path: string; size_bytes?: number }[]): { rel_path: string; size_bytes?: number }[] {
  return files.filter((f) => /\.gguf$/i.test(f.rel_path) && !/mmproj/i.test(f.rel_path) && !/-(?!00001)\d{5}-of-\d{5}\.gguf$/i.test(f.rel_path));
}

export function projectorGguf(files: { rel_path: string; size_bytes?: number }[]): { rel_path: string; size_bytes?: number }[] {
  return files.filter((f) => /mmproj/i.test(f.rel_path) && /\.gguf$/i.test(f.rel_path));
}
