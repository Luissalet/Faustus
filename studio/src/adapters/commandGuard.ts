import { getJson } from './api';

/**
 * SEC-01 — `routes/command_guard_routes.py`'s allowlist (`/api/command-guard/
 * allowlist`), wired here for the first time: the route existed with no
 * Studio caller. A pattern on this list is a standing authority downgrade —
 * the command-guard classifier skips its own checks for anything matching
 * it — so this is deliberately the same "who can see/change it" shape as
 * the approvals screen it sits beside.
 */

export interface AllowlistEntry {
  pattern: string;
  kind: string;
  reason: string;
  added_by: string;
  created_at: string;
  expires_at?: string | null;
}

async function ok(r: Response, what: string): Promise<Response> {
  if (r.ok) return r;
  let msg = `${what}: HTTP ${r.status}`;
  try {
    const d = (await r.json()) as { detail?: unknown };
    if (typeof d.detail === 'string') msg = d.detail;
  } catch {
    /* not json */
  }
  throw new Error(msg);
}

export async function listCommandAllowlist(): Promise<AllowlistEntry[]> {
  const d = await getJson<{ allow?: AllowlistEntry[] }>('/api/command-guard/allowlist');
  return d.allow ?? [];
}

export async function addCommandAllowlistEntry(entry: { pattern: string; kind: string; reason: string; ttl_hours?: number | null }): Promise<AllowlistEntry> {
  const r = await ok(
    await fetch('/api/command-guard/allowlist', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(entry),
    }),
    'command-guard/allowlist',
  );
  const d = (await r.json()) as { entry: AllowlistEntry };
  return d.entry;
}

export async function removeCommandAllowlistEntry(pattern: string): Promise<void> {
  await ok(
    await fetch('/api/command-guard/allowlist', {
      method: 'DELETE',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pattern }),
    }),
    'command-guard/allowlist',
  );
}
