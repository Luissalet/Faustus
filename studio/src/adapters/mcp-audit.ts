import { useCallback, useEffect, useState } from 'react';

export interface McpAuditFinding {
  code: string;
  severity: 'high' | 'medium' | 'low';
  server_id: string;
  server: string;
  enabled: boolean;
  message: string;
  detail?: string;
  fix?: string;
}

export interface McpAuditReport {
  servers: number;
  with_findings: number;
  findings: McpAuditFinding[];
  per_server: { id: string; findings: number; worst: string }[];
}

/** Configuration only: the preview endpoint never persists or launches it. */
export type McpAuditConfig = {
  id?: string; name?: string; transport: string; command?: string | null;
  args?: string[] | string; env?: Record<string, string> | string; url?: string | null;
  inherit_env?: boolean; is_enabled?: boolean;
};

export async function readMcpAudit(config?: McpAuditConfig, signal?: AbortSignal): Promise<McpAuditReport> {
  const r = await fetch(config ? '/api/mcp/audit/preview' : '/api/mcp/audit', {
    credentials: 'same-origin', signal,
    ...(config ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(config) } : {}),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json() as Promise<McpAuditReport>;
}

/** Aborts old drafts so a slow response never attaches to a newer configuration. */
export function useMcpAudit(config?: McpAuditConfig) {
  const serialized = config ? JSON.stringify(config) : '';
  const [revision, setRevision] = useState(0);
  const [report, setReport] = useState<McpAuditReport | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);
  const refresh = useCallback(() => setRevision((n) => n + 1), []);
  useEffect(() => {
    const ctrl = new AbortController();
    setReport(null);
    setError(false);
    setLoading(true);
    const timer = window.setTimeout(() => {
      const draft = serialized ? JSON.parse(serialized) as McpAuditConfig : undefined;
      readMcpAudit(draft, ctrl.signal).then((next) => {
        if (!ctrl.signal.aborted) setReport(next);
      }).catch(() => {
        if (!ctrl.signal.aborted) setError(true);
      }).finally(() => {
        if (!ctrl.signal.aborted) setLoading(false);
      });
    }, serialized ? 300 : 0);
    return () => { ctrl.abort(); window.clearTimeout(timer); };
  }, [serialized, revision]);
  return { report, error, loading, refresh };
}
