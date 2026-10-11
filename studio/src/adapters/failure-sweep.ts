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

export type FailureSweepCoverage = 'complete' | 'partial';
export type HubPublishStatus = 'delivered' | 'skipped' | 'failed' | 'queued';

export interface FailureSweepReport {
  days: number;
  since: string;
  owner: string;
  turns_scanned: number;
  hit_count: number;
  signature_count: number;
  by_signature: FailureSweepGroup[];
  truncated?: boolean;
  coverage?: FailureSweepCoverage;
  next_cursor?: string | null;
  page_limit?: number;
  rows_read?: number;
  hub_publish?: HubPublishStatus;
  generated_at?: string;
}

export function loadFailureSweep(
  days = 1,
  publish = false,
  opts?: { cursor?: string; pageLimit?: number },
): Promise<FailureSweepReport> {
  const q = new URLSearchParams({
    days: String(days),
    publish: publish ? '1' : '0',
  });
  if (opts?.cursor) q.set('cursor', opts.cursor);
  if (opts?.pageLimit && opts.pageLimit > 0) q.set('page_limit', String(opts.pageLimit));
  return getJson<FailureSweepReport>(`/api/diagnostics/failure-sweep?${q}`);
}

/** Merge a continuation page into an accumulated report (same rules as the Python helper). */
export function mergeFailureSweep(
  base: FailureSweepReport,
  next: FailureSweepReport,
): FailureSweepReport {
  const map = new Map<string, FailureSweepGroup>();
  const order: string[] = [];
  for (const g of [...(base.by_signature || []), ...(next.by_signature || [])]) {
    const key = `${g.signature}\0${g.tool || ''}`;
    const prev = map.get(key);
    if (!prev) {
      map.set(key, {
        signature: g.signature,
        tool: g.tool || '',
        count: g.count || 0,
        examples: [...(g.examples || [])].slice(0, 3),
      });
      order.push(key);
    } else {
      prev.count += g.count || 0;
      for (const ex of g.examples || []) {
        if (prev.examples.length >= 3) break;
        prev.examples.push(ex);
      }
    }
  }
  const groups = order.map((k) => map.get(k)!).sort(
    (a, b) => b.count - a.count || a.signature.localeCompare(b.signature) || a.tool.localeCompare(b.tool),
  );
  const coverage = next.coverage || (next.truncated ? 'partial' : 'complete');
  return {
    ...base,
    ...next,
    turns_scanned: (base.turns_scanned || 0) + (next.turns_scanned || 0),
    hit_count: (base.hit_count || 0) + (next.hit_count || 0),
    rows_read: (base.rows_read || 0) + (next.rows_read || 0),
    signature_count: groups.length,
    by_signature: groups,
    coverage,
    truncated: coverage === 'partial',
    next_cursor: next.next_cursor,
    hub_publish: next.hub_publish ?? base.hub_publish,
  };
}
