import { t } from '../i18n';

/**
 * INF-05 Lote C (CONTRATO_INF05.md) — a thin typed mirror of Lote A's
 * backend: `src/contracts/inference.py`'s `GpuInfo`/`HardwareSnapshot`/
 * `IndexReconciliation`/`MemoryBudget`/`CandidateEstimate`/`ContextLimits`
 * shapes, `routes/hardware_routes.py`'s four endpoints. Field names are
 * kept exactly as the server writes them (`to_dict()`), the same choice
 * `adapters/bench.ts` made for INF-04 — this adapter never re-implements a
 * rule the server already owns (the budget math, the reconciliation, the
 * three context limits all stay server-side; this only parses and labels).
 *
 * Parsing is defensive throughout: `absent`/`null` on the wire stays `null`
 * here, NEVER coerced to `0` (§11: "lo que no se observa es absent/unknown,
 * nunca 0") — a field this client doesn't recognise falls back to the same
 * "nothing established yet" value the contract itself defaults to.
 *
 * Errors: every route here answers a failure with the FLAT
 * `{"error": str, "error_class": "hardware.<reason>"}` body
 * `routes/hardware_routes.py` documents — read directly, same convention
 * `adapters/bench.ts::BenchApiError` already follows for `bench.*`.
 */

import { ApiError, responseReason } from './api';

// ── vocabularies (mirrors src/contracts/inference.py) ───────────────────────

export type LinkSource = 'observed' | 'manual' | 'absent';
export type TransportKind = 'pcie' | 'thunderbolt' | 'oculink' | 'usb4' | 'unknown';
export type TransportSource = 'observed' | 'manual' | 'heuristic';
export type MemoryComponentSource = 'observed' | 'reported_engine' | 'estimated' | 'manual' | 'absent';
export type ConsumerKind = 'ollama' | 'faustus_serve' | 'other_process';
export type ReconciliationState = 'same' | 'moved' | 'missing' | 'new' | 'unidentifiable';
export type CandidateBasis = 'single_observation' | 'fitted' | 'metadata' | 'incomplete';
export type HardwareVerdict = 'fits' | 'does_not_fit' | 'unknown';
export type ContextNativeSource = 'hf_config' | 'ollama_show' | 'absent';
export type ContextConfiguredSource = 'receipt' | 'load_options' | 'absent';
export type ContextEvaluatedSource = 'bench_runs' | 'absent';
export type SystemMemorySource = 'psutil' | 'windows_api' | 'absent';

export const TRANSPORT_KINDS: readonly TransportKind[] = ['pcie', 'thunderbolt', 'oculink', 'usb4', 'unknown'];

// ── shapes (src/contracts/inference.py's `to_dict()`, unchanged) ───────────

export interface LinkInfo {
  gen_current: number | null;
  width_current: number | null;
  gen_max: number | null;
  width_max: number | null;
  source: LinkSource;
}

export interface TransportInfo {
  kind: TransportKind;
  source: TransportSource;
  note: string;
  observed_at: string | null;
}

export interface GpuInfo {
  index: number;
  name: string;
  uuid: string | null;
  bus_id: string | null;
  vram_bytes: number | null;
  provenance: string;
  driver: string | null;
  link: LinkInfo | null;
  transport: TransportInfo | null;
}

export interface HardwareSnapshot {
  host: string;
  gpus: GpuInfo[];
  ram_bytes: number | null;
  observed_at: string | null;
  topology: string;
  provenance: string;
}

export interface IndexReconciliation {
  key: string | null;
  previous_index: number | null;
  current_index: number | null;
  state: ReconciliationState;
}

export interface MemoryComponent {
  bytes: number | null;
  source: MemoryComponentSource;
  note: string;
}

export const MEMORY_COMPONENT_NAMES = [
  'weights_resident', 'kv_state', 'buffers_runtime', 'auxiliary_models',
  'other_processes', 'system_margin', 'free',
] as const;
export type MemoryComponentName = (typeof MEMORY_COMPONENT_NAMES)[number];

export type MemoryComponents = Record<MemoryComponentName, MemoryComponent>;

export interface MemoryConsumer {
  kind: ConsumerKind;
  label: string;
  pid: number | null;
  bytes: number | null;
  source: MemoryComponentSource;
}

export interface MemoryBudget {
  gpu_key: string | null;
  gpu_index: number | null;
  gpu_name: string;
  total_bytes: number | null;
  observed_at: string | null;
  components: MemoryComponents;
  consumers: MemoryConsumer[];
  shared_spill: MemoryComponent;
  stale: boolean;
}

export interface EstimateValidity {
  ctx_min: number | null;
  ctx_max: number | null;
  slots: number | null;
}

export interface CandidateEstimate {
  weights: MemoryComponent;
  kv_state: MemoryComponent;
  buffers: MemoryComponent;
  margin: MemoryComponent;
  total_lower: number | null;
  total_upper: number | null;
  complete: boolean;
  basis: CandidateBasis;
  validity: EstimateValidity | null;
  notes: string[];
}

export interface Verdict {
  verdict: HardwareVerdict;
  reason: string;
  shortfall_bytes: number | null;
}

export interface SystemMemory {
  ram_total: number | null;
  ram_available: number | null;
  commit_total: number | null;
  commit_limit: number | null;
  commit_available: number | null;
  source: SystemMemorySource;
}

export interface NativeContextLimit {
  value: number | null;
  source: ContextNativeSource;
  note: string;
}

export interface ConfiguredContextLimit {
  value: number | null;
  source: ContextConfiguredSource;
  note: string;
}

export interface EvaluatedContextLimit {
  min: number | null;
  max: number | null;
  source: ContextEvaluatedSource;
  note: string;
}

export interface ContextLimits {
  native: NativeContextLimit;
  configured: ConfiguredContextLimit;
  evaluated: EvaluatedContextLimit;
}

/** A `MemoryComponent` this client never actually read (e.g. `estimate` was
 *  `null` because no `weights_bytes`/`model` was given) — renders exactly
 *  the same as a server-reported `absent`, never a bare `0`. */
export const ABSENT_MEMORY_COMPONENT: MemoryComponent = { bytes: null, source: 'absent', note: '' };

// ── parsing (defensive: absent/null stays null, never 0) ───────────────────

function asObj(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' ? (v as Record<string, unknown>) : {};
}
function asList(v: unknown): unknown[] {
  return Array.isArray(v) ? v : [];
}
function numOrNull(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}
function strOrNull(v: unknown): string | null {
  return typeof v === 'string' && v.length > 0 ? v : null;
}
function str(v: unknown): string {
  return typeof v === 'string' ? v : '';
}
function bool(v: unknown, fallback = false): boolean {
  return typeof v === 'boolean' ? v : fallback;
}

function parseLinkInfo(raw: unknown): LinkInfo | null {
  if (raw == null) return null;
  const r = asObj(raw);
  return {
    gen_current: numOrNull(r.gen_current), width_current: numOrNull(r.width_current),
    gen_max: numOrNull(r.gen_max), width_max: numOrNull(r.width_max),
    source: (r.source as LinkSource) || 'absent',
  };
}

function parseTransportInfo(raw: unknown): TransportInfo | null {
  if (raw == null) return null;
  const r = asObj(raw);
  return {
    kind: (r.kind as TransportKind) || 'unknown',
    source: (r.source as TransportSource) || 'observed',
    note: str(r.note),
    observed_at: strOrNull(r.observed_at),
  };
}

function parseGpuInfo(raw: unknown): GpuInfo {
  const r = asObj(raw);
  return {
    index: typeof r.index === 'number' ? r.index : 0,
    name: str(r.name),
    uuid: strOrNull(r.uuid),
    bus_id: strOrNull(r.bus_id),
    vram_bytes: numOrNull(r.vram_bytes),
    provenance: str(r.provenance),
    driver: strOrNull(r.driver),
    link: parseLinkInfo(r.link),
    transport: parseTransportInfo(r.transport),
  };
}

export function parseHardwareSnapshot(raw: unknown): HardwareSnapshot {
  const r = asObj(raw);
  return {
    host: str(r.host),
    gpus: asList(r.gpus).map(parseGpuInfo),
    ram_bytes: numOrNull(r.ram_bytes),
    observed_at: strOrNull(r.observed_at),
    topology: str(r.topology) || 'unknown',
    provenance: str(r.provenance),
  };
}

export function parseIndexReconciliation(raw: unknown): IndexReconciliation {
  const r = asObj(raw);
  return {
    key: strOrNull(r.key),
    previous_index: numOrNull(r.previous_index),
    current_index: numOrNull(r.current_index),
    state: (r.state as ReconciliationState) || 'unidentifiable',
  };
}

export function parseMemoryComponent(raw: unknown): MemoryComponent {
  const r = asObj(raw);
  return { bytes: numOrNull(r.bytes), source: (r.source as MemoryComponentSource) || 'absent', note: str(r.note) };
}

function parseMemoryComponents(raw: unknown): MemoryComponents {
  const r = asObj(raw);
  const out = {} as MemoryComponents;
  for (const name of MEMORY_COMPONENT_NAMES) out[name] = parseMemoryComponent(r[name]);
  return out;
}

function parseMemoryConsumer(raw: unknown): MemoryConsumer {
  const r = asObj(raw);
  return {
    kind: (r.kind as ConsumerKind) || 'other_process',
    label: str(r.label),
    pid: numOrNull(r.pid),
    bytes: numOrNull(r.bytes),
    source: (r.source as MemoryComponentSource) || 'absent',
  };
}

export function parseMemoryBudget(raw: unknown): MemoryBudget {
  const r = asObj(raw);
  return {
    gpu_key: strOrNull(r.gpu_key),
    gpu_index: numOrNull(r.gpu_index),
    gpu_name: str(r.gpu_name),
    total_bytes: numOrNull(r.total_bytes),
    observed_at: strOrNull(r.observed_at),
    components: parseMemoryComponents(r.components),
    consumers: asList(r.consumers).map(parseMemoryConsumer),
    shared_spill: parseMemoryComponent(r.shared_spill),
    stale: bool(r.stale),
  };
}

function parseEstimateValidity(raw: unknown): EstimateValidity | null {
  if (raw == null) return null;
  const r = asObj(raw);
  return { ctx_min: numOrNull(r.ctx_min), ctx_max: numOrNull(r.ctx_max), slots: numOrNull(r.slots) };
}

export function parseCandidateEstimate(raw: unknown): CandidateEstimate | null {
  if (raw == null) return null;
  const r = asObj(raw);
  return {
    weights: parseMemoryComponent(r.weights),
    kv_state: parseMemoryComponent(r.kv_state),
    buffers: parseMemoryComponent(r.buffers),
    margin: parseMemoryComponent(r.margin),
    total_lower: numOrNull(r.total_lower),
    total_upper: numOrNull(r.total_upper),
    complete: bool(r.complete),
    basis: (r.basis as CandidateBasis) || 'incomplete',
    validity: parseEstimateValidity(r.validity),
    notes: asList(r.notes).map(str),
  };
}

export function parseVerdict(raw: unknown): Verdict {
  const r = asObj(raw);
  return { verdict: (r.verdict as HardwareVerdict) || 'unknown', reason: str(r.reason), shortfall_bytes: numOrNull(r.shortfall_bytes) };
}

export function parseSystemMemory(raw: unknown): SystemMemory {
  const r = asObj(raw);
  return {
    ram_total: numOrNull(r.ram_total), ram_available: numOrNull(r.ram_available),
    commit_total: numOrNull(r.commit_total), commit_limit: numOrNull(r.commit_limit),
    commit_available: numOrNull(r.commit_available),
    source: (r.source as SystemMemorySource) || 'absent',
  };
}

export function parseContextLimits(raw: unknown): ContextLimits {
  const r = asObj(raw);
  const native = asObj(r.native);
  const configured = asObj(r.configured);
  const evaluated = asObj(r.evaluated);
  return {
    native: { value: numOrNull(native.value), source: (native.source as ContextNativeSource) || 'absent', note: str(native.note) },
    configured: { value: numOrNull(configured.value), source: (configured.source as ContextConfiguredSource) || 'absent', note: str(configured.note) },
    evaluated: { min: numOrNull(evaluated.min), max: numOrNull(evaluated.max), source: (evaluated.source as ContextEvaluatedSource) || 'absent', note: str(evaluated.note) },
  };
}

// ── transport ────────────────────────────────────────────────────────────

export class HardwareApiError extends ApiError {
  readonly errorClass: string | null;

  constructor(message: string, status: number, errorClass: string | null) {
    super(message, status);
    this.name = 'HardwareApiError';
    this.errorClass = errorClass;
  }
}

async function payloadOf(response: Response): Promise<Record<string, unknown>> {
  try {
    const body: unknown = await response.clone().json();
    return body && typeof body === 'object' ? (body as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: { Accept: 'application/json', ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const payload = await payloadOf(response);
    const errorClass = typeof payload.error_class === 'string' ? payload.error_class : null;
    const flatMessage = typeof payload.error === 'string' && payload.error.trim() ? payload.error : null;
    const message = flatMessage ?? (await responseReason(response, path));
    throw new HardwareApiError(message, response.status, errorClass);
  }
  return (await response.json()) as T;
}

function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { signal });
}

function post<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}), signal });
}

// ── routes (routes/hardware_routes.py) ──────────────────────────────────────

export interface TopologyResult {
  snapshot: HardwareSnapshot;
  reconciliation: IndexReconciliation[];
  profile_id: string | null;
}

/** `GET /api/hardware/topology?host=&ssh_port=` — read-only, never starts
 *  inference. `host`/`sshPort` name a remote box the same way
 *  `adapters/cookbook.ts::listGpus` does; empty means this machine. */
export function getTopology(host = '', sshPort = '', signal?: AbortSignal): Promise<TopologyResult> {
  const p = new URLSearchParams();
  if (host) p.set('host', host);
  if (sshPort) p.set('ssh_port', sshPort);
  return get<Record<string, unknown>>(`/api/hardware/topology?${p.toString()}`, signal).then((d) => ({
    snapshot: parseHardwareSnapshot(d.snapshot),
    reconciliation: asList(d.reconciliation).map(parseIndexReconciliation),
    profile_id: strOrNull(d.profile_id),
  }));
}

export interface AnnotateInput {
  gpuKey: string;
  kind: TransportKind;
  note?: string;
  profileId?: string;
}

/** `POST /api/hardware/topology/annotate` — a person's explicit call on one
 *  physical GPU's transport, NEVER inferred automatically; only ever called
 *  from a click (`Servers.tsx`'s "Annotate…" dialog Save button, never a
 *  `useEffect` — `tests/test_inf05_hardware_js.py` greps for exactly that). */
export function annotateTopology(input: AnnotateInput): Promise<Record<string, unknown>> {
  return post<{ profile: Record<string, unknown> }>('/api/hardware/topology/annotate', {
    gpu_key: input.gpuKey,
    kind: input.kind,
    note: input.note ?? '',
    profile_id: input.profileId || undefined,
  }).then((d) => d.profile);
}

export interface BudgetInput {
  endpoint?: string;
  model?: string;
  ctx?: number;
  slots?: number;
  weightsBytes?: number;
}

export interface BudgetResult {
  budgets: Record<string, MemoryBudget>;
  estimate: CandidateEstimate | null;
  verdict: Verdict;
  system_memory: SystemMemory;
}

/** `GET /api/hardware/budget?endpoint=&model=&ctx=&slots=&weights_bytes=` —
 *  the desegregated per-GPU picture, plus a candidate-load estimate only
 *  when `model`/`weightsBytes` is given (otherwise `estimate: null`). */
export function getBudget(input: BudgetInput = {}, signal?: AbortSignal): Promise<BudgetResult> {
  const p = new URLSearchParams();
  if (input.endpoint) p.set('endpoint', input.endpoint);
  if (input.model) p.set('model', input.model);
  if (input.ctx != null && Number.isFinite(input.ctx)) p.set('ctx', String(Math.round(input.ctx)));
  if (input.slots != null && Number.isFinite(input.slots)) p.set('slots', String(Math.round(input.slots)));
  if (input.weightsBytes != null && Number.isFinite(input.weightsBytes)) p.set('weights_bytes', String(Math.round(input.weightsBytes)));
  return get<Record<string, unknown>>(`/api/hardware/budget?${p.toString()}`, signal).then((d) => {
    const budgets: Record<string, MemoryBudget> = {};
    for (const [key, value] of Object.entries(asObj(d.budgets))) budgets[key] = parseMemoryBudget(value);
    return {
      budgets,
      estimate: parseCandidateEstimate(d.estimate),
      verdict: parseVerdict(d.verdict),
      system_memory: parseSystemMemory(d.system_memory),
    };
  });
}

export interface ContextLimitsInput {
  endpoint?: string;
  model?: string;
  profileId?: string;
}

/** `GET /api/hardware/context-limits?endpoint=&model=&profile_id=` — the
 *  three limits kept SEPARATE (§14): never collapsed into one number here. */
export function getContextLimits(input: ContextLimitsInput = {}, signal?: AbortSignal): Promise<ContextLimits> {
  const p = new URLSearchParams();
  if (input.endpoint) p.set('endpoint', input.endpoint);
  if (input.model) p.set('model', input.model);
  if (input.profileId) p.set('profile_id', input.profileId);
  return get<{ limits: Record<string, unknown> }>(`/api/hardware/context-limits?${p.toString()}`, signal).then((d) => parseContextLimits(d.limits));
}

// ── pure presentation helpers — exercised by studio/checks/hardware.check.mjs
// without a DOM. ─────────────────────────────────────────────────────────────

const GIB = 1073741824;

/** Bytes as "14.2 GB" (one decimal, the same convention `bytesLabel`/`fmtGb`
 *  use elsewhere in Cookbook) — `null` is `"not reported"`, spelled out,
 *  NEVER a bare `0` or a bare dash standing in for "nothing observed" (§11). */
export function gbLabel(bytes: number | null): string {
  if (bytes === null) return t('not reported');
  return `${(bytes / GIB).toFixed(1)} GB`;
}

function gbNumber(bytes: number): string {
  return (bytes / GIB).toFixed(1);
}

/** A source enum's badge text — text, never colour alone (accessibility
 *  requirement: a badge must say what it is, not just look a certain way). */
export function sourceLabel(source: string): string {
  switch (source) {
    case 'observed': return t('observed');
    case 'manual': return t('manual');
    case 'heuristic': return t('heuristic');
    case 'estimated': return t('estimated');
    case 'reported_engine': return t('reported by the engine');
    case 'hf_config': return t('hf config');
    case 'ollama_show': return t('ollama');
    case 'receipt': return t('receipt');
    case 'load_options': return t('saved options');
    case 'bench_runs': return t('benchmark runs');
    case 'psutil': return t('psutil');
    case 'windows_api': return t('Windows API');
    case 'absent':
    default:
      return t('not reported');
  }
}

/** A coarse tone for a source badge: `ok` for a real reading (observed, or
 *  the engine's own report), `neutral` for a person's own annotation,
 *  `warning` for anything guessed or missing — never `danger` (a missing
 *  reading is a gap to fill, not a fault). */
export function sourceTone(source: string): 'ok' | 'warning' | 'neutral' {
  if (source === 'observed' || source === 'reported_engine') return 'ok';
  if (source === 'manual') return 'neutral';
  return 'warning'; // heuristic, estimated, absent
}

export function transportKindLabel(kind: TransportKind): string {
  switch (kind) {
    case 'pcie': return t('PCIe');
    case 'thunderbolt': return t('Thunderbolt');
    case 'oculink': return t('OCuLink');
    case 'usb4': return t('USB4');
    default: return t('unknown');
  }
}

export function transportLabel(transport: TransportInfo | null): string {
  if (!transport) return t('not reported');
  return t('{kind} ({source})', { kind: transportKindLabel(transport.kind), source: sourceLabel(transport.source) });
}

export function transportTone(transport: TransportInfo | null): 'ok' | 'warning' | 'neutral' {
  if (!transport) return 'warning';
  return sourceTone(transport.source);
}

/** "Gen4 x16 (observed)" / "Gen3 x4 (observed) — narrow: may be an external
 *  enclosure, verify manually (heuristic)" / "not reported" for an absent
 *  link — the heuristic note only ever appears when `transport.source` is
 *  itself `heuristic` (§11: a narrow OBSERVED link is the only thing that
 *  triggers it, never a GPU's commercial name). */
export function linkLabel(gpu: Pick<GpuInfo, 'link' | 'transport'>): string {
  const link = gpu.link;
  if (!link || link.source === 'absent' || link.gen_current == null || link.width_current == null) return t('not reported');
  // The current generation sags at idle (a 4070 Ti reports Gen1 x16 while
  // idle and Gen4 x16 under load), so the maximum the driver reports is
  // shown next to it whenever it differs — otherwise "Gen1" reads as a fault.
  const hasMax = link.gen_max != null && link.width_max != null;
  const differs = hasMax && (link.gen_max !== link.gen_current || link.width_max !== link.width_current);
  const base = differs
    ? t('Gen{gen} x{width} now · up to Gen{genMax} x{widthMax} ({source})', {
        gen: String(link.gen_current), width: String(link.width_current),
        genMax: String(link.gen_max), widthMax: String(link.width_max), source: sourceLabel(link.source),
      })
    : t('Gen{gen} x{width} ({source})', {
        gen: String(link.gen_current), width: String(link.width_current), source: sourceLabel(link.source),
      });
  if (gpu.transport && gpu.transport.source === 'heuristic') {
    return t('{base} — narrow: may be an external enclosure, verify manually (heuristic)', { base });
  }
  return base;
}

/** `uuid:...`/`bus:...` — mirrors `src.contracts.inference.identity_key`
 *  exactly (an index is NEVER identity), for `POST .../annotate`'s
 *  `gpu_key`. `null` when the driver reported neither. */
export function gpuIdentityKey(gpu: Pick<GpuInfo, 'uuid' | 'bus_id'>): string | null {
  if (gpu.uuid) return `uuid:${gpu.uuid}`;
  if (gpu.bus_id) return `bus:${gpu.bus_id}`;
  return null;
}

/** "a1b2c3d4… · 0000:01:00.0" for the table cell; `gpuIdentityFull` is the
 *  same information untruncated, for the cell's tooltip. */
export function gpuIdentityShort(gpu: Pick<GpuInfo, 'uuid' | 'bus_id'>): string {
  const parts: string[] = [];
  if (gpu.uuid) parts.push(`${gpu.uuid.slice(0, 8)}…`);
  if (gpu.bus_id) parts.push(gpu.bus_id);
  return parts.length ? parts.join(' · ') : t('no stable identity');
}

export function gpuIdentityFull(gpu: Pick<GpuInfo, 'uuid' | 'bus_id'>): string {
  const parts: string[] = [];
  if (gpu.uuid) parts.push(`uuid ${gpu.uuid}`);
  if (gpu.bus_id) parts.push(`bus ${gpu.bus_id}`);
  return parts.length ? parts.join(' · ') : t('no stable identity');
}

function shortReconciliationKey(key: string | null): string {
  if (!key) return t('unknown device');
  const value = key.includes(':') ? key.slice(key.indexOf(':') + 1) : key;
  return value.length > 12 ? `${value.slice(0, 8)}…` : value;
}

/** "GPU a1b2c3d4… was index 1, now 2 (moved)" / "…now missing" / "…is new
 *  at index N" / "cannot identify" — §11 T15: an index is never identity,
 *  so "same" is the only state that ever reads as reassuring. */
export function reconciliationLabel(rec: IndexReconciliation): string {
  const key = shortReconciliationKey(rec.key);
  switch (rec.state) {
    case 'same':
      return t('GPU {key} is still index {idx}', { key, idx: String(rec.current_index ?? rec.previous_index ?? '?') });
    case 'moved':
      return t('GPU {key} was index {prev}, now {curr} (moved)', {
        key, prev: String(rec.previous_index ?? '?'), curr: String(rec.current_index ?? '?'),
      });
    case 'missing':
      return t('GPU {key} was index {prev}, now missing', { key, prev: String(rec.previous_index ?? '?') });
    case 'new':
      return t('GPU {key} is new at index {curr}', { key, curr: String(rec.current_index ?? '?') });
    case 'unidentifiable':
    default:
      return t('A GPU without a stable identity cannot be reconciled (index {idx})', {
        idx: String(rec.previous_index ?? rec.current_index ?? '?'),
      });
  }
}

export function reconciliationTone(state: ReconciliationState): 'ok' | 'warning' | 'danger' | 'neutral' {
  if (state === 'same') return 'ok';
  if (state === 'missing') return 'danger';
  if (state === 'moved' || state === 'unidentifiable') return 'warning';
  return 'neutral'; // new
}

export function memoryComponentLabel(name: MemoryComponentName): string {
  switch (name) {
    case 'weights_resident': return t('Weights (resident)');
    case 'kv_state': return t('KV cache');
    case 'buffers_runtime': return t('Runtime buffers');
    case 'auxiliary_models': return t('Auxiliary models');
    case 'other_processes': return t('Other processes');
    case 'system_margin': return t('System margin');
    case 'free': return t('Free');
    default: return name;
  }
}

/** "16.5 GB (observed)" / "not reported" — never a bare `0` for `absent`. */
export function componentLabel(component: MemoryComponent): string {
  if (component.bytes === null) return t('not reported');
  return t('{bytes} ({source})', { bytes: gbLabel(component.bytes), source: sourceLabel(component.source) });
}

/** A stacked-bar segment's own accessible text (the bar is decorative; this
 *  is what a screen reader — or a print-out — actually says). */
export function budgetBarSummary(budget: Pick<MemoryBudget, 'components'>): string {
  return MEMORY_COMPONENT_NAMES.map((name) => `${memoryComponentLabel(name)}: ${componentLabel(budget.components[name])}`).join(', ');
}

export function consumerKindLabel(kind: ConsumerKind): string {
  switch (kind) {
    case 'ollama': return t('ollama');
    case 'faustus_serve': return t('faustus serve');
    default: return t('other process');
  }
}

/** "ollama · qwen3.8 · 16.5 GB (observed)" / "faustus serve · session … ·
 *  not reported" / "other process · pid 1234 · estimated". */
export function consumerLabel(c: MemoryConsumer): string {
  const parts: string[] = [consumerKindLabel(c.kind)];
  if (c.label) parts.push(c.label);
  else if (c.pid != null) parts.push(t('pid {pid}', { pid: String(c.pid) }));
  parts.push(c.bytes != null ? t('{bytes} ({source})', { bytes: gbLabel(c.bytes), source: sourceLabel(c.source) }) : sourceLabel(c.source));
  return parts.join(' · ');
}

/** "Shared system memory (spill): 3.2 GB — not VRAM" — only when the WDDM
 *  runner actually reported a spill; `null` (nothing to show) when it did
 *  not, never "0 GB" pretending there is a number worth showing. */
export function sharedSpillLabel(spill: MemoryComponent): string | null {
  if (spill.bytes === null || spill.bytes <= 0) return null;
  return t('Shared system memory (spill): {n} — not VRAM', { n: gbLabel(spill.bytes) });
}

/** "Reading is 42 s old — refresh" when `stale`; `null` when the reading is
 *  fresh (nothing to show). */
export function staleLabel(budget: Pick<MemoryBudget, 'stale' | 'observed_at'>, now: number = Date.now()): string | null {
  if (!budget.stale) return null;
  const observed = budget.observed_at ? Date.parse(budget.observed_at) : NaN;
  if (!Number.isFinite(observed)) return t('Reading is stale — refresh');
  const seconds = Math.max(0, Math.round((now - observed) / 1000));
  return t('Reading is {n} s old — refresh', { n: String(seconds) });
}

function incompleteReason(estimate: CandidateEstimate): string {
  return estimate.notes.length > 0 ? estimate.notes[0] : t('architecture unknown');
}

/** "14.2–15.1 GB" for a complete estimate; "≥ 14.2 GB (incomplete: …)" for
 *  a floor with no upper bound; "unknown (…)" when there is nothing to
 *  show at all — §11: an unknown architecture is an INCOMPLETE estimate,
 *  never false precision dressed up as a number. */
export function estimateRangeLabel(estimate: CandidateEstimate | null): string {
  if (!estimate) return t('unknown');
  if (estimate.complete && estimate.total_lower != null && estimate.total_upper != null) {
    return t('{lower}–{upper} GB', { lower: gbNumber(estimate.total_lower), upper: gbNumber(estimate.total_upper) });
  }
  if (estimate.total_lower != null) {
    return t('≥ {lower} GB (incomplete: {reason})', { lower: gbNumber(estimate.total_lower), reason: incompleteReason(estimate) });
  }
  return t('unknown ({reason})', { reason: incompleteReason(estimate) });
}

function fmtCtxShort(n: number): string {
  return n >= 1024 ? `${Math.round(n / 1024)}k` : String(n);
}

/** "observed overhead at ctx 8192" / "fitted over 4k–32k" / "from
 *  metadata" / "incomplete" — the estimate's own `basis`, in words. */
export function basisLabel(estimate: Pick<CandidateEstimate, 'basis' | 'validity'>): string {
  switch (estimate.basis) {
    case 'single_observation':
      return estimate.validity?.ctx_min != null
        ? t('observed overhead at ctx {ctx}', { ctx: String(estimate.validity.ctx_min) })
        : t('observed overhead in this configuration');
    case 'fitted':
      return estimate.validity?.ctx_min != null && estimate.validity?.ctx_max != null
        ? t('fitted over {min}–{max}', { min: fmtCtxShort(estimate.validity.ctx_min), max: fmtCtxShort(estimate.validity.ctx_max) })
        : t('fitted over the observed range');
    case 'metadata':
      return t('from metadata');
    default:
      return t('incomplete');
  }
}

export function verdictTone(v: HardwareVerdict): 'ok' | 'warning' | 'danger' {
  if (v === 'fits') return 'ok';
  if (v === 'does_not_fit') return 'danger';
  return 'warning';
}

/** "fits" / "does not fit (short by 2.1 GB)" / "unknown (reading is 42 s
 *  old; refresh before loading)" — §11: a stale or incomplete reading is
 *  never presented as "fits". */
export function verdictLabel(v: Verdict): string {
  if (v.verdict === 'fits') return t('fits');
  if (v.verdict === 'does_not_fit') {
    return v.shortfall_bytes != null
      ? t('does not fit (short by {n})', { n: gbLabel(v.shortfall_bytes) })
      : t('does not fit');
  }
  return t('unknown ({reason})', { reason: v.reason || t('reason not given') });
}

/** "native 8192 (hf config)" / "native: not reported" — §14: the model's
 *  TRAINED context, the original `max_position_embeddings` even when
 *  RoPE/YaRN has since extended it further. */
export function nativeContextLabel(n: NativeContextLimit): string {
  if (n.value == null) return t('native: not reported');
  return t('native {n} ({detail})', { n: String(n.value), detail: n.note || sourceLabel(n.source) });
}

/** "configured 8192 (receipt)" / "configured: not reported" — what the
 *  running engine is actually SET to right now. */
export function configuredContextLabel(c: ConfiguredContextLimit): string {
  if (c.value == null) return t('configured: not reported');
  return t('configured {n} ({detail})', { n: String(c.value), detail: c.note || sourceLabel(c.source) });
}

/** "evaluated up to 4096 (largest observed prompt across 3 run(s))" /
 *  "evaluated: not reported" — the largest prompt a benchmark run has
 *  actually EXERCISED, never a guess. */
export function evaluatedContextLabel(e: EvaluatedContextLimit): string {
  if (e.max == null) return t('evaluated: not reported');
  return t('evaluated up to {n} ({detail})', { n: String(e.max), detail: e.note || sourceLabel(e.source) });
}

/** "Context: native 8192 (hf config) · configured 8192 (receipt) ·
 *  evaluated up to 4096 (…)" — §14: three SEPARATE numbers, never
 *  collapsed into one. */
export function contextLimitsLine(limits: ContextLimits): string {
  return t('Context: {native} · {configured} · {evaluated}', {
    native: nativeContextLabel(limits.native),
    configured: configuredContextLabel(limits.configured),
    evaluated: evaluatedContextLabel(limits.evaluated),
  });
}
