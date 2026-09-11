import type { ModelRoute } from '../adapters/chat';

/** Distinguish identical names across providers without changing routing IDs. */
export function modelLabel(current: ModelRoute, routes: ModelRoute[]): string {
  const ambiguous = current.model === 'client-default'
    || routes.some(route => route.model === current.model && route.endpointId !== current.endpointId);
  return ambiguous && current.endpointName
    ? `${current.endpointName} · ${current.model}` : current.model;
}

/**
 * Whether the session's current model still resolves against the live
 * routes list — false once its endpoint/model pair has been unloaded,
 * disconnected, or renamed out from under it, so the picker can flag a
 * stale selection instead of silently rendering a label for a route that
 * no longer exists.
 */
export function isInstalled(current: ModelRoute, routes: ModelRoute[]): boolean {
  return routes.some(route => route.endpointId === current.endpointId && route.model === current.model);
}
