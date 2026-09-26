import { getJson } from './api';
import { vramBlockedFrom, type VramBlocked } from './vramAdmission';
import { t } from '../i18n';

/**
 * Local models — the Ollama model manager (Settings → Local models), on
 * routes/local_models_routes.py, the same endpoints static/js/localModels.js
 * used: the list (installed + loaded + the card(s) + fit), pulls with an
 * EventSource per job, load/unload, delete, per-model options, the
 * placement policy, and the curated catalogue.
 */

const API = '/api/local-models';
const GIB = 1073741824;
const JSON_HEADERS = { 'Content-Type': 'application/json' };

export interface Fit {
  state?: 'fits' | 'tight' | 'over' | '';
  split?: boolean;
  note?: string;
  headroom_bytes?: number;
}
export interface Caps {
  vision?: boolean;
  tools?: boolean;
  thinking?: boolean;
  embedding?: boolean;
}
export interface InstalledModel {
  name: string;
  size: number;
  digest?: string;
  modified_at?: string;
  family?: string;
  families?: string[];
  parameter_size?: string;
  quantization?: string;
  capabilities?: Caps;
  context_length?: number;
  license?: string;
  fit?: Fit;
  loaded?: boolean;
  options?: Record<string, string | number>;
}
export interface LoadedModel {
  name: string;
  size: number;
  size_vram: number;
  size_cpu: number;
  gpu_pct: number;
  expires_at?: string | null;
  context_length?: number | null;
  placement?: 'cpu' | 'single' | 'split' | 'unknown';
  gpus?: number[];
  per_gpu?: { index: number; bytes?: number }[];
  /** Set only for a model residing on something other than this endpoint's
   * Ollama — e.g. "llama.cpp" for a self-hosted OpenAI-compatible runner
   * (src/runner_providers.py). Absent/undefined means "this Ollama", same as
   * before this field existed. */
  engine?: string;
  /** The endpoint name that serves it, shown so it is obvious which server
   * holds the memory when `engine` is set. */
  endpoint_name?: string;
  /** False when there is no real unload action for this row (llama-server
   * has no unload API) — render "served by <endpoint>" instead of a button
   * that would lie. Undefined/true means the normal Unload control applies. */
  unloadable?: boolean;
  /** True when `size`/`size_vram` came from measuring the GGUF file on disk
   * rather than a runtime VRAM reading — set alongside `engine`. */
  footprint_measured?: boolean;
}
export interface GpuProcess {
  pid: number;
  label: string;
  /** MB, or null when the driver could not report it (Windows WDDM reports
   * `[N/A]` for a compute app's `used_memory`; the process is still listed). */
  used_mb: number | null;
  /** "model": holds a model (a managed engine, llama-server, Ollama);
   * "other": any other app drawing on the card, only counted. */
  kind?: 'model' | 'other';
}
export interface GpuCard {
  index: number;
  name?: string;
  total_bytes?: number;
  used_bytes?: number;
  models_bytes?: number | null;
  other_bytes?: number | null;
  budget_bytes?: number | null;
  models?: string[];
  /** Every process nvidia-smi sees actually running compute on this card
   * right now — set even when `models` is empty, e.g. a llama.cpp engine
   * that Ollama knows nothing about. Empty (not undefined) when nvidia-smi
   * is unavailable or nothing is attributable. */
  processes?: GpuProcess[];
}
export interface Vram {
  supported: boolean;
  reason?: string;
  name?: string;
  count?: number;
  total_bytes?: number;
  held_by_runner_bytes?: number;
  other_bytes?: number;
  /** Held by llama-server engines (counted as models, not "other"). */
  engines_bytes?: number;
  reserve_bytes?: number;
  reserve_per_gpu_bytes?: number;
  budget_bytes?: number;
  clean_budget_bytes?: number;
  largest_single_budget_bytes?: number;
  gpus?: GpuCard[];
  orphans?: { pid: number; name?: string; bytes?: number; gpus?: number[] }[];
}
export interface Pull {
  id: string;
  name: string;
  endpoint_id?: string;
  active?: boolean;
  status?: 'pulling' | 'done' | 'error' | 'cancelled' | 'lost' | string;
  status_text?: string;
  percent?: number;
  completed?: number;
  total?: number;
  error?: string;
}
export interface LmEndpoint {
  id: string;
  name: string;
  same_machine?: boolean;
}
export interface LocalModelsData {
  endpoints: LmEndpoint[];
  endpoint_id: string;
  reachable: boolean;
  error?: string | null;
  models: InstalledModel[];
  loaded: LoadedModel[];
  gpus: { index: number; name?: string; total_bytes?: number }[];
  vram: Vram;
  placement_policy?: { prefer: number; order?: number[]; name?: string; mode?: string };
  disk?: { path?: string; free_bytes?: number; total_bytes?: number };
  pulls: Pull[];
  /** Models resident on a self-hosted OpenAI-compatible runner (llama.cpp's
   * llama-server) other than this endpoint's own Ollama — already merged
   * into `loaded` (with `engine`/`endpoint_name`/`unloadable: false` set) so
   * "Loaded now" shows them without extra wiring; this list is the same rows
   * on their own, for a caller that wants only the external ones. */
  external_runners?: LoadedModel[];
}
export interface DiscoverTag {
  tag: string;
  name: string;
  params?: string;
  gb?: number;
  size_bytes?: number;
  fit?: Fit;
  installed?: boolean;
}
export interface DiscoverEntry {
  name: string;
  family?: string;
  vendor?: string;
  blurb?: string;
  capabilities?: string[];
  default_tag?: string;
  tags: DiscoverTag[];
}

/**
 * The capability manifest (Settings → Local models, "Calibrate") — announced
 * (from /api/show, refreshed on every GET) vs tested (from a calibration
 * run, kept until the model is calibrated again), on routes/local_models_routes.py's
 * /api/models/{name}/capabilities and .../calibrate. Never confuse the two:
 * `announced.capabilities.vision` is a claim, `tested.vision.ok` is a receipt.
 */
export type TestKey = 'tool_calling' | 'vision' | 'json_mode' | 'context_length_effective' | 'streaming_tool_calls' | 'refusal_format';
export interface CapabilityTestResult {
  ok: boolean | null;
  tested_at?: string;
  evidence?: Record<string, unknown>;
}
export interface ModelCapabilityManifest {
  model: string;
  endpoint_id: string;
  stable_model_id?: string;
  announced?: {
    capabilities?: { tools?: boolean; vision?: boolean; reasoning?: boolean };
    limits?: Record<string, number>;
    family?: string;
  };
  tested?: Partial<Record<TestKey, CapabilityTestResult>>;
  degraded?: string[];
  updated_at?: string;
}
/** Tri-state a chip renders: gray "announced", green "tested ✓", red "failed ✗". */
export function capChipState(result?: CapabilityTestResult): 'announced' | 'tested' | 'failed' {
  if (!result) return 'announced';
  if (result.ok === true) return 'tested';
  if (result.ok === false) return 'failed';
  return 'announced';
}

/* ── formatting (mirrors localModels.js) ── */

export function fmtGb(bytes?: number | null): string {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n <= 0) return '—';
  if (n < 0.95 * GIB) return `${Math.round(n / 1048576)} MB`;
  return `${(n / GIB).toFixed(1)} GB`;
}
export function fmtCtx(n?: number | null): string {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return '—';
  return v >= 1024 ? `${Math.round(v / 1024)}k` : String(v);
}
export function untilText(iso?: string | null, now = Date.now()): string {
  if (!iso) return '';
  const at = Date.parse(iso);
  if (!Number.isFinite(at)) return '';
  const s = Math.round((at - now) / 1000);
  if (s > 10 * 365 * 86400) return t('kept loaded');
  if (s <= 0) return t('unloading');
  if (s < 90) return t('{n}s left', { n: s });
  if (s < 3600) return t('{n} min left', { n: Math.round(s / 60) });
  return t('{n} h left', { n: (s / 3600).toFixed(1) });
}
export function shortGpuName(name?: string | null): string {
  return String(name ?? '').replace(/^NVIDIA GeForce /, '');
}
/** The fit word for a verdict; `split` when it fits the pool but no single card. */
export function fitState(fit?: Fit): 'fits' | 'tight' | 'over' | 'split' | '' {
  let state = (fit?.state ?? '') as 'fits' | 'tight' | 'over' | 'split' | '';
  if (fit?.split && state && state !== 'over') state = 'split';
  return state;
}
/** Pinned to a card it does not fit, Ollama does not split: the rest goes to the CPU. */
export function pinWarning(gpuIndex: number | null, sizeBytes: number, cards: GpuCard[]): string {
  if (gpuIndex == null || !sizeBytes) return '';
  const g = cards.find((c) => Number(c.index) === Number(gpuIndex));
  if (!g?.total_bytes) return '';
  const budget = g.total_bytes * 0.82 - 800 * 1048576;
  if (sizeBytes <= budget) return '';
  return t('{size} of weights will not fit {gpu} ({total}) with room for the context: pinned there, Ollama does not split, the rest runs on the CPU.', { size: fmtGb(sizeBytes), gpu: shortGpuName(g.name), total: fmtGb(g.total_bytes) });
}

/* ── calls ── */

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const r = await fetch(path, { credentials: 'same-origin', ...init });
  const text = await r.text();
  let data: unknown = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    /* not json */
  }
  if (!r.ok) {
    const d = data as { detail?: unknown; error?: string };
    throw new Error(typeof d.detail === 'string' ? d.detail : d.error ?? `HTTP ${r.status}`);
  }
  return data as T;
}
const encName = (name: string) => name.split('/').map(encodeURIComponent).join('/');

export function loadLocalModels(endpointId?: string): Promise<LocalModelsData> {
  return getJson<LocalModelsData>(`${API}${endpointId ? `?endpoint_id=${encodeURIComponent(endpointId)}` : ''}`);
}
export async function discoverModels(q: string, endpointId: string): Promise<DiscoverEntry[]> {
  const d = await getJson<{ items?: DiscoverEntry[] }>(`${API}/discover?q=${encodeURIComponent(q)}&endpoint_id=${encodeURIComponent(endpointId)}`);
  return d.items ?? [];
}
export const VALID_NAME = /^[A-Za-z0-9._/:-]+$/;
export async function startPull(endpointId: string, name: string): Promise<{ pull?: Pull; created?: boolean }> {
  return call(`${API}/pull?stream=false`, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ endpoint_id: endpointId, name }) });
}
export const cancelPull = (id: string) => call<unknown>(`${API}/pulls/${encodeURIComponent(id)}`, { method: 'DELETE' });
export function pullEvents(id: string): EventSource {
  return new EventSource(`${API}/pulls/${encodeURIComponent(id)}/events`);
}
/**
 * Load into VRAM. When the model does not fit next to what is resident the
 * server answers 409 with the admission assessment instead of loading it
 * behind your back (OBJ-1); that comes back as `{ blocked }` for the screen
 * to ask with. `force` is the answer — "load anyway", or "I unloaded, go".
 */
export async function loadModel(endpointId: string, name: string, embedding: boolean, force = false): Promise<{ ok: true } | { blocked: VramBlocked }> {
  const r = await fetch(`${API}/load`, { method: 'POST', credentials: 'same-origin', headers: JSON_HEADERS, body: JSON.stringify({ endpoint_id: endpointId, name, embedding, force }) });
  const text = await r.text();
  let data: unknown = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    /* not json */
  }
  if (r.status === 409) {
    const detail = (data as { detail?: { admission?: Record<string, unknown> } }).detail;
    const blocked = detail?.admission ? vramBlockedFrom({ ...detail.admission, phase: 'vram_blocked', ticket: 'load-button' }) : undefined;
    if (blocked) return { blocked };
  }
  if (!r.ok) {
    const d = data as { detail?: unknown; error?: string };
    const detail = d.detail;
    throw new Error(typeof detail === 'string' ? detail : (detail as { message?: string } | undefined)?.message ?? d.error ?? `HTTP ${r.status}`);
  }
  return { ok: true };
}
export const unloadModel = (endpointId: string, name: string, embedding: boolean) => call<unknown>(`${API}/unload`, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ endpoint_id: endpointId, name, embedding }) });
export const deleteModel = (endpointId: string, name: string) => call<unknown>(`${API}/${encName(name)}?endpoint_id=${encodeURIComponent(endpointId)}`, { method: 'DELETE' });
export async function saveModelOptions(endpointId: string, name: string, options: Record<string, string>): Promise<Record<string, string | number>> {
  const d = await call<{ options?: Record<string, string | number> }>(`${API}/${encName(name)}/options?endpoint_id=${encodeURIComponent(endpointId)}`, { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify({ options }) });
  return d.options ?? {};
}
export const setPlacement = (order: number[]) => call<unknown>(`${API}/placement`, { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify({ order }) });
const MODELS_API = '/api/models';
/** Announced (always fresh) + tested (from the last calibration, if any). Cheap: reuses the cached /api/show read. */
export function loadModelCapabilities(endpointId: string, name: string): Promise<ModelCapabilityManifest> {
  return getJson<ModelCapabilityManifest>(`${MODELS_API}/${encName(name)}/capabilities?endpoint_id=${encodeURIComponent(endpointId)}`);
}
/** ~1 minute of short probes against the already-loaded model. 409 if it is not resident — this never loads one. */
export function calibrateModel(endpointId: string, name: string): Promise<ModelCapabilityManifest> {
  return call<ModelCapabilityManifest>(`${MODELS_API}/${encName(name)}/calibrate?endpoint_id=${encodeURIComponent(endpointId)}`, { method: 'POST', headers: JSON_HEADERS });
}
export async function setDefaultModel(endpointId: string, name: string): Promise<void> {
  await call('/api/auth/settings', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ default_endpoint_id: endpointId, default_model: name }) });
}
export const releaseOrphanRunner = (pid: number) => call<unknown>('/api/system/gpu/orphans/release', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ pid }) });

/**
 * How to make a model fit on the card instead of spilling.
 *
 * `/api/system/vram-fit` measures the weights and the KV cache against the
 * free VRAM and answers with the two numbers that decide it: how much
 * context, and how many layers on the GPU. `fits: false` is not a refusal —
 * it is the honest plan for a model too big to hold whole, with the layer
 * count that keeps most of it on the card.
 */
export interface VramFit {
  previewBudget: number | null;
  weights: number;
  bytesPerToken: number | null;
  runtimeContext: number | null;
  runtimeRam: number | null;
  runtimeSpilling: boolean | null;
  fits: boolean;
  num_ctx: number;
  /** null means "let Ollama decide", which is right when it all fits. */
  num_gpu: number | null;
  /** What to do, in the server's own words — one line per step. */
  steps: string[];
  model: string;
  gpuName?: string;
}

/**
 * The default-model residency switch ("Load the default model at startup
 * and keep it loaded"; src/model_warmup.py, `GET`/`POST
 * /api/models/default/residency`). Covers both backends the default can
 * live on — Ollama (kept resident via `keep_alive`) and a managed
 * llama.cpp engine (started, then exempted from `src.engine_swap`'s idle
 * reaper) — the caller never needs to know which one it is.
 */
export interface DefaultResidency {
  enabled: boolean;
  loaded: boolean;
  since: number | null;
  backend: 'ollama' | 'llamacpp' | null;
  backendLabel: string | null;
  model: string | null;
}

function residencyFrom(d: Record<string, unknown>): DefaultResidency {
  return {
    enabled: Boolean(d.enabled),
    loaded: Boolean(d.loaded),
    since: d.since == null ? null : Number(d.since),
    backend: (d.backend as DefaultResidency['backend']) ?? null,
    backendLabel: typeof d.backend_label === 'string' ? d.backend_label : null,
    model: typeof d.model === 'string' ? d.model : null,
  };
}

export async function getDefaultResidency(): Promise<DefaultResidency> {
  return residencyFrom(await call<Record<string, unknown>>('/api/models/default/residency'));
}

export async function setDefaultResidency(enabled: boolean): Promise<DefaultResidency> {
  return residencyFrom(await call<Record<string, unknown>>('/api/models/default/residency', {
    method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ enabled }),
  }));
}

/**
 * Model lease (`src/model_lease.py`, lot L) — when several Faustus instances
 * (dev + prod, or two ports on the same machine) share one Ollama, this is
 * who else is out there, who is the residency leader for a given model, and
 * who — across every instance, not just this one — holds each resident
 * model right now and how (`default` / `pinned` / `active` / `reserved`).
 */
export interface LeaseHolder {
  port: number | null;
  instance_id: string | null;
  kind: 'default' | 'pinned' | 'active' | 'reserved';
}
export interface ResidentModelInfo {
  name: string;
  key: string;
  total_bytes: number;
  in_vram_bytes: number;
  spill_bytes: number;
  ctx: number;
  expires_at: string;
  pinned?: boolean;
  seconds_since_active?: number | null;
  in_grace?: boolean;
}
export interface ResidencySnapshot {
  endpoint_id: string;
  root: string;
  supported: boolean;
  residents: ResidentModelInfo[];
  grace_seconds?: number;
  /** `{}` while the lease is off — never an extra directory read for that. */
  holders: Record<string, LeaseHolder[]>;
  error?: string | null;
}
export interface ModelLeaseInstance {
  instance_id: string | null;
  port: number | null;
  pid: number | null;
  data_dir: string | null;
  default: { root?: string; model?: string };
  pinned: string[];
  adopted_model: string;
  leader: boolean;
  is_self: boolean;
  age_seconds: number | null;
}
export interface ModelLeaseResident {
  model: string;
  holders: LeaseHolder[];
}
export interface ModelLeaseSnapshot {
  root: string;
  self_instance_id: string | null;
  instances: ModelLeaseInstance[];
  resident: ModelLeaseResident[];
}
/** Who is resident right now, with pin + last-active + (lot L) who else
 * holds it — the same reading Settings → Local models already pays for. */
export function loadResidency(endpointId?: string): Promise<ResidencySnapshot> {
  return getJson<ResidencySnapshot>(`${API}/residency${endpointId ? `?endpoint_id=${encodeURIComponent(endpointId)}` : ''}`);
}
/** Every Faustus instance sharing this Ollama right now. Empty lists, not an
 * error, while the lease is off — there is simply nothing else to see. */
export function loadModelLeaseInstances(endpointId?: string): Promise<ModelLeaseSnapshot> {
  return getJson<ModelLeaseSnapshot>(`${API}/instances${endpointId ? `?endpoint_id=${encodeURIComponent(endpointId)}` : ''}`);
}

export async function vramFit(model: string, targetCtx?: number): Promise<VramFit> {
  const q = new URLSearchParams({ model });
  if (targetCtx) q.set('target_ctx', String(targetCtx));
  const d = await call<Record<string, unknown>>(`/api/system/vram-fit?${q.toString()}`);
  return {
    fits: Boolean(d.fits),
    num_ctx: Number(d.num_ctx) || 0,
    num_gpu: d.num_gpu == null ? null : Number(d.num_gpu),
    steps: Array.isArray(d.steps) ? (d.steps as unknown[]).map(String) : [],
    model: String(d.model ?? model),
    gpuName: typeof d.gpu_name === 'string' ? d.gpu_name : undefined,
    previewBudget: d.preview_budget_bytes == null ? null : Number(d.preview_budget_bytes),
    weights: Number(d.file_size_bytes) || 0,
    bytesPerToken: d.kv_bytes_per_token == null ? null : Number(d.kv_bytes_per_token),
    runtimeContext: d.runtime_context == null ? null : Number(d.runtime_context),
    runtimeRam: d.runtime_ram_bytes == null ? null : Number(d.runtime_ram_bytes),
    runtimeSpilling: typeof d.runtime_spilling === 'boolean' ? d.runtime_spilling : null,
  };
}
