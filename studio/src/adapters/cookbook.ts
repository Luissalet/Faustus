/**
 * Cookbook (`/api/cookbook/*`, `/api/model/*`, `/api/hwfit/*`): what fits
 * this machine, what is cached, launching and downloading models as
 * background sessions, the servers those sessions run on, and the
 * dependencies each engine needs.
 *
 * The state (tasks, servers, presets, favourites) is the server's
 * `/api/cookbook/state`, mirrored in a small external store so every tab
 * reads the same object and every change is pushed back (debounced) —
 * the previous interface kept the same shape in localStorage and synced it
 * the same way, so the agent's own cookbook tools keep working.
 */
import { useSyncExternalStore } from 'react';
import { ApiError, asArray, getJson } from './api';
import { listEndpoints, type ModelEndpoint } from './settings';
import { redactTask, type LiveStatus, type Task, type TaskPayload, type TaskType } from '../lib/cookbook/tasks';
import type { ServeCtx, ServeFields } from '../lib/cookbook/serve';

/* ── shapes ── */

export interface Server {
  name?: string;
  /** '' = this machine. */
  host: string;
  port?: string;
  env: string;
  envPath: string;
  platform: string;
  modelDirs: string[];
  downloadDir?: string;
  color?: string;
}

export interface CookbookEnv {
  env: string;
  envPath: string;
  gpus: string;
  remoteHost: string;
  remoteServerKey: string;
  servers: Server[];
  platform: string;
  hostPlatform: string;
  hfTokenConfigured: boolean;
  hfTokenMasked: string;
  /** Only present right after the user typed a new one; never read back. */
  hfToken?: string;
  defaultServer?: string;
  modelPaths?: string[];
}

export interface Preset {
  id?: string;
  label: string;
  repo: string;
  backend: string;
  fields: ServeFields;
  env?: string;
  envPath?: string;
  host?: string;
  ts?: number;
  auto?: boolean;
}

export interface CookbookState {
  tasks: Task[];
  removedTasks: Record<string, number>;
  presets: Preset[];
  env: CookbookEnv;
  serveState: { _byRepo: Record<string, ServeFields> } | null;
  serveFavorites: string[];
}

const DEFAULT_DIR = '~/.cache/huggingface/hub';

const emptyEnv = (): CookbookEnv => ({ env: 'none', envPath: '', gpus: '', remoteHost: '', remoteServerKey: '', servers: [{ host: '', env: 'none', envPath: '', platform: '', modelDirs: [DEFAULT_DIR] }], platform: '', hostPlatform: '', hfTokenConfigured: false, hfTokenMasked: '' });
const emptyState = (): CookbookState => ({ tasks: [], removedTasks: {}, presets: [], env: emptyEnv(), serveState: null, serveFavorites: [] });

const isObj = (v: unknown): v is Record<string, unknown> => Boolean(v) && typeof v === 'object' && !Array.isArray(v);
const str = (v: unknown) => (v === null || v === undefined ? '' : String(v));

export const isLocal = (s: Server | null | undefined): boolean => !s || !s.host || s.host === 'local' || s.host.toLowerCase() === 'localhost';

/** A stable key per server profile (same host, different venv = different key). */
export function serverKey(s: Server | null | undefined): string {
  if (isLocal(s)) return 'local';
  return 'srv:' + [s!.name || '', s!.host || '', s!.port || '', s!.envPath || '', s!.platform || ''].map((v) => encodeURIComponent(String(v).trim())).join('|');
}

export function serverByKey(env: CookbookEnv, key: string | null | undefined): Server | null {
  if (!key || key === 'local') return null;
  return env.servers.find((s) => serverKey(s) === key) ?? env.servers.find((s) => s.host === key) ?? env.servers.find((s) => s.name === key) ?? null;
}

export function serverLabel(s: Server | null | undefined): string {
  if (isLocal(s)) return 'Local';
  return s!.name || s!.host;
}

function normalizeServers(raw: unknown, hostPlatform: string): Server[] {
  const list = (Array.isArray(raw) ? raw : []).filter(isObj).map((s): Server => {
    let dirs = Array.isArray(s.modelDirs) ? s.modelDirs.map(str) : [];
    if (s.modelDir && !dirs.includes(str(s.modelDir))) dirs.push(str(s.modelDir));
    dirs = dirs.map((d) => d.replaceAll('✕', '').replaceAll('✖', '').trim()).filter(Boolean);
    if (!dirs.includes(DEFAULT_DIR)) dirs.unshift(DEFAULT_DIR);
    const out: Server = { name: str(s.name) || undefined, host: str(s.host), port: str(s.port) || undefined, env: str(s.env) || 'none', envPath: str(s.envPath), platform: str(s.platform), modelDirs: [...new Set(dirs)], downloadDir: str(s.downloadDir) || undefined, color: str(s.color) || undefined };
    if (out.downloadDir && !out.modelDirs.includes(out.downloadDir)) out.downloadDir = undefined;
    return out;
  });
  let seenLocal = false;
  const kept = list.filter((s) => {
    if (isLocal(s)) {
      s.host = '';
      s.platform = hostPlatform;
      if (seenLocal) return false;
      seenLocal = true;
    }
    return true;
  });
  if (!seenLocal) kept.unshift({ host: '', env: 'none', envPath: '', platform: hostPlatform, modelDirs: [DEFAULT_DIR] });
  return kept;
}

function stateFrom(raw: unknown): CookbookState {
  const src = isObj(raw) ? raw : {};
  const envRaw = isObj(src.env) ? src.env : {};
  const hostPlatform = str(envRaw.hostPlatform);
  const env: CookbookEnv = {
    env: str(envRaw.env) || 'none',
    envPath: str(envRaw.envPath),
    gpus: str(envRaw.gpus),
    remoteHost: str(envRaw.remoteHost),
    remoteServerKey: str(envRaw.remoteServerKey),
    servers: normalizeServers(envRaw.servers, hostPlatform),
    platform: str(envRaw.platform),
    hostPlatform,
    hfTokenConfigured: Boolean(envRaw.hfTokenConfigured),
    hfTokenMasked: str(envRaw.hfTokenMasked),
    defaultServer: str(envRaw.defaultServer) || undefined,
    modelPaths: Array.isArray(envRaw.modelPaths) ? envRaw.modelPaths.map(str) : [],
  };
  const tasksRaw = Array.isArray(src.tasks) ? src.tasks : isObj(src.tasks) ? Object.values(src.tasks) : [];
  const tasks = tasksRaw.filter(isObj).map((t): Task => ({
    id: str(t.id || t.sessionId),
    sessionId: str(t.sessionId || t.id),
    name: str(t.name),
    type: (t.type === 'serve' ? 'serve' : 'download') as TaskType,
    status: str(t.status) || 'running',
    output: str(t.output),
    progress: str(t.progress) || undefined,
    ts: Number(t.ts) || 0,
    payload: isObj(t.payload) ? (t.payload as TaskPayload) : null,
    remoteHost: str(t.remoteHost),
    remoteServerKey: str(t.remoteServerKey) || undefined,
    remoteServerName: str(t.remoteServerName) || undefined,
    sshPort: str(t.sshPort) || undefined,
    platform: str(t.platform) || undefined,
    exit_code: typeof t.exit_code === 'number' ? t.exit_code : null,
    _serveReady: Boolean(t._serveReady),
    _endpointAdded: Boolean(t._endpointAdded),
    _adoptedExternally: Boolean(t._adoptedExternally),
    _backendDiagnosis: isObj(t._backendDiagnosis) ? (t._backendDiagnosis as { message?: string }) : null,
    _diagnosisDismissed: Boolean(t._diagnosisDismissed),
  }));
  const presets = (Array.isArray(src.presets) ? src.presets : []).filter(isObj).map((p): Preset => ({ id: str(p.id) || undefined, label: str(p.label || p.name), repo: str(p.repo || p.repo_id), backend: str(p.backend), fields: isObj(p.fields) ? (p.fields as ServeFields) : {}, env: str(p.env) || undefined, envPath: str(p.envPath) || undefined, host: str(p.host) || undefined, ts: Number(p.ts) || undefined, auto: Boolean(p.auto) }));
  const serveState = isObj(src.serveState) && isObj(src.serveState._byRepo) ? { _byRepo: src.serveState._byRepo as Record<string, ServeFields> } : null;
  return {
    tasks,
    removedTasks: isObj(src.removedTasks) ? (src.removedTasks as Record<string, number>) : {},
    presets,
    env,
    serveState,
    serveFavorites: Array.isArray(src.serveFavorites) ? src.serveFavorites.map(str).filter(Boolean) : [],
  };
}

/* ── the store ── */

let state: CookbookState = emptyState();
let hydrated = false;
const listeners = new Set<() => void>();
let saveTimer: number | null = null;
let pendingToken: string | null = null;

const MIRROR = 'fs-cookbook-state';
try {
  const cached = localStorage.getItem(MIRROR);
  if (cached) state = stateFrom(JSON.parse(cached));
} catch {
  /* private mode */
}

function emit() {
  try {
    localStorage.setItem(MIRROR, JSON.stringify(state));
  } catch {
    /* quota */
  }
  for (const l of listeners) l();
}

export function getCookbookState(): CookbookState {
  return state;
}

export function useCookbookState(): CookbookState {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => state,
    () => state,
  );
}

/** Replace the state (immutable update) and schedule the push. */
export function updateState(fn: (s: CookbookState) => CookbookState, { push = true } = {}): void {
  state = fn(state);
  emit();
  if (push) scheduleSave();
}

export async function loadState(signal?: AbortSignal): Promise<CookbookState> {
  const raw = await getJson<unknown>('/api/cookbook/state', signal);
  const fresh = stateFrom(raw);
  // The active server pick is per device: keep ours over the server's copy.
  const keep = { remoteHost: state.env.remoteHost, remoteServerKey: state.env.remoteServerKey, env: state.env.env, envPath: state.env.envPath, platform: state.env.platform };
  const selected = serverByKey(fresh.env, keep.remoteServerKey || keep.remoteHost);
  fresh.env = { ...fresh.env, ...(hydrated ? keep : {}), ...(selected ? { remoteHost: selected.host, remoteServerKey: serverKey(selected), env: selected.env, envPath: selected.envPath, platform: selected.platform } : {}) };
  if (!selected && !fresh.env.remoteHost) {
    const local = fresh.env.servers.find(isLocal);
    fresh.env = { ...fresh.env, remoteHost: '', remoteServerKey: '', env: local?.env || 'none', envPath: local?.envPath || '', platform: local?.platform || fresh.env.hostPlatform };
  }
  // Tasks the server knows and we don't (the agent's downloads) join in.
  const known = new Set(state.tasks.map((t) => t.sessionId));
  const merged = hydrated ? [...state.tasks, ...fresh.tasks.filter((t) => !known.has(t.sessionId) && !fresh.removedTasks[t.sessionId] && !state.removedTasks[t.sessionId])] : fresh.tasks;
  state = { ...fresh, tasks: merged, removedTasks: { ...fresh.removedTasks, ...state.removedTasks } };
  hydrated = true;
  emit();
  return state;
}

function scheduleSave() {
  if (saveTimer) window.clearTimeout(saveTimer);
  saveTimer = window.setTimeout(() => void saveState(), 400);
}

export async function saveState(): Promise<void> {
  if (!hydrated || !state.env.servers.length) return;
  const env: Record<string, unknown> = { ...state.env };
  delete env.hfToken;
  delete env.hfTokenMasked;
  delete env.hfTokenConfigured;
  if (pendingToken !== null) {
    env.hfToken = pendingToken;
  }
  const body = { tasks: state.tasks.map(redactTask), removedTasks: state.removedTasks, presets: state.presets, env, serveState: state.serveState, serveFavorites: state.serveFavorites };
  const res = await fetch('/api/cookbook/state', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (res.ok && pendingToken !== null) {
    const had = pendingToken !== '';
    pendingToken = null;
    state = { ...state, env: { ...state.env, hfTokenConfigured: had, hfTokenMasked: had ? '••••' : '' } };
    emit();
  }
}

/** Store a new HF token (empty string clears it) with the next save. */
export function setHfToken(token: string): void {
  pendingToken = token;
  updateState((s) => ({ ...s, env: { ...s.env, hfTokenConfigured: Boolean(token) } }));
}

/* ── selection helpers ── */

export function selectedServer(env: CookbookEnv): Server | null {
  return serverByKey(env, env.remoteServerKey) ?? (env.remoteHost ? env.servers.find((s) => s.host === env.remoteHost) ?? null : null);
}

export function selectServer(key: string): void {
  updateState((s) => {
    const srv = serverByKey(s.env, key);
    if (!srv) {
      const local = s.env.servers.find(isLocal);
      return { ...s, env: { ...s.env, remoteHost: '', remoteServerKey: '', env: local?.env || 'none', envPath: local?.envPath || '', platform: local?.platform || s.env.hostPlatform } };
    }
    return { ...s, env: { ...s.env, remoteHost: srv.host, remoteServerKey: serverKey(srv), env: srv.env || 'none', envPath: srv.envPath || '', platform: srv.platform || '' } };
  });
}

/** The facts the command builders need for the selected (or a given) server. */
export function serveCtx(env: CookbookEnv, hwBackend: string, server?: Server | null): ServeCtx {
  const srv = server === undefined ? selectedServer(env) : server;
  return {
    platform: srv ? srv.platform : env.hostPlatform,
    remoteHost: srv ? srv.host : '',
    env: srv ? srv.env || 'none' : env.servers.find(isLocal)?.env || 'none',
    envPath: srv ? srv.envPath : env.servers.find(isLocal)?.envPath || '',
    gpus: env.gpus,
    hwBackend,
    hostPlatform: env.hostPlatform,
  };
}

/* ── tasks ── */

export function addTask(sessionId: string, name: string, type: TaskType, payload: TaskPayload, meta: { host: string; serverKey?: string; serverName?: string; sshPort?: string; platform?: string; status?: string }): Task {
  const task: Task = redactTask({ id: sessionId, sessionId, name, type, status: meta.status || 'running', output: '', ts: Date.now(), payload, remoteHost: meta.host, remoteServerKey: meta.serverKey, remoteServerName: meta.serverName, sshPort: meta.sshPort, platform: meta.platform });
  updateState((s) => {
    let tasks = s.tasks;
    if (type === 'serve' && payload.repo_id) tasks = tasks.filter((t) => !(t.type === 'download' && t.status === 'done' && t.payload?.repo_id === payload.repo_id));
    tasks = tasks.filter((t) => t.sessionId !== sessionId);
    return { ...s, tasks: [...tasks, task] };
  });
  return task;
}

export function patchTask(sessionId: string, updates: Partial<Task>): void {
  updateState((s) => ({ ...s, tasks: s.tasks.map((t) => (t.sessionId === sessionId ? { ...t, ...updates } : t)) }));
}

export function removeTask(sessionId: string): void {
  updateState((s) => ({ ...s, tasks: s.tasks.filter((t) => t.sessionId !== sessionId), removedTasks: { ...s.removedTasks, [sessionId]: Date.now() } }));
}

export async function tasksStatus(signal?: AbortSignal): Promise<LiveStatus[]> {
  const raw = await getJson<{ tasks?: unknown }>('/api/cookbook/tasks/status', signal);
  return asArray<LiveStatus>(raw.tasks);
}

/* ── shell ── */

export interface ShellResult {
  stdout: string;
  stderr: string;
  exit_code: number;
}

export async function shellExec(command: string, timeout = 30): Promise<ShellResult> {
  const res = await fetch('/api/shell/exec', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ command, timeout }) });
  const data = (await res.json().catch(() => ({}))) as Partial<ShellResult>;
  return { stdout: str(data.stdout), stderr: str(data.stderr), exit_code: typeof data.exit_code === 'number' ? data.exit_code : res.ok ? 0 : 1 };
}

async function postJson(path: string, body: unknown): Promise<Record<string, unknown>> {
  const res = await fetch(path, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
  if (!res.ok) throw new ApiError(str(data.detail || data.error) || `${path} responded ${res.status}`, res.status);
  return data;
}

/* ── launching ── */

export interface ServeRequest {
  repo_id: string;
  cmd: string;
  remote_host?: string;
  ssh_port?: string;
  env_prefix?: string;
  hf_token?: string;
  gpus?: string;
  platform?: string;
}

export async function serveModel(body: ServeRequest): Promise<{ sessionId: string; endpointId: string | null }> {
  const data = await postJson('/api/model/serve', body);
  if (data.ok === false) throw new ApiError(str(data.error || data.detail) || 'Launch failed', 400);
  return { sessionId: str(data.session_id), endpointId: data.endpoint_id ? str(data.endpoint_id) : null };
}

export interface DownloadRequest {
  repo_id: string;
  backend?: string;
  include?: string;
  hf_token?: string;
  env_prefix?: string;
  remote_host?: string;
  ssh_port?: string;
  platform?: string;
  local_dir?: string;
  disable_hf_transfer?: boolean;
  remote_server_key?: string;
  remote_server_name?: string;
}

export async function downloadModel(body: DownloadRequest): Promise<string> {
  const { remote_server_key: _k, remote_server_name: _n, ...wire } = body;
  const data = await postJson('/api/model/download', wire);
  if (data.ok === false) throw new ApiError(str(data.error || data.detail) || 'Download failed', 400);
  return str(data.session_id);
}

/** Kill a session the server's way (tmux kill-session, or the Windows tree). */
export async function stopSession(sessionId: string): Promise<void> {
  await postJson(`/api/codex/cookbook/stop/${encodeURIComponent(sessionId)}`, {});
}

export async function sessionOutput(sessionId: string, tail = 400): Promise<string> {
  const raw = await getJson<{ output?: unknown }>(`/api/codex/cookbook/output/${encodeURIComponent(sessionId)}?tail=${tail}`);
  return str(raw.output);
}

export async function killPid(pid: number, host: string, sshPort: string | undefined, signal: 'TERM' | 'KILL'): Promise<Record<string, unknown>> {
  return postJson('/api/cookbook/kill-pid', { pid, host: host || null, ssh_port: sshPort || null, signal });
}

/* ── inventory ── */

export interface CachedModel {
  repo_id: string;
  size: string;
  size_bytes?: number;
  nb_files: number;
  has_incomplete: boolean;
  status: string;
  path: string;
  mtime?: number;
  is_diffusion: boolean;
  is_video: boolean;
  is_adapter: boolean;
  is_ollama?: boolean;
  is_local_dir?: boolean;
  is_gguf?: boolean;
  backend?: string;
  gguf_files: { rel_path: string; size_bytes?: number }[];
}

export async function cachedModels(server: Server | null, signal?: AbortSignal): Promise<{ models: CachedModel[]; error: string }> {
  const p = new URLSearchParams();
  if (server && !isLocal(server)) {
    p.set('host', server.host);
    if (server.port) p.set('ssh_port', server.port);
  }
  const dirs = (server?.modelDirs ?? []).filter((d) => d && d !== DEFAULT_DIR);
  if (dirs.length) p.set('model_dir', dirs.join(','));
  if (server?.platform) p.set('platform', server.platform);
  const raw = await getJson<{ models?: unknown; error?: unknown }>(`/api/model/cached${p.toString() ? `?${p.toString()}` : ''}`, signal);
  return {
    models: asArray<Record<string, unknown>>(raw.models).map((m) => ({
      repo_id: str(m.repo_id),
      size: str(m.size),
      size_bytes: typeof m.size_bytes === 'number' ? m.size_bytes : undefined,
      nb_files: Number(m.nb_files) || 0,
      has_incomplete: Boolean(m.has_incomplete),
      status: str(m.status) || 'ready',
      path: str(m.path),
      mtime: typeof m.mtime === 'number' ? m.mtime : undefined,
      is_diffusion: Boolean(m.is_diffusion),
      is_video: Boolean(m.is_video),
      is_adapter: Boolean(m.is_adapter),
      is_ollama: Boolean(m.is_ollama),
      is_local_dir: Boolean(m.is_local_dir),
      is_gguf: Boolean(m.is_gguf),
      backend: str(m.backend) || undefined,
      gguf_files: asArray<Record<string, unknown>>(m.gguf_files).map((f) => ({ rel_path: str(f.rel_path), size_bytes: typeof f.size_bytes === 'number' ? f.size_bytes : undefined })),
    })),
    error: str(raw.error),
  };
}

export interface Gpu {
  index: number;
  name: string;
  free_mb: number;
  total_mb: number;
  used_mb: number;
  util_pct: number;
  busy: boolean;
  processes: { pid?: number; name?: string; used_mb?: number }[];
}

export async function listGpus(server: Server | null, signal?: AbortSignal): Promise<{ gpus: Gpu[]; error: string }> {
  const p = new URLSearchParams();
  if (server && !isLocal(server)) {
    p.set('host', server.host);
    if (server.port) p.set('ssh_port', server.port);
  }
  try {
    const raw = await getJson<{ gpus?: unknown; error?: unknown }>(`/api/cookbook/gpus${p.toString() ? `?${p.toString()}` : ''}`, signal);
    return { gpus: asArray<Gpu>(raw.gpus), error: str(raw.error) };
  } catch (e) {
    return { gpus: [], error: (e as Error).message };
  }
}

/* ── hardware fit ── */

export interface HwSystem {
  total_ram_gb: number;
  available_ram_gb: number;
  cpu_cores: number;
  cpu_name: string;
  cpu_arch: string;
  has_gpu: boolean;
  gpu_name: string;
  gpu_vram_gb: number;
  gpu_count: number;
  backend: string;
  homogeneous: boolean;
  gpu_error: string | null;
  platform: string;
  gpus: { index: number; name: string; vram_gb: number }[];
  gpu_groups: { name: string; vram_each: number; count: number; indices: number[]; vram_total: number }[];
  unified_memory?: boolean;
  error?: string;
}

export interface FitModel {
  name: string;
  provider: string;
  parameter_count: string;
  params_b: number;
  is_moe: boolean;
  use_case: string;
  fit_level: 'perfect' | 'good' | 'marginal' | 'too_tight' | 'no_fit' | string;
  run_mode: string;
  quant: string;
  context: number;
  required_gb: number;
  speed_tps: number;
  score: number;
  scores: { quality: number; speed: number; fit: number; context: number };
  gguf_sources: { repo: string; provider: string; file: string }[];
  context_length: number;
  release_date: string;
  target_context: number | null;
  is_image_gen?: boolean;
  ollama?: string;
  [k: string]: unknown;
}

export interface FitQuery {
  server?: Server | null;
  useCase?: string;
  search?: string;
  sort?: string;
  limit?: number;
  ctx?: string;
  gpuCount?: string;
  gpuGroup?: string;
  fresh?: boolean;
  refreshCatalog?: boolean;
  manual?: { mode: string; gpuCount?: string; vramGb?: string; ramGb?: string; backend?: string; ignoreGpu?: boolean; ignoreRam?: boolean } | null;
  fitOnly?: boolean;
}

function targetParams(server: Server | null | undefined, p: URLSearchParams) {
  if (server && !isLocal(server)) {
    p.set('host', server.host);
    if (server.port) p.set('ssh_port', server.port);
    if (server.platform) p.set('platform', server.platform);
  }
}

export async function hwSystem(server: Server | null, fresh = false, signal?: AbortSignal): Promise<HwSystem> {
  const p = new URLSearchParams();
  targetParams(server, p);
  if (fresh) p.set('fresh', 'true');
  return getJson<HwSystem>(`/api/hwfit/system?${p.toString()}`, signal);
}

export async function hwModels(q: FitQuery, signal?: AbortSignal): Promise<{ system: HwSystem; models: FitModel[]; error: string }> {
  const p = new URLSearchParams();
  targetParams(q.server, p);
  if (q.useCase) p.set('use_case', q.useCase);
  if (q.search) p.set('search', q.search);
  p.set('sort', q.sort || 'newest');
  p.set('limit', String(q.limit ?? 80));
  if (q.ctx) p.set('ctx', q.ctx);
  if (q.gpuCount) p.set('gpu_count', q.gpuCount);
  if (q.gpuGroup) p.set('gpu_group', q.gpuGroup);
  if (q.fresh) p.set('fresh', 'true');
  if (q.refreshCatalog) p.set('refresh_catalog', 'true');
  if (q.fitOnly) p.set('fit_only', 'true');
  if (q.manual) {
    p.set('manual_mode', q.manual.mode);
    if (q.manual.gpuCount) p.set('manual_gpu_count', q.manual.gpuCount);
    if (q.manual.vramGb) p.set('manual_vram_gb', q.manual.vramGb);
    if (q.manual.ramGb) p.set('manual_ram_gb', q.manual.ramGb);
    if (q.manual.backend) p.set('manual_backend', q.manual.backend);
    if (q.manual.ignoreGpu) p.set('ignore_detected_gpu', 'true');
    if (q.manual.ignoreRam) p.set('ignore_detected_ram', 'true');
  }
  const raw = await getJson<{ system: HwSystem; models?: unknown; error?: unknown }>(`/api/hwfit/models?${p.toString()}`, signal);
  return { system: raw.system, models: asArray<FitModel>(raw.models), error: str(raw.error) };
}

export interface ImageFitModel {
  id: string;
  name: string;
  provider: string;
  params_b: number;
  vram_needed: number;
  quant: string;
  quant_repo: string | null;
  fits: boolean;
  fit: string;
  fit_label: string;
  fit_budget: string;
  [k: string]: unknown;
}

export async function hwImageModels(server: Server | null, signal?: AbortSignal): Promise<{ system: HwSystem; models: ImageFitModel[] }> {
  const p = new URLSearchParams();
  targetParams(server, p);
  p.set('limit', '60');
  const raw = await getJson<{ system: HwSystem; models?: unknown }>(`/api/hwfit/image-models?${p.toString()}`, signal);
  return { system: raw.system, models: asArray<ImageFitModel>(raw.models) };
}

export interface ServeProfile {
  key: string;
  label: string;
  quant: string;
  n_gpu_layers: number;
  n_cpu_moe: number;
  cache_type: string;
  ctx: number;
  est_vram_gb: number;
  fits: boolean;
  offloads: boolean;
  note: string;
}

export async function serveProfiles(model: string, server: Server | null, opts: { modelPath?: string; weightsGb?: number; quant?: string } = {}, signal?: AbortSignal): Promise<{ system: HwSystem; profiles: ServeProfile[] }> {
  const p = new URLSearchParams({ model });
  targetParams(server, p);
  if (opts.modelPath) p.set('model_path', opts.modelPath);
  if (opts.weightsGb) p.set('serve_weights_gb', String(opts.weightsGb));
  if (opts.quant) p.set('serve_quant', opts.quant);
  const raw = await getJson<{ system: HwSystem; profiles?: unknown }>(`/api/hwfit/profiles?${p.toString()}`, signal);
  return { system: raw.system, profiles: asArray<ServeProfile>(raw.profiles) };
}

/* ── catalogues ── */

export interface OllamaLibraryModel {
  name: string;
  description: string;
  sizes: string[];
}

export async function ollamaLibrary(signal?: AbortSignal): Promise<OllamaLibraryModel[]> {
  const raw = await getJson<{ models?: unknown }>('/api/cookbook/ollama/library', signal);
  return asArray<Record<string, unknown>>(raw.models).map((m) => ({ name: str(m.name), description: str(m.description).replace(/&#39;/g, "'").replace(/&amp;/g, '&'), sizes: Array.isArray(m.sizes) ? m.sizes.map(str) : [] }));
}

export interface HfLatestModel {
  repo_id: string;
  downloads: number;
  likes: number;
  createdAt: string;
  tags: string[];
  pipeline_tag?: string;
}

export async function hfLatest(limit = 30, signal?: AbortSignal): Promise<HfLatestModel[]> {
  const raw = await getJson<{ models?: unknown }>(`/api/cookbook/hf-latest?limit=${limit}`, signal);
  return asArray<Record<string, unknown>>(raw.models).map((m) => ({ repo_id: str(m.repo_id), downloads: Number(m.downloads) || 0, likes: Number(m.likes) || 0, createdAt: str(m.createdAt), tags: Array.isArray(m.tags) ? m.tags.map(str) : [], pipeline_tag: str(m.pipeline_tag) || undefined }));
}

export async function hfGgufFiles(repo: string, signal?: AbortSignal): Promise<string[]> {
  const raw = await getJson<{ files?: unknown; ok?: boolean; error?: unknown }>(`/api/cookbook/hf-gguf-files?repo_id=${encodeURIComponent(repo)}`, signal);
  return asArray<string>(raw.files);
}

/* ── dependencies ── */

export interface Package {
  name: string;
  pip: string;
  desc: string;
  category: string;
  target: 'local' | 'remote' | string;
  kind?: string;
  installed: boolean;
  partial?: boolean;
  partial_reason?: string;
  partial_action?: string;
  system_prereqs?: string[];
  system_prereqs_status?: Record<string, boolean>;
  install_hint?: string;
  install_cmd?: string | null;
  update_cmd?: string | null;
  pip_update_available?: boolean;
  update_note?: string;
  version?: string;
  [k: string]: unknown;
}

export async function listPackages(server: Server | null, signal?: AbortSignal): Promise<Package[]> {
  const p = new URLSearchParams();
  if (server && !isLocal(server)) {
    p.set('host', server.host);
    if (server.port) p.set('ssh_port', server.port);
  }
  if (server?.envPath) p.set('venv', server.envPath);
  if (server?.platform) p.set('platform', server.platform);
  const raw = await getJson<{ packages?: unknown }>(`/api/cookbook/packages${p.toString() ? `?${p.toString()}` : ''}`, signal);
  return asArray<Package>(raw.packages);
}

export async function installLocalPackage(pip: string): Promise<Record<string, unknown>> {
  return postJson('/api/cookbook/packages/install', { pip });
}

export async function installSystemDeps(packages: string[], server: Server | null): Promise<Record<string, unknown>> {
  return postJson('/api/cookbook/install-system-deps', { packages, remote_host: server && !isLocal(server) ? server.host : '', ssh_port: server?.port || null });
}

export async function rebuildEngine(server: Server | null, updateSource: boolean): Promise<Record<string, unknown>> {
  return postJson('/api/cookbook/rebuild-engine', { engine: 'llamacpp', remote_host: server && !isLocal(server) ? server.host : '', ssh_port: server?.port || null, update_source: updateSource });
}

/* ── servers ── */

export async function sshKey(): Promise<{ public_key: string; exists: boolean }> {
  const raw = await getJson<Record<string, unknown>>('/api/cookbook/ssh-key');
  return { public_key: str(raw.public_key || raw.pubkey || raw.key), exists: Boolean(raw.exists ?? raw.public_key) };
}

export async function generateSshKey(): Promise<{ public_key: string }> {
  const raw = await postJson('/api/cookbook/ssh-key', {});
  return { public_key: str(raw.public_key || raw.pubkey || raw.key) };
}

export async function testSsh(host: string, port?: string): Promise<ShellResult> {
  const raw = await postJson('/api/cookbook/test-ssh', { host, ssh_port: port || null });
  return { stdout: str(raw.stdout), stderr: str(raw.stderr), exit_code: typeof raw.exit_code === 'number' ? raw.exit_code : 1 };
}

export async function setupServer(host: string, port?: string): Promise<Record<string, unknown>> {
  return postJson('/api/cookbook/setup', { host, ssh_port: port || null });
}

/* ── endpoints (a ready serve registers itself) ── */

export async function endpointsFor(): Promise<ModelEndpoint[]> {
  try {
    return await listEndpoints();
  } catch {
    return [];
  }
}

export async function registerEndpoint(input: { baseUrl: string; name: string; remoteHost: string; model?: string; image?: boolean; supportsTools?: boolean }): Promise<string | null> {
  const fd = new FormData();
  fd.append('base_url', input.baseUrl);
  fd.append('name', input.name);
  fd.append('skip_probe', 'true');
  const h = input.remoteHost.trim();
  if (!h || h === 'local' || h === 'localhost' || h === '127.0.0.1') fd.append('container_local', 'true');
  if (input.model) fd.append('pinned_models', input.model);
  if (input.image) fd.append('model_type', 'image');
  if (input.supportsTools) fd.append('supports_tools', 'true');
  const res = await fetch('/api/model-endpoints', { method: 'POST', credentials: 'same-origin', body: fd });
  if (!res.ok) return null;
  const data = (await res.json().catch(() => ({}))) as { id?: unknown };
  return data.id ? str(data.id) : null;
}

export async function probeEndpoint(id: string): Promise<boolean> {
  try {
    const res = await fetch(`/api/model-endpoints/${encodeURIComponent(id)}/probe`, { method: 'POST', credentials: 'same-origin' });
    const data = (await res.json().catch(() => ({}))) as { online?: boolean; models?: unknown[] };
    return Boolean(data.online || (Array.isArray(data.models) && data.models.length));
  } catch {
    return false;
  }
}

/* ── schedule ── */

export async function scheduleServe(input: { title: string; repoId: string; host: string; startTime: string; endTime: string; days: string[]; mirrorToCalendar: boolean }): Promise<string> {
  const DAYS = ['MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU'];
  const [sh, sm] = input.startTime.split(':').map(Number);
  const [eh, em] = input.endTime.split(':').map(Number);
  let dur = eh * 60 + em - (sh * 60 + sm);
  if (dur <= 0) dur += 24 * 60;
  const d = new Date();
  d.setHours(sh, sm, 0, 0);
  const startUtc = `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`;
  const [shU, smU] = startUtc.split(':').map(Number);
  const days = input.days;
  const sched: Record<string, unknown> = {};
  if (days.length === 7) {
    sched.schedule = 'daily';
    sched.scheduled_time = startUtc;
  } else if (days.length === 5 && ['MO', 'TU', 'WE', 'TH', 'FR'].every((x) => days.includes(x))) {
    sched.schedule = 'cron';
    sched.cron_expression = `${smU} ${shU} * * 1-5`;
  } else if (days.length === 1) {
    sched.schedule = 'weekly';
    sched.scheduled_time = startUtc;
    sched.scheduled_day = DAYS.indexOf(days[0]);
  } else {
    const nums = days.map((k) => {
      const i = DAYS.indexOf(k);
      return i === 6 ? 0 : i + 1;
    });
    sched.schedule = 'cron';
    sched.cron_expression = `${smU} ${shU} * * ${nums.join(',')}`;
  }
  const fullName = input.title || input.repoId || 'model';
  const prompt = { preset: fullName, repo_id: input.repoId, host: input.host, end_after_min: dur };
  const data = await postJson('/api/tasks', { name: `Serve: ${fullName}`, task_type: 'action', action: 'cookbook_serve', trigger_type: 'schedule', prompt: JSON.stringify(prompt), ...sched });
  const id = str(data.id || data.task_id);
  if (input.mirrorToCalendar) {
    try {
      const cals = await getJson<{ calendars?: { name?: string; href?: string }[] }>('/api/calendar/calendars');
      let cal = (cals.calendars ?? []).find((c) => (c.name || '').toLowerCase() === 'cookbook');
      if (!cal) {
        const mk = await fetch('/api/calendar/calendars?name=Cookbook&color=%233b82f6', { method: 'POST', credentials: 'same-origin' });
        cal = mk.ok ? ((await mk.json()) as { href?: string }) : undefined;
      }
      const today = new Date();
      const iso = today.toISOString().slice(0, 10);
      const rrule = days.length === 7 ? 'FREQ=DAILY' : `FREQ=WEEKLY;BYDAY=${days.join(',')}`;
      const evBody: Record<string, unknown> = { title: `Serve: ${fullName}`, start: `${iso}T${input.startTime}:00`, end: `${iso}T${input.endTime}:00`, rrule, color: '#3b82f6', description: `Cookbook serve of ${input.repoId}${input.host ? ` on ${input.host}` : ''}` };
      if (cal?.href) evBody.calendar_href = cal.href;
      const ev = await fetch('/api/calendar/events', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(evBody) });
      const evData = ev.ok ? ((await ev.json()) as { uid?: string; id?: string }) : null;
      const uid = evData?.uid || evData?.id;
      if (uid && id) {
        await fetch(`/api/tasks/${encodeURIComponent(id)}`, { method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt: JSON.stringify({ ...prompt, cookbook_event_uid: uid, cookbook_event_calendar: cal?.href || '' }) }) });
      }
    } catch {
      /* the calendar mirror is a courtesy */
    }
  }
  return id;
}

/** The host a browser connects to for a task's server: the SSH host without the user part. */
export function connectHost(remoteHost: string, fallback = 'localhost'): string {
  const host = (remoteHost || '').trim();
  if (!host || host === 'local') return fallback;
  return host.includes('@') ? host.split('@').pop()! : host;
}

/** What model a serve was told to answer as (for the endpoint check). */
export function expectedModel(task: Task): string {
  const f = (task.payload?._fields ?? {}) as Record<string, unknown>;
  return String(f.served_model_name || f.model_path || task.payload?.repo_id || task.name || '').trim();
}

export function modelMatches(modelId: string, expected: string): boolean {
  const got = modelId.trim().toLowerCase();
  const want = expected.trim().toLowerCase();
  if (!got || !want || got === want) return true;
  const gb = got.split('/').pop()!;
  const wb = want.split('/').pop()!;
  return gb === wb || got.includes(wb) || want.includes(gb);
}

/** The endpoint an Ollama serve advertised in its output, if any. */
export function advertisedEndpoint(output: string, currentHost: string): { host: string; port: string; baseUrl: string } | null {
  const m = output.match(/Ollama API ready on port\s+\d+:\s*(http:\/\/[^\s]+)/i);
  if (!m) return null;
  try {
    const u = new URL(m[1]);
    const any = ['0.0.0.0', '::', '[::]'].includes(u.hostname.toLowerCase());
    const host = any ? currentHost : u.hostname || currentHost;
    const port = u.port || '11434';
    const bh = host.includes(':') && !host.startsWith('[') ? `[${host}]` : host;
    return { host, port, baseUrl: `${u.protocol}//${bh}:${port}/v1` };
  } catch {
    return null;
  }
}
