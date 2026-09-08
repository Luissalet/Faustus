import type { ModelRoute } from '../adapters/chat';

/** Distinguish identical names across providers without changing routing IDs. */
export function modelLabel(current: ModelRoute, routes: ModelRoute[]): string {
  const ambiguous = current.model === 'client-default'
    || routes.some(route => route.model === current.model && route.endpointId !== current.endpointId);
  return ambiguous && current.endpointName
    ? `${current.endpointName} · ${current.model}` : current.model;
}
