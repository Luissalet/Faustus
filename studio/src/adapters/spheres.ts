import { useCallback, useEffect, useState } from 'react';
import { CLIENT_API_VERSION, CLIENT_VERSION_HEADER, getJson } from './api';
import type { Lang } from '../i18n';

/**
 * The family hub's spheres (personal / work), through Faustus's own proxy.
 *
 * `GET /api/hoard/spheres` and `POST /api/hoard/spheres/active`
 * (routes/hoard_hub_routes.py, src/hoard_hub.py) never fail at the HTTP level
 * because of the hub: a hub that is down or refusing answers 200 with
 * `{ok: false, error}`. Here that is `null` — "there is no hub to show" — and
 * the header chip hides itself on it.
 */

export interface Sphere {
  id: string;
  name: { es: string; en: string };
  color: string;
}

export interface SphereState {
  active: string;
  spheres: Sphere[];
  hubUrl: string;
}

export const SPHERE_POLL_MS = 60_000;

function str(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

/** The proxy's answer as a state, or null when it is not a usable one. */
export function parseSpheres(raw: unknown): SphereState | null {
  if (!raw || typeof raw !== 'object') return null;
  const body = raw as Record<string, unknown>;
  if (body.ok !== true || !Array.isArray(body.spheres)) return null;
  const spheres: Sphere[] = [];
  for (const item of body.spheres) {
    if (!item || typeof item !== 'object') continue;
    const row = item as Record<string, unknown>;
    const id = str(row.id);
    if (!id) continue;
    const name = (row.name && typeof row.name === 'object' ? row.name : {}) as Record<string, unknown>;
    spheres.push({
      id,
      name: { es: str(name.es) || str(name.en) || id, en: str(name.en) || str(name.es) || id },
      color: str(row.color),
    });
  }
  if (!spheres.length) return null;
  return { active: str(body.active), spheres, hubUrl: str(body.hub_url) };
}

/** The sphere's name in the interface language. */
export function sphereName(sphere: Sphere, lang: Lang): string {
  return sphere.name[lang] || sphere.name.en || sphere.id;
}

export async function loadSpheres(signal?: AbortSignal): Promise<SphereState | null> {
  try {
    return parseSpheres(await getJson<unknown>('/api/hoard/spheres', signal));
  } catch {
    return null;
  }
}

export type SwitchResult = { state: SphereState } | { error: string };

export async function switchSphere(id: string): Promise<SwitchResult> {
  try {
    const response = await fetch('/api/hoard/spheres/active', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json', [CLIENT_VERSION_HEADER]: CLIENT_API_VERSION },
      body: JSON.stringify({ id }),
      signal: AbortSignal.timeout(8000),
    });
    const body = (await response.json().catch(() => null)) as Record<string, unknown> | null;
    const state = parseSpheres(body);
    if (state) return { state };
    const detail = body && (str(body.error) || str(body.detail));
    return { error: detail || `HTTP ${response.status}` };
  } catch {
    return { error: 'hub unreachable' };
  }
}

/**
 * The spheres, refreshed every minute and whenever the window regains focus.
 * `state` is null until the hub has answered once and again whenever it stops
 * answering, which is when the chip must not be shown.
 */
export function useSpheres(pollMs: number = SPHERE_POLL_MS): {
  state: SphereState | null;
  select: (id: string) => Promise<string | null>;
} {
  const [state, setState] = useState<SphereState | null>(null);

  useEffect(() => {
    let cancelled = false;
    let controller: AbortController | null = null;
    const refresh = async () => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
      controller?.abort();
      controller = new AbortController();
      const next = await loadSpheres(controller.signal);
      if (!cancelled && !controller.signal.aborted) setState(next);
    };
    void refresh();
    const id = window.setInterval(() => void refresh(), pollMs);
    const onFocus = () => void refresh();
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onFocus);
    return () => {
      cancelled = true;
      controller?.abort();
      window.clearInterval(id);
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onFocus);
    };
  }, [pollMs]);

  /** Resolves to an error text, or null when the switch went through. */
  const select = useCallback(async (id: string) => {
    const result = await switchSphere(id);
    if ('state' in result) {
      setState(result.state);
      return null;
    }
    return result.error;
  }, []);

  return { state, select };
}
