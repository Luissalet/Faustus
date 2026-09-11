import { useCallback, useEffect, useState } from 'react';
import { Button, Dialog, EmptyState, Skeleton } from '../../components';
import { getJson } from '../../adapters/api';
import { changeWorkflow } from '../../adapters/activity';
import { t } from '../../i18n';
import { PlanGraph, type NodeMark, type PlanGraphNode } from './PlanGraph';

/**
 * W2-E (CMP-07) — "Ejecución real autorizada": the same `PlanGraph` design
 * mode draws, with a real run's node statuses superimposed
 * (`RUN_STATUS_MARK`). Starting a run is one explicit, confirmed action —
 * `POST /api/workflows/runs` is the existing route
 * (`routes/workflows_routes.py::create_run`), never called silently. Once a
 * run exists, `changeWorkflow` (`adapters/activity.ts`, already used by
 * `Activity.tsx`) drives it — no second implementation of advance/cancel.
 */

interface RunState {
  status: string;
  reason: string;
  definitionNodes: PlanGraphNode[];
  nodeStatus: Record<string, string>;
}

async function fetchRunState(runId: string, signal?: AbortSignal): Promise<RunState> {
  const body = await getJson<{
    ok?: boolean;
    run?: Record<string, unknown>;
    definition?: { nodes?: Record<string, unknown>[] };
    nodes?: Record<string, Record<string, unknown>>;
  }>(`/api/workflows/runs/${encodeURIComponent(runId)}`, signal);
  const rawNodes = Array.isArray(body.definition?.nodes) ? (body.definition!.nodes as Record<string, unknown>[]) : [];
  const nodeStatus: Record<string, string> = {};
  for (const [id, row] of Object.entries(body.nodes ?? {})) nodeStatus[id] = String(row.status ?? '');
  return {
    status: String(body.run?.status ?? ''),
    reason: String(body.run?.reason ?? ''),
    definitionNodes: rawNodes.map((n) => ({
      id: String(n.id), type: String(n.type), title: String(n.title || n.id),
      needs: Array.isArray(n.needs) ? n.needs.map(String) : [],
    })),
    nodeStatus,
  };
}

const RUN_STATUS_MARK: Record<string, NodeMark> = {
  completed: 'run_completed',
  failed: 'run_failed',
  running: 'run_running',
  paused: 'run_paused',
  pending: 'run_pending',
  skipped: 'not_taken',
  cancelled: 'run_failed',
};

async function startRun(definition: Record<string, unknown>): Promise<string> {
  const response = await fetch('/api/workflows/runs', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ definition, advance: true }),
    signal: AbortSignal.timeout(20000),
  });
  const body = await response.json().catch(() => ({}) as Record<string, unknown>);
  if (!response.ok || typeof body.run_id !== 'string' || !body.run_id) {
    throw new Error(typeof body.detail === 'string' ? body.detail : t('The server refused to start this run.'));
  }
  return body.run_id;
}

export interface RunOverlayProps {
  definition: Record<string, unknown> | null;
  runId: string | null;
  onRunStarted: (runId: string) => void;
  selectedNodeId: string | null;
  onSelectNode: (id: string) => void;
}

export function RunOverlay({ definition, runId, onRunStarted, selectedNodeId, onSelectNode }: RunOverlayProps) {
  const [state, setState] = useState<RunState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);

  const refresh = useCallback(async () => {
    if (!runId) {
      setState(null);
      return;
    }
    setError(null);
    try {
      setState(await fetchRunState(runId));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [runId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function confirmStart() {
    if (!definition) return;
    setBusy(true);
    setError(null);
    try {
      const id = await startRun(definition);
      setConfirming(false);
      onRunStarted(id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function act(action: 'advance' | 'cancel', nodeId?: string) {
    if (!runId) return;
    setBusy(true);
    setError(null);
    try {
      await changeWorkflow(runId, action, nodeId);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  if (!runId) {
    return (
      <div className="fs-runoverlay" data-testid="run-overlay-unstarted">
        <EmptyState
          title={t('Real, authorized execution')}
          body={t('This starts an actual run of the current definition — effectful nodes (skills, deliveries) can run for real. Nothing happens until you confirm.')}
          primaryAction={definition ? { label: t('Start run'), onClick: () => setConfirming(true) } : undefined}
        />
        <Dialog
          open={confirming}
          onOpenChange={setConfirming}
          title={t('Start a real run?')}
          description={t('This calls POST /api/workflows/runs against the live engine. Effectful nodes can run for real.')}
          testId="run-overlay-confirm"
          footer={
            <>
              <Button variant="ghost" label={t('Cancel')} onClick={() => setConfirming(false)} testId="run-overlay-confirm-cancel" />
              <Button variant="primary" label={t('Start for real')} onClick={() => void confirmStart()} loading={busy} testId="run-overlay-confirm-go" />
            </>
          }
        />
        {error && <p className="fs-runoverlay__error" role="alert">{error}</p>}
      </div>
    );
  }

  if (!state && !error) return <Skeleton label={t('Loading the run')} count={3} height="52px" />;
  if (error) {
    return (
      <EmptyState
        tone="error"
        title={t('Could not read this run')}
        body={error}
        primaryAction={{ label: t('Retry'), onClick: () => void refresh() }}
      />
    );
  }
  if (!state) return null;

  const marks: Record<string, NodeMark> = {};
  for (const [id, status] of Object.entries(state.nodeStatus)) marks[id] = RUN_STATUS_MARK[status];
  const waitingNode = Object.entries(state.nodeStatus).find(([, s]) => s === 'paused')?.[0];

  return (
    <div className="fs-runoverlay" data-testid="run-overlay">
      <header className="fs-runoverlay__head">
        <div>
          <p className="fs-runoverlay__status" data-status={state.status}>{state.status}</p>
          {state.reason && <p className="fs-runoverlay__reason">{state.reason}</p>}
        </div>
        <div className="fs-runoverlay__actions">
          <Button variant="secondary" size="sm" label={t('Refresh')} onClick={() => void refresh()} testId="run-overlay-refresh" />
          {waitingNode && (
            <Button variant="secondary" size="sm" label={t('Resume')} onClick={() => void act('advance', waitingNode)} loading={busy} testId="run-overlay-resume" />
          )}
          <Button variant="secondary" size="sm" label={t('Advance')} onClick={() => void act('advance')} loading={busy} testId="run-overlay-advance" />
          <Button variant="danger" size="sm" label={t('Cancel run')} onClick={() => void act('cancel')} loading={busy} testId="run-overlay-cancel" />
        </div>
      </header>
      <PlanGraph nodes={state.definitionNodes} marks={marks} selectedNodeId={selectedNodeId} onSelectNode={onSelectNode} />
    </div>
  );
}
