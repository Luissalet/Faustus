import { useEffect, useState } from 'react';
import { ApiError, getJson } from './api';

/**
 * Period budget for Studio Vitals (`GET /api/budget/period`).
 *
 * Same numbers as Cassandra's "Agents running" card and the MCP
 * `budget_period` tool: per-provider spend against the day/week target,
 * the pace line, local GPU seconds, the unattended breaker and whether
 * an interactive turn is holding unattended work. Honest empties: no
 * target, auth refusal, or a missing answer — never a fake zero meter.
 */

export interface PeriodPace {
  elapsedFraction: number;
  allowedFraction: number;
  allowed: number | null;
  band: number;
}

export interface PeriodRow {
  provider: string;
  window: string;
  metric: string;
  used: number;
  target: number | null;
  pace: PeriodPace;
  paused: boolean;
  pausedUntil: number | null;
  reason: string;
  unpricedTokens: number;
  calls: number;
  windowResetsAt: number | null;
}

export interface PeriodBreaker {
  enabled: boolean;
  open: boolean;
  consecutiveFailures: number;
  threshold: number;
  pauseUntil: number | null;
  lastFailureKind: string | null;
}

export interface PeriodBudget {
  now: number;
  window: string;
  windowStart: number | null;
  windowResetsAt: number | null;
  enabled: boolean;
  interactiveActive: boolean;
  providers: PeriodRow[];
  gpu: PeriodRow | null;
  breaker: PeriodBreaker;
  cooldowns: { endpoint: string; status: string; until: number | null; hits: number }[];
  hostedSeen: { provider: string }[];
}

function num(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string' && v.trim() !== '') {
    const n = Number(v);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

function paceOf(raw: unknown): PeriodPace {
  const p = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  return {
    elapsedFraction: num(p.elapsed_fraction) ?? 0,
    allowedFraction: num(p.allowed_fraction) ?? 0,
    allowed: num(p.allowed),
    band: num(p.band) ?? 0,
  };
}

function rowOf(raw: Record<string, unknown>): PeriodRow {
  return {
    provider: String(raw.provider ?? ''),
    window: String(raw.window ?? ''),
    metric: String(raw.metric ?? ''),
    used: num(raw.used) ?? 0,
    target: num(raw.target),
    pace: paceOf(raw.pace),
    paused: Boolean(raw.paused),
    pausedUntil: num(raw.paused_until),
    reason: String(raw.reason ?? ''),
    unpricedTokens: Math.max(0, Math.floor(num(raw.unpriced_tokens) ?? 0)),
    calls: Math.max(0, Math.floor(num(raw.calls) ?? 0)),
    windowResetsAt: num(raw.window_resets_at),
  };
}

function breakerOf(raw: unknown): PeriodBreaker {
  const b = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  const last = (b.last_failure && typeof b.last_failure === 'object' ? b.last_failure : {}) as Record<string, unknown>;
  return {
    enabled: Boolean(b.enabled),
    open: Boolean(b.open),
    consecutiveFailures: Math.max(0, Math.floor(num(b.consecutive_failures) ?? 0)),
    threshold: Math.max(0, Math.floor(num(b.threshold) ?? 0)),
    pauseUntil: num(b.pause_until),
    lastFailureKind: typeof last.kind === 'string' ? last.kind : typeof b.last_failure_kind === 'string' ? b.last_failure_kind : null,
  };
}

/** Pure parse for checks and the screen. Unknown shapes become empty fields, not throw. */
export function parsePeriodBudget(raw: Record<string, unknown>): PeriodBudget {
  const providers = Array.isArray(raw.providers)
    ? (raw.providers as Record<string, unknown>[]).filter((r) => r && typeof r === 'object').map(rowOf)
    : [];
  const gpuRaw = raw.gpu;
  const hosted = Array.isArray(raw.hosted_seen)
    ? (raw.hosted_seen as Record<string, unknown>[])
        .filter((h) => h && typeof h === 'object')
        .map((h) => ({ provider: String(h.provider ?? '') }))
        .filter((h) => h.provider)
    : [];
  const cools = Array.isArray(raw.cooldowns)
    ? (raw.cooldowns as Record<string, unknown>[])
        .filter((c) => c && typeof c === 'object')
        .map((c) => ({
          endpoint: String(c.endpoint ?? ''),
          status: String(c.status ?? ''),
          until: num(c.until ?? c.pause_until),
          hits: Math.max(0, Math.floor(num(c.hits) ?? 0)),
        }))
    : [];
  return {
    now: num(raw.now) ?? 0,
    window: String(raw.window ?? ''),
    windowStart: num(raw.window_start),
    windowResetsAt: num(raw.window_resets_at),
    enabled: Boolean(raw.enabled),
    interactiveActive: Boolean(raw.interactive_active),
    providers,
    gpu: gpuRaw && typeof gpuRaw === 'object' ? rowOf(gpuRaw as Record<string, unknown>) : null,
    breaker: breakerOf(raw.breaker),
    cooldowns: cools,
    hostedSeen: hosted,
  };
}

/** Meter level from used vs pace allowance (and hard pause). */
export function rowLevel(row: PeriodRow): '' | 'warm' | 'hot' {
  if (row.paused) return 'hot';
  const target = row.target;
  if (target == null || target <= 0) return '';
  const usedFrac = row.used / target;
  const allowed = row.pace.allowedFraction;
  if (usedFrac > allowed + 1e-9) return 'hot';
  if (usedFrac > Math.max(0, allowed - (row.pace.band || 0))) return 'warm';
  return '';
}

/** `loc` is the interface locale, so 10400 reads "10.400" in Spanish. */
export function formatAmount(metric: string, value: number | null | undefined, loc?: string): string {
  const v = typeof value === 'number' && Number.isFinite(value) ? value : 0;
  if (metric === 'usd') {
    if (v < 1) return `$${v.toFixed(4).replace(/0+$/, '').replace(/\.$/, '')}`;
    return `$${v.toFixed(2)}`;
  }
  if (metric === 'gpu_seconds') return `${Math.round(v).toLocaleString(loc)} s`;
  return Math.round(v).toLocaleString(loc);
}

export function rowLabel(row: PeriodRow): string {
  const metric =
    row.metric === 'usd' ? 'USD' : row.metric === 'tokens' ? 'tokens' : row.metric === 'gpu_seconds' ? 'GPU seconds' : row.metric;
  return `${row.provider || '?'} · ${metric}`;
}

/**
 * null when this account may not read the route (401/403) or it is gone (404).
 * Other failures throw so the section can say why.
 */
export async function fetchPeriodBudget(signal?: AbortSignal): Promise<PeriodBudget | null> {
  try {
    const raw = await getJson<Record<string, unknown>>('/api/budget/period', signal);
    return parsePeriodBudget(raw && typeof raw === 'object' ? raw : {});
  } catch (err) {
    if (err instanceof ApiError && (err.status === 401 || err.status === 403 || err.status === 404)) return null;
    throw err;
  }
}

/**
 * Polls only while `on` (popover open). A 401/403 stops polling rather than
 * retrying a refusal every half minute.
 */
export function usePeriodBudget(on: boolean): {
  budget: PeriodBudget | null;
  allowed: boolean;
  error: string | null;
  loading: boolean;
} {
  const [budget, setBudget] = useState<PeriodBudget | null>(null);
  const [allowed, setAllowed] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!on || !allowed) return;
    const ctrl = new AbortController();
    let timer = 0;
    const read = async () => {
      try {
        const next = await fetchPeriodBudget(ctrl.signal);
        if (ctrl.signal.aborted) return;
        if (next === null) {
          setAllowed(false);
          setBudget(null);
          setError(null);
          setLoading(false);
          return;
        }
        setBudget(next);
        setError(null);
        setLoading(false);
      } catch (err) {
        if (!ctrl.signal.aborted) {
          setError((err as Error).message || 'period budget unread');
          setLoading(false);
        }
      }
      if (!ctrl.signal.aborted) timer = window.setTimeout(() => void read(), 30_000);
    };
    void read();
    return () => {
      ctrl.abort();
      window.clearTimeout(timer);
    };
  }, [on, allowed]);

  return { budget, allowed, error, loading };
}
