import { useEffect, useState, useSyncExternalStore } from 'react';
import { locale, t } from '../i18n';
import { getJson } from './api';

/**
 * Live usage — what `ollama ps` and `nvidia-smi` would say, from
 * `/api/system/usage` (routes/system_usage_routes.py), the same endpoint the
 * previous interface's pill polled (static/js/sysUsage.js).
 *
 * The helpers are pure and mirror the old ones (poolOf, placementText,
 * level) so the numbers agree between the two interfaces. What is new is the
 * history: the last minute of GPU utilisation lives in this module, so the
 * trace in the header survives a screen remount and grows from the first
 * sample instead of starting flat.
 */

export interface GpuModel {
  name?: string;
  bytes?: number | null;
}

export interface Gpu {
  index: number;
  name?: string;
  util?: number | null;
  mem_used?: number | null; // MiB
  mem_total?: number | null;
  mem_free?: number | null;
  temp?: number | null;
  power?: number | null;
  power_limit?: number | null;
  models?: GpuModel[];
}

export interface GpuPool {
  count: number;
  util: number | null;
  util_avg: number | null;
  temp: number | null;
  mem_used: number | null;
  mem_total: number | null;
  mem_free: number | null;
  power: number | null;
  power_limit: number | null;
  names: string[];
}

export interface LoadedModel {
  name: string;
  parameter_size?: string;
  quantization?: string;
  gpu_pct: number;
  cpu_pct: number;
  size?: number | null;
  size_vram?: number | null;
  context_length?: number | null;
  expires_at?: string | null;
  placement?: 'cpu' | 'single' | 'split' | null;
  gpus?: number[];
  per_gpu?: { index: number; bytes?: number | null }[];
}

export interface HealthComponent {
  name: string;
  label?: string;
  value?: string | number | null;
  state?: 'ok' | 'warn' | 'bad' | 'no_data';
  why?: string;
}

export interface Health {
  score: number;
  grade?: string;
  collected?: boolean;
  reporting?: number;
  of?: number;
  components: HealthComponent[];
}

export interface Orphan {
  pid: number;
  name?: string;
  bytes?: number | null;
  gpus?: number[];
}

export interface Usage {
  ts?: number;
  ollama?: { reachable: boolean; base?: string; models?: LoadedModel[] };
  gpu?: Gpu[];
  gpu_pool?: Partial<GpuPool> & { name?: string };
  orphans?: Orphan[];
  gpu_mem?: {
    supported: boolean;
    total_shared?: number;
    ollama?: { shared?: number; dedicated?: number; shared_fraction?: number; spilling?: boolean };
  };
  sysmem_fallback?: { steps?: string[] };
  ram?: { used: number; total: number; percent: number };
  cpu?: { percent?: number | null; count?: number };
  health?: Health;
  errors?: string[];
}

export type Level = '' | 'warm' | 'hot';

/* ── formatting ─────────────────────────────────────────────────────── */

export function gb(bytes: number | null | undefined, digits = 1): string {
  return bytes == null ? '—' : (bytes / 1073741824).toFixed(digits);
}
export function mib2gb(mib: number | null | undefined, digits = 1): string {
  return mib == null ? '—' : (mib / 1024).toFixed(digits);
}
/** Whole gigabytes for a total (12282 MiB → "12"). */
export function gbInt(mib: number | null | undefined): string {
  return mib == null ? '—' : String(Math.round(mib / 1024));
}
export function pct(v: number | null | undefined): string {
  return v == null ? '—' : `${Math.round(v)}%`;
}
export function fmtCtx(n: number | null | undefined): string {
  if (!n) return '—';
  return n >= 1024 ? `${Math.round(n / 1024)}k` : String(n);
}
export function mbytes(bytes: number | null | undefined): string {
  return bytes == null ? '—' : Math.round(bytes / 1048576).toLocaleString(locale());
}
/** "NVIDIA GeForce RTX 4070 Ti" → "RTX 4070 Ti". */
export function shortGpuName(name?: string): string {
  return String(name ?? '').replace(/^NVIDIA GeForce /, '');
}
export function untilText(iso?: string | null): string {
  if (!iso) return '';
  const at = Date.parse(iso);
  if (!Number.isFinite(at)) return '';
  const s = Math.round((at - Date.now()) / 1000);
  if (s <= 0) return t('unloading');
  if (s < 90) return t('{n}s left', { n: s });
  if (s < 3600) return t('{n} min left', { n: Math.round(s / 60) });
  return t('{n} h left', { n: (s / 3600).toFixed(1) });
}

/** '' | 'warm' | 'hot' for a percentage where high is bad. */
export function level(p: number | null | undefined): Level {
  return p == null ? '' : p >= 90 ? 'hot' : p >= 70 ? 'warm' : '';
}

/* ── the pool ───────────────────────────────────────────────────────── */

export function isMulti(d: Usage | null): boolean {
  return !!(d?.gpu_pool && (d.gpu_pool.count ?? 0) > 1);
}

/** The pool block with every gap filled from the cards themselves. */
export function poolOf(d: Usage | null): GpuPool {
  const gpus = d?.gpu ?? [];
  const p = d?.gpu_pool ?? {};
  const num = <T,>(v: T | null | undefined, fallback: T | null): T | null => (v == null ? fallback : v);
  const has = (k: keyof Gpu) => gpus.some((g) => g[k] != null);
  const sum = (k: keyof Gpu) => gpus.reduce((a, g) => a + (Number(g[k]) || 0), 0);
  const max = (k: keyof Gpu) => gpus.reduce<number | null>((a, g) => (g[k] != null && (a == null || Number(g[k]) > a) ? Number(g[k]) : a), null);
  return {
    count: Number(num(p.count, gpus.length)) || 0,
    util: num(p.util, max('util')),
    util_avg: num(p.util_avg, gpus.length && has('util') ? sum('util') / gpus.length : null),
    temp: num(p.temp, max('temp')),
    mem_used: num(p.mem_used, has('mem_used') ? sum('mem_used') : null),
    mem_total: num(p.mem_total, has('mem_total') ? sum('mem_total') : null),
    mem_free: num(p.mem_free, has('mem_free') ? sum('mem_free') : null),
    power: num(p.power, has('power') ? sum('power') : null),
    power_limit: num(p.power_limit, has('power_limit') ? sum('power_limit') : null),
    names: Array.isArray(p.names) ? p.names : gpus.map((g) => g.name ?? ''),
  };
}

export function firstModel(d: Usage | null): LoadedModel | null {
  return d?.ollama?.models?.[0] ?? null;
}
export function spilling(d: Usage | null): boolean {
  return !!d?.gpu_mem?.ollama?.spilling;
}

/** The busiest card decides: a card at 95 % beside an idle one is a full card. */
export function worstLevel(d: Usage | null): Level {
  let worst = 0;
  for (const g of d?.gpu ?? []) {
    const vram = g.mem_total ? ((g.mem_used ?? 0) / g.mem_total) * 100 : 0;
    worst = Math.max(worst, g.util ?? 0, vram);
  }
  return level(worst);
}

/** `GPU 1 (RTX 5060 Ti)` / `split: #0 8.5 GB + #1 10.2 GB` / `CPU`. */
export function placementText(m: LoadedModel, d: Usage | null): string {
  const p = m.placement;
  if (p == null) return '';
  if (p === 'cpu') return 'CPU';
  if (p === 'single') {
    const idx = m.gpus?.[0];
    if (idx == null) return 'GPU';
    const g = d?.gpu?.find((x) => x.index === idx);
    const name = g?.name ? shortGpuName(g.name) : '';
    return `GPU ${idx}${name ? ` (${name})` : ''}`;
  }
  if (p === 'split') {
    const parts = m.per_gpu?.length ? m.per_gpu : (m.gpus ?? []).map((i) => ({ index: i }));
    if (!parts.length) return t('split');
    return `${t('split')}: ${parts
      .map((x: { index: number; bytes?: number | null }) => `#${x.index}${x.bytes != null ? ` ${gb(x.bytes)} GB` : ''}`)
      .join(' + ')}`;
  }
  return '—';
}

/* ── the store: one poller shared by every subscriber ───────────────── */

const IDLE_MS = 5000;
const BUSY_MS = 1500;
const HISTORY = 24; // two minutes at the idle cadence
const VIS_KEY = 'faustus_studio_usage';

let last: Usage | null = null;
let failures = 0;
let history: number[] = [];
let timer: number | null = null;
let busy = false;
let subscribers = 0;
let visible = readVisible();
const listeners = new Set<() => void>();

function readVisible(): boolean {
  try {
    return window.localStorage.getItem(VIS_KEY) !== '0';
  } catch {
    return true;
  }
}
function emit() {
  for (const fn of listeners) fn();
}

async function tick(): Promise<void> {
  if (!visible) return;
  try {
    last = await getJson<Usage>('/api/system/usage');
    failures = 0;
    const util = poolOf(last).util;
    history = [...history, util ?? 0].slice(-HISTORY);
  } catch {
    failures += 1;
    if (failures > 3) last = null;
  }
  emit();
}

function schedule() {
  if (timer) window.clearInterval(timer);
  timer = subscribers > 0 && visible ? window.setInterval(() => void tick(), busy ? BUSY_MS : IDLE_MS) : null;
}

function onVisibility() {
  if (!document.hidden) void tick();
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  subscribers += 1;
  if (subscribers === 1) {
    document.addEventListener('visibilitychange', onVisibility);
    void tick();
    schedule();
  }
  return () => {
    listeners.delete(fn);
    subscribers -= 1;
    if (subscribers === 0) {
      document.removeEventListener('visibilitychange', onVisibility);
      schedule();
    }
  };
}

/** Faster while a reply streams, like the previous interface. */
export function setUsageBusy(active: boolean): void {
  if (active === busy) return;
  busy = active;
  schedule();
  if (active) void tick();
}

export function isUsageVisible(): boolean {
  return visible;
}
export function setUsageVisible(v: boolean): void {
  visible = v;
  try {
    window.localStorage.setItem(VIS_KEY, v ? '1' : '0');
  } catch {
    /* private mode */
  }
  schedule();
  if (v) void tick();
  emit();
}

export function refreshUsage(): Promise<void> {
  return tick();
}

let snapshot: { last: Usage | null; history: number[]; visible: boolean; busy: boolean } = { last, history, visible, busy };
function getSnapshot() {
  if (snapshot.last !== last || snapshot.history !== history || snapshot.visible !== visible || snapshot.busy !== busy) {
    snapshot = { last, history, visible, busy };
  }
  return snapshot;
}

export function useUsage(active = false): { last: Usage | null; history: number[]; visible: boolean; busy: boolean; intervalMs: number } {
  const s = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  useEffect(() => {
    setUsageBusy(active);
  }, [active]);
  return { ...s, intervalMs: s.busy ? BUSY_MS : IDLE_MS };
}

/** Ticks once a second so "12s left" and "updated 10:41:03" stay honest while the panel is open. */
export function useClock(on: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!on) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [on]);
  return now;
}

/** Kill an orphaned runner (re-checked as orphaned server-side first). */
export async function releaseOrphan(pid: number): Promise<void> {
  const r = await fetch('/api/system/gpu/orphans/release', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pid }),
  });
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try {
      msg = ((await r.json()) as { detail?: string }).detail ?? msg;
    } catch {
      /* not json */
    }
    throw new Error(msg);
  }
  await tick();
}
