import { ApiError, getJson, responseReason } from './api';

export interface ShellResult {
  session_id: string;
  result: { output?: string; stdout?: string; stderr?: string; error?: string; exit_code?: number; blocked?: boolean };
  approval_id: string | null;
}
export interface ShellPending { approval_id: string; session_id: string; command: string; reason: string }

async function post(path: string, body: unknown): Promise<ShellResult> {
  const response = await fetch(path, { method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!response.ok) throw new ApiError(await responseReason(response, 'The shell command could not run.'), response.status);
  return await response.json() as ShellResult;
}

export const shellCommand = (body: { command: string; session_id?: string; workspace?: string;
  project_id?: string; autonomy_preset?: string }) => post('/api/shell/command', body);
export const shellApproval = (approval_id: string, decision: 'approve_action' | 'deny') =>
  post('/api/shell/command/approval', { approval_id, decision });
export const pendingShellCommand = (session_id: string) =>
  getJson<{ approval: ShellPending | null }>(`/api/shell/command/pending?session_id=${encodeURIComponent(session_id)}`);
