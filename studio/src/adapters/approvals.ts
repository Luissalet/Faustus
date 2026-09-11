import { getJson } from './api';

/**
 * SEC-01 — approval cards over HTTP (`routes/approvals_routes.py`): what a
 * model or document could currently point to and say "I have permission"
 * (`active`, granted with uses left) and immediate revocation (`revoke`),
 * distinct from `pending` (a request nobody has decided on yet, already
 * read elsewhere in Studio's tool-approval flow).
 */

export interface ApprovalPlan {
  action: string;
  skill_id: string;
  skill_version: string;
  backend: string;
  recipients: string[];
  cost_units: number | null;
  secret_names: string[];
  permissions: Record<string, unknown>;
  output_kinds: string[];
  detail: string;
}

export interface Approval {
  id: string;
  plan: ApprovalPlan;
  plan_fingerprint: string;
  status: string;
  owner: string;
  requested_at: string;
  decided_at: string | null;
  decided_by: string;
  expires_at: string | null;
  uses_left: number;
  reason: string;
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

export async function listActiveApprovals(): Promise<Approval[]> {
  const d = await getJson<{ active?: Approval[] }>('/api/approvals/active');
  return d.active ?? [];
}

export async function revokeApproval(id: string, reason = ''): Promise<{ ok: boolean; reason?: string }> {
  const r = await ok(
    await fetch(`/api/approvals/${encodeURIComponent(id)}`, {
      method: 'DELETE',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason }),
    }),
    'approvals/revoke',
  );
  return (await r.json()) as { ok: boolean; reason?: string };
}
