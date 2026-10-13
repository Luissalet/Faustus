import { getJson } from './api';
import type { ModelRoute } from './chat';

/** Read the same owner-scoped default the native chat API resolves. */
export async function configuredDefaultRoute(signal?: AbortSignal): Promise<ModelRoute | null> {
  const raw = await getJson<{ endpoint_id?: string; endpoint_url?: string; model?: string; missing?: boolean }>('/api/default-chat', signal);
  if (!raw.model && !raw.endpoint_id) return null;
  return { id: `${raw.endpoint_id ?? ''}::${raw.model ?? ''}`, model: raw.model ?? '',
    endpointId: raw.endpoint_id ?? '', endpointName: 'Configured default',
    endpointUrl: raw.endpoint_url ?? '', kind: 'configured-default', missing: raw.missing === true };
}

export function chooseStudioRoute(routes: ModelRoute[], pickedId: string | null,
  remembered: Partial<ModelRoute>, configured: ModelRoute | null, defaultLoaded: boolean): ModelRoute | null {
  const selected = routes.find((r) => r.id === pickedId);
  if (selected) return selected;
  if (pickedId && remembered.id === pickedId && remembered.model) {
    return { id: pickedId, model: remembered.model, endpointId: remembered.endpointId ?? '',
      endpointName: remembered.endpointName ?? '', endpointUrl: remembered.endpointUrl ?? '', kind: remembered.kind ?? '', missing: true };
  }
  if (!defaultLoaded) return null;
  if (configured) {
    const exact = routes.find((r) => r.model === configured.model && (!configured.endpointId || r.endpointId === configured.endpointId));
    return exact ?? { ...configured, missing: true };
  }
  return routes[0] ?? null;
}
