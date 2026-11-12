import { getJson } from './api';

export type DestinationScope = 'machine' | 'network' | 'external' | 'unknown';
export interface DataRoute {
  id: string; category: string; name: string; address: string; scope: DestinationScope;
  enabled: boolean; data: string; provider?: string; models?: string[]; roles?: string[]; user_override_count?: number;
  conversations?: { id: string; name: string; model: string; archived: boolean; project_id: string | null; project_name: string | null }[];
}
export interface DataBoundaryReport {
  version: number; computed_at: string; profile: string; routes: DataRoute[];
  projects: { id: string; name: string; privacy_profile?: string | null }[];
  local: string[]; limits: string[]; missing_sources: string[];
}
export const loadDataBoundaryReport = (signal?: AbortSignal) => getJson<DataBoundaryReport>('/api/privacy/report', signal);
export function attachmentDestination(endpointId: string | null, sessionId: string | null, signal?: AbortSignal) {
  const query = new URLSearchParams();
  if (endpointId) query.set('endpoint_id', endpointId);
  if (sessionId) query.set('session_id', sessionId);
  return getJson<{ address: string; scope: DestinationScope; warning: boolean }>(`/api/privacy/attachment-destination?${query}`, signal);
}
