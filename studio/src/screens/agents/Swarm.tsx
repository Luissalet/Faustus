import { Layers, Play, Square } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Button, Skeleton } from '../../components';
import {
  cancelSwarmRun,
  isLiveSwarm,
  listSwarmRuns,
  resumeSwarmRun,
  swarmFileUrl,
  type SwarmRun,
  type SwarmStatus,
} from '../../adapters/swarm';
import { t } from '../../i18n';

/**
 * Swarm map runs (`src/swarm/`, `/api/swarm`): one instruction applied to
 * many items at once by the `swarm_map` tool. Listed here with their
 * progress, how many items run at a time, and the result table files.
 */

const STATUS_WORD: Record<SwarmStatus, string> = {
  queued: 'queued',
  running: 'running',
  done: 'done',
  partial: 'partial',
  failed: 'failed',
  cancelled: 'cancelled',
  interrupted: 'interrupted',
};

function short(text: string, n: number): string {
  const s = (text || '').replace(/\s+/g, ' ').trim();
  return s.length <= n ? s : `${s.slice(0, n - 1)}…`;
}

function SwarmRow({ run, onCancel, onResume }: { run: SwarmRun; onCancel: () => void; onResume: () => void }) {
  const live = isLiveSwarm(run.status);
  const c = run.counts;
  const pct = Math.round((run.progress || 0) * 100);
  const par = run.parallel?.effective;
  return (
    <div className="fs-wk__job" data-testid="swarm-row" data-status={run.status}>
      <div className="fs-wk__head">
        <div className="fs-wk__swarm-main">
          <div className="fs-wk__swarm-title">
            <span className="fs-wk__muted">{run.run_id}</span>
            <span data-testid="swarm-state">{t(STATUS_WORD[run.status] ?? run.status)}</span>
            <span className="fs-wk__muted">{run.mode === 'agent' ? t('workers') : t('model calls')}</span>
            {par ? <span className="fs-wk__muted">{t('{n} at a time', { n: par })}</span> : null}
          </div>
          <div className="fs-wk__swarm-instr" title={run.instruction}>{short(run.instruction, 140)}</div>
          <div className="fs-wk__swarm-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
            <span style={{ inlineSize: `${pct}%` }} />
          </div>
          <div className="fs-wk__muted" data-testid="swarm-counts">
            {t('{ok} ok, {failed} failed, {pending} pending of {total}', { ok: c.ok, failed: c.failed, pending: c.pending, total: c.total })}
            {run.reduce_status ? ` · ${t('reduce')}: ${t(run.reduce_status === 'ok' ? 'done' : 'failed')}` : ''}
          </div>
          {run.error && <div className="fs-wk__error">{run.error}</div>}
          {run.files.length > 0 && (
            <div className="fs-wk__swarm-files">
              {run.files.map((name) => (
                <a key={name} href={swarmFileUrl(run.run_id, name)} download={name} className="fs-wk__swarm-file">
                  {name.slice(run.run_id.length) || name}
                </a>
              ))}
            </div>
          )}
        </div>
        {live && <Button variant="ghost" size="sm" icon={Square} label={t('Stop')} onClick={onCancel} testId="swarm-cancel" />}
        {!live && run.resumable && <Button variant="ghost" size="sm" icon={Play} label={t('Resume')} onClick={onResume} testId="swarm-resume" />}
      </div>
    </div>
  );
}

export function SwarmSection() {
  const [runs, setRuns] = useState<SwarmRun[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setRuns(await listSwarmRuns());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const anyLive = (runs ?? []).some((r) => isLiveSwarm(r.status));

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), anyLive ? 3000 : 15000);
    return () => window.clearInterval(timer);
  }, [load, anyLive]);

  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="fs-wk__section" data-testid="swarm">
      <div className="fs-agents__intro">
        <p className="fs-prose">
          <Layers size={14} aria-hidden="true" /> {t('Swarm: one instruction applied to many items at once, as many as the model server serves in parallel. Started from a chat with the swarm_map tool.')}
        </p>
      </div>
      {error && <div className="fs-wk__error">{error}</div>}
      {runs === null ? (
        <Skeleton label={t('Loading swarm runs')} height="32px" count={1} />
      ) : runs.length === 0 ? (
        <p className="fs-wk__muted">{t('No swarm run yet.')}</p>
      ) : (
        <div className="fs-wk__list">
          {runs.map((r) => (
            <SwarmRow key={r.run_id} run={r} onCancel={() => void act(() => cancelSwarmRun(r.run_id))} onResume={() => void act(() => resumeSwarmRun(r.run_id))} />
          ))}
        </div>
      )}
    </div>
  );
}
