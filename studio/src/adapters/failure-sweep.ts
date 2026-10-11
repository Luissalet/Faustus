/** Failure-signature sweep over every turn (radar #498 / #276). */
import { getJson } from './api';

export interface FailureSweepExample {
  session_id: string;
  message_id: string;
  run_id?: string;
  detail?: string;
}

export interface FailureSweepGroup {
  signature: string;
  tool: string;
  count: number;
  examples: FailureSweepExample[];
}

export interface FailureSweepReport {
  days: number;
  since: string;
  owner: string;
  turns_scanned: number;
  hit_count: number;
  signature_count: number;
  by_signature: FailureSweepGroup[];
  truncated?: boolean;
  generated_at?: string;
}

export function loadFailureSweep(days = 1, publish = false): Promise<FailureSweepReport> {
  const q = new URLSearchParams({
    days: String(days),
    publish: publish ? '1' : '0',
  });
  return getJson<FailureSweepReport>(`/api/diagnostics/failure-sweep?${q}`);
}
