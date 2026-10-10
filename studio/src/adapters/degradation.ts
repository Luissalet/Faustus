import { useEffect, useState } from 'react';

/**
 * What Faustus is running without right now (`GET /api/diagnostics/degradation`).
 *
 * One entry per dependency (vector store, web search, notifications, email,
 * model endpoints, Sparks, MCP servers) with the state, the cause, since when,
 * a fix hint in both languages and the features that degrade. Informational:
 * nothing is blocked. Admin-only; a 403/404 hides the readout.
 */

export type DegradationState = 'ok' | 'degraded' | 'down' | 'unconfigured';

export interface DegradationItem {
  id: string;
  label: string;
  state: DegradationState;
  cause: string;
  since: string;
  lastChecked: string;
  latencyMs: number | null;
  hint: { es: string; en: string; command: string };
  affects: string[];
}

export interface DegradationReport {
  overall: DegradationState;
  generatedAt: string;
  items: DegradationItem[];
  /** degraded + down only: what the banner shows. */
  problems: DegradationItem[];
  unconfigured: number;
}

const STATES: DegradationState[] = ['ok', 'degraded', 'down', 'unconfigured'];

function stateOf(raw: unknown): DegradationState {
  return STATES.includes(raw as DegradationState) ? (raw as DegradationState) : 'degraded';
}

function item(raw: Record<string, unknown>): DegradationItem {
  const hint = (raw.fix_hint ?? {}) as Record<string, unknown>;
  return {
    id: String(raw.id ?? ''),
    label: String(raw.label ?? raw.id ?? ''),
    state: stateOf(raw.state),
    cause: String(raw.cause ?? ''),
    since: String(raw.since ?? ''),
    lastChecked: String(raw.last_checked ?? ''),
    latencyMs: typeof raw.latency_ms === 'number' ? raw.latency_ms : null,
    hint: { es: String(hint.es ?? ''), en: String(hint.en ?? ''), command: String(hint.command ?? '') },
    affects: Array.isArray(raw.affects) ? raw.affects.map(String) : [],
  };
}

export function parseDegradation(raw: Record<string, unknown>): DegradationReport {
  const list = Array.isArray(raw.items) ? (raw.items as Record<string, unknown>[]).map(item) : [];
  return {
    overall: stateOf(raw.overall),
    generatedAt: String(raw.generated_at ?? ''),
    items: list,
    problems: list.filter((i) => i.state === 'degraded' || i.state === 'down'),
    unconfigured: list.filter((i) => i.state === 'unconfigured').length,
  };
}

/** null when this account may not ask (403) or the route is not there. */
export async function degradationReport(signal?: AbortSignal, refresh = false): Promise<DegradationReport | null> {
  const r = await fetch(`/api/diagnostics/degradation${refresh ? '?refresh=1' : ''}`, { credentials: 'same-origin', signal });
  if (r.status === 403 || r.status === 404) return null;
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return parseDegradation((await r.json()) as Record<string, unknown>);
}

/** Polls while `on`; a 403 stops it for good. The server caches, so this is cheap. */
export function useDegradation(on: boolean): { report: DegradationReport | null; allowed: boolean; error: string | null } {
  const [report, setReport] = useState<DegradationReport | null>(null);
  const [allowed, setAllowed] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!on || !allowed) return;
    const ctrl = new AbortController();
    let timer = 0;
    const read = async () => {
      try {
        const next = await degradationReport(ctrl.signal);
        if (ctrl.signal.aborted) return;
        if (next === null) {
          setAllowed(false);
          return;
        }
        setReport(next);
        setError(null);
      } catch (err) {
        if (!ctrl.signal.aborted) setError((err as Error).message);
      }
      if (!ctrl.signal.aborted) timer = window.setTimeout(() => void read(), 30_000);
    };
    void read();
    return () => {
      ctrl.abort();
      window.clearTimeout(timer);
    };
  }, [on, allowed]);

  return { report, allowed, error };
}
