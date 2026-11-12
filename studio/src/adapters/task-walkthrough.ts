export interface Recording {
  job_id: string;
  state: string;
  message?: string;
  progress?: number;
  video_url?: string;
  error?: string;
}

export async function walkthrough(body: unknown): Promise<Recording> {
  const response = await fetch('/api/task-walkthrough', {
    method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body), signal: AbortSignal.timeout(40000),
  });
  const result = await response.json() as Recording & { detail?: string };
  if (!response.ok || result.error) throw new Error(result.error ?? result.detail ?? `Prospero: HTTP ${response.status}`);
  return result;
}
