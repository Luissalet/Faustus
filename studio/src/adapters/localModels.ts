import { getJson } from './api';
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
}
export interface Vram {
  supported: boolean;
  reason?: string;
  name?: string;
  count?: number;
  total_bytes?: number;
  held_by_runner_bytes?: number;
  other_bytes?: number;
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
  placement_policy?: { prefer: number; name?: string; mode?: string };
  disk?: { path?: string; free_bytes?: number; total_bytes?: number };
  pulls: Pull[];
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
export const loadModel = (endpointId: string, name: string, embedding: boolean) => call<unknown>(`${API}/load`, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ endpoint_id: endpointId, name, embedding }) });
export const unloadModel = (endpointId: string, name: string, embedding: boolean) => call<unknown>(`${API}/unload`, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ endpoint_id: endpointId, name, embedding }) });
export const deleteModel = (endpointId: string, name: string) => call<unknown>(`${API}/${encName(name)}?endpoint_id=${encodeURIComponent(endpointId)}`, { method: 'DELETE' });
export async function saveModelOptions(endpointId: string, name: string, options: Record<string, string>): Promise<Record<string, string | number>> {
  const d = await call<{ options?: Record<string, string | number> }>(`${API}/${encName(name)}/options?endpoint_id=${encodeURIComponent(endpointId)}`, { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify({ options }) });
  return d.options ?? {};
}
export const setPlacement = (prefer: number) => call<unknown>(`${API}/placement`, { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify({ prefer }) });
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
  fits: boolean;
  num_ctx: number;
  /** null means "let Ollama decide", which is right when it all fits. */
  num_gpu: number | null;
  /** What to do, in the server's own words — one line per step. */
  steps: string[];
  model: string;
  gpuName?: string;
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
  };
}
