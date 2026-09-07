import { ApiError } from './api';

export type OfficialRunner = 'codex' | 'claude';
export type ConnectionState = 'connected' | 'login_required' | 'configuration_conflict' | 'not_installed' | 'timeout' | 'unverified';
export interface RunnerConnection {
  state: ConnectionState;
  authMethod: 'subscription' | 'api' | 'none' | 'unknown';
  enabled: boolean;
  overrides: string[];
}

export function parseRunnerConnection(raw: unknown, key: OfficialRunner): RunnerConnection {
  if (!raw || typeof raw !== 'object' || !('connection' in raw)) throw new Error('invalid_connection_response');
  const data = raw.connection;
  if (!data || typeof data !== 'object' || !('runner' in data) || data.runner !== key) throw new Error('invalid_connection_response');
  const d = data as Record<string, unknown>;
  const method = typeof d.auth_method === 'string' && ['subscription', 'api', 'none'].includes(d.auth_method) ? d.auth_method as RunnerConnection['authMethod'] : 'unknown';
  const states: ConnectionState[] = ['connected', 'login_required', 'configuration_conflict', 'not_installed', 'timeout', 'unverified'];
  let state: ConnectionState = states.includes(d.state as ConnectionState) ? d.state as ConnectionState : 'unverified';
  if (state === 'connected' && (d.authenticated !== true || d.installed !== true || !['subscription', 'api'].includes(method))) state = 'unverified';
  return {
    state, authMethod: method, enabled: d.external_runners_enabled === true,
    overrides: Array.isArray(d.environment_override_names) ? d.environment_override_names.filter((s): s is string => typeof s === 'string' && /^[A-Z][A-Z0-9_]{0,79}$/.test(s)).slice(0, 16) : [],
  };
}

export async function checkRunnerConnection(key: OfficialRunner, signal: AbortSignal): Promise<RunnerConnection> {
  const response = await fetch(`/api/agent-runners/${key}/connection`, {signal, credentials: 'same-origin', headers: {Accept: 'application/json'}});
  // Do not echo raw diagnostics or proxy response bodies into the interface.
  if (!response.ok) throw new ApiError('connection_check_failed', response.status);
  return parseRunnerConnection(await response.json(), key);
}
