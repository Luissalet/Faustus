import { ApiError } from './api';

/**
 * The two things a person can do to a running sub-agent (delegate_agents
 * worker) besides watching it: stop it alone, or steer it — a message the
 * worker reads before its next round. Same endpoints the legacy board uses
 * (routes/chat_routes.py, /api/chat/subagent/*).
 */

export async function stopWorker(childSessionId: string): Promise<boolean> {
  const response = await fetch(`/api/chat/subagent/stop/${encodeURIComponent(childSessionId)}`, {
    method: 'POST',
    credentials: 'same-origin',
  });
  if (!response.ok) throw new ApiError(`stop responded ${response.status}`, response.status);
  const body = (await response.json().catch(() => ({}))) as { stopped?: boolean };
  return Boolean(body.stopped);
}

/** Resolves to false when the worker is no longer running (404). */
export async function steerWorker(childSessionId: string, text: string): Promise<boolean> {
  const response = await fetch(`/api/chat/subagent/steer/${encodeURIComponent(childSessionId)}`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  });
  if (response.status === 404) return false;
  if (!response.ok) throw new ApiError(`steer responded ${response.status}`, response.status);
  return true;
}
