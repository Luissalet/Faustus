import { useEffect, useState } from 'react';

/**
 * The services Faustus leans on, and whether they are actually there.
 *
 * `/api/diagnostics/services` probes ChromaDB, SearXNG, ntfy, email and the
 * model endpoints without disturbing them, and says why in words plus a
 * hint with a command to run. The failure it exists for is the quiet one:
 * Docker Desktop was closed, so the vector store went keyword-only, and
 * nothing said so — search just got worse. `/reconnect` re-establishes the
 * ChromaDB-backed stores and re-probes, so that case is recovered in place
 * without a restart.
 *
 * Admin-only, like the rest of diagnostics: a 403 means this account is not
 * an admin, and the readout hides rather than nagging.
 */

export type ServiceState = 'ok' | 'degraded' | 'down' | 'disabled' | 'unknown';

export interface ServiceHint {
  text: string;
  command?: string;
}

export interface Service {
  name: string;
  status: ServiceState;
  detail: string;
  hint?: ServiceHint;
}

export interface ServiceHealth {
  overall: ServiceState;
  services: Service[];
  /** Present only in a reconnect's answer: what it managed to bring back. */
  recovery?: { reconnected?: string[]; failed?: string[] };
}

/** Names the server uses, in the words a person would. */
export const SERVICE_LABEL: Record<string, string> = {
  chromadb: 'Vector store',
  searxng: 'Web search',
  ntfy: 'Notifications',
  email: 'Email',
  providers: 'Model endpoints',
};

const STATES: ServiceState[] = ['ok', 'degraded', 'down', 'disabled', 'unknown'];

function stateOf(raw: unknown): ServiceState {
  return STATES.includes(raw as ServiceState) ? (raw as ServiceState) : 'unknown';
}

function parse(raw: Record<string, unknown>): ServiceHealth {
  const list = Array.isArray(raw.services) ? (raw.services as Record<string, unknown>[]) : [];
  const recovery = raw.recovery as Record<string, unknown> | undefined;
  return {
    overall: stateOf(raw.overall),
    services: list.map((s) => {
      const hint = s.hint as Record<string, unknown> | undefined;
      return {
        name: String(s.name ?? ''),
        status: stateOf(s.status),
        detail: String(s.detail ?? ''),
        hint: hint && typeof hint.text === 'string' ? { text: hint.text, command: typeof hint.command === 'string' ? hint.command : undefined } : undefined,
      };
    }),
    recovery: recovery
      ? {
          reconnected: Array.isArray(recovery.reconnected) ? recovery.reconnected.map(String) : undefined,
          failed: Array.isArray(recovery.failed) ? recovery.failed.map(String) : undefined,
        }
      : undefined,
  };
}

/** null when this account may not ask (403) or the route is not there. */
export async function serviceHealth(signal?: AbortSignal): Promise<ServiceHealth | null> {
  const r = await fetch('/api/diagnostics/services', { credentials: 'same-origin', signal });
  if (r.status === 403 || r.status === 404) return null;
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return parse((await r.json()) as Record<string, unknown>);
}

export async function reconnectServices(): Promise<ServiceHealth | null> {
  const r = await fetch('/api/diagnostics/services/reconnect', { method: 'POST', credentials: 'same-origin' });
  if (r.status === 403 || r.status === 404) return null;
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return parse((await r.json()) as Record<string, unknown>);
}

/**
 * Polls while `on` — which is while the panel is open. The probes are cheap
 * but they are still probes: nothing asks for them with nobody looking.
 * A 403 stops the polling for good rather than retrying every half minute.
 */
export function useServiceHealth(on: boolean): { health: ServiceHealth | null; allowed: boolean; error: string | null } {
  const [health, setHealth] = useState<ServiceHealth | null>(null);
  const [allowed, setAllowed] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!on || !allowed) return;
    const ctrl = new AbortController();
    let timer = 0;
    const read = async () => {
      try {
        const next = await serviceHealth(ctrl.signal);
        if (ctrl.signal.aborted) return;
        if (next === null) {
          setAllowed(false);
          return;
        }
        setHealth(next);
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

  return { health, allowed, error };
}
