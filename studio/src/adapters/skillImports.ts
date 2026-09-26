import { ApiError, getJson } from './api';
import type { SecurityScanResult } from './integrations';

/**
 * Skills import review (ADP-25, `src/skill_import_review.py`,
 * `routes/skills_routes.py`): a skill discovered under a project's own
 * `.faustus|.agents|.claude/skills` folder is a manifest pinned to exact
 * bytes, never runnable until a human approves its current digest. See
 * docs/api/skills_review.md for the full contract.
 */

export interface SkillReviewStatus {
  state: 'unreviewed' | 'needs_review' | 'approved' | string;
  reason: string;
  approval: Record<string, unknown> | null;
}

export interface SkillReview {
  id: string;
  origin: string;
  version: string;
  digest: string;
  tools_required: string[];
  permissions: Record<string, unknown>;
  privilege_request: string[];
  security_scan: SecurityScanResult;
  status: SkillReviewStatus;
}

export interface SkillReviewErrorBody {
  error_class: string;
  detail?: string;
}

/** Every skill discovery finds in this project's workspace, each already
 * carrying its pre-scan risk and current gate status. */
export function listDiscoveredSkills(projectId: string): Promise<SkillReview[]> {
  return getJson<{ skills: SkillReview[] }>(`/api/skills/discovered?project_id=${encodeURIComponent(projectId)}`).then((d) => d.skills ?? []);
}

export function getSkillReview(projectId: string, id: string): Promise<SkillReview> {
  return getJson<SkillReview>(`/api/skills/${encodeURIComponent(id)}/review?project_id=${encodeURIComponent(projectId)}`);
}

export interface SkillApproval {
  ok: true;
  approval: Record<string, unknown>;
}

/** `override: true` acknowledges a CRITICAL pre-scan finding (SEC-09) and
 * approves anyway. Ignored unless the scan is actually critical. */
export async function approveSkillImport(projectId: string, id: string, override = false): Promise<SkillApproval> {
  const response = await fetch(`/api/skills/${encodeURIComponent(id)}/approve`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_id: projectId, override }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ApiError((body as SkillReviewErrorBody).detail || (body as SkillReviewErrorBody).error_class || `approve responded ${response.status}`, response.status);
  }
  return body as SkillApproval;
}

export interface SkillDiff {
  has_approved: boolean;
  diff: string;
  reason: string;
}

export function getSkillDiff(projectId: string, id: string): Promise<SkillDiff> {
  return getJson<SkillDiff>(`/api/skills/${encodeURIComponent(id)}/diff?project_id=${encodeURIComponent(projectId)}`);
}
