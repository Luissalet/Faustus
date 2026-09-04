import { ApiError, asArray, getJson } from './api';

/**
 * The files a turn touched: diff, revert, the turn's checkpoint, and a
 * commit to the user's own repository. All `routes/workspace_routes.py`,
 * all admin-gated there; nothing here decides anything about safety.
 */

const q = (params: Record<string, string>) =>
  Object.entries(params)
    .filter(([, v]) => v !== '')
    .map(([k, v]) => `${k}=${encodeURIComponent(v)}`)
    .join('&');

async function post(path: string, body?: unknown): Promise<Record<string, unknown>> {
  const response = await fetch(path, {
    method: 'POST',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = '';
    try {
      detail = String(((await response.json()) as { detail?: unknown }).detail ?? '');
    } catch {
      detail = '';
    }
    throw new ApiError(detail || `${path} responded ${response.status}`, response.status);
  }
  try {
    return (await response.json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

export interface FileDiff {
  git: boolean;
  diff: string;
  status: string | null;
  rel: string;
}

export async function fileDiff(workspace: string, path: string, checkpoint?: string): Promise<FileDiff> {
  const raw = await getJson<Partial<FileDiff>>(`/api/workspace/file_diff?${q({ workspace, path, checkpoint: checkpoint ?? '' })}`);
  return { git: Boolean(raw.git), diff: raw.diff ?? '', status: raw.status ?? null, rel: raw.rel ?? path };
}

export async function revertFile(workspace: string, path: string, checkpoint?: string): Promise<string> {
  const res = await post(`/api/workspace/revert?${q({ workspace, path, checkpoint: checkpoint ?? '' })}`);
  return String(res.action ?? 'restored');
}

export interface CheckpointChange {
  path: string;
  status: string;
}

export async function checkpointChanges(workspace: string, sha: string): Promise<CheckpointChange[]> {
  const raw = await getJson<{ changed?: unknown }>(`/api/workspace/checkpoint/changes?${q({ workspace, sha })}`);
  return asArray<Record<string, unknown>>(raw.changed).map((c) => ({
    path: String(c.path ?? c.rel ?? ''),
    status: String(c.status ?? ''),
  }));
}

export async function restoreCheckpoint(
  workspace: string,
  sha: string,
  paths?: string[],
): Promise<{ restored: number; deleted: number; failed: number }> {
  const res = await post(`/api/workspace/checkpoint/restore?${q({ workspace, sha })}`, paths ? { paths } : undefined);
  const count = (v: unknown) => (Array.isArray(v) ? v.length : typeof v === 'number' ? v : 0);
  return { restored: count(res.restored), deleted: count(res.deleted), failed: count(res.failed) };
}

export interface Checkpoint {
  sha: string;
  createdAt?: string;
  reason?: string;
}

export async function listCheckpoints(workspace: string): Promise<Checkpoint[]> {
  const raw = await getJson<{ checkpoints?: unknown }>(`/api/workspace/checkpoint/list?${q({ workspace, limit: '30' })}`);
  return asArray<Record<string, unknown>>(raw.checkpoints).map((c) => ({
    sha: String(c.sha ?? c.id ?? ''),
    createdAt: typeof c.created_at === 'string' ? c.created_at : typeof c.date === 'string' ? c.date : undefined,
    reason: typeof c.reason === 'string' ? c.reason : typeof c.message === 'string' ? c.message : undefined,
  }));
}

export async function commitProposal(workspace: string, paths: string[], text: string): Promise<{ git: boolean; message: string }> {
  const raw = await getJson<{ git?: boolean; message?: string }>(
    `/api/workspace/commit/proposal?${q({ workspace, paths: paths.join('\n'), text: text.slice(0, 2000), language: 'es' })}`,
  );
  return { git: Boolean(raw.git), message: raw.message ?? '' };
}

export async function commitFiles(workspace: string, paths: string[], message: string): Promise<string> {
  const res = await post(`/api/workspace/commit?${q({ workspace })}`, { paths, message });
  return String(res.sha ?? res.commit ?? 'ok');
}
