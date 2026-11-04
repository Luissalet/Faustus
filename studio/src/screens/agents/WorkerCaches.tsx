import { useCallback, useEffect, useState } from 'react';
import { Eraser, Package, RefreshCw } from 'lucide-react';
import { Button } from '../../components';
import { clearWorkerCaches, loadWorkerCaches, type WorkerCache, type WorkerCachesSnapshot } from '../../adapters/agents';
import { relativeTime } from '../../adapters/home';
import { t, tn } from '../../i18n';
import '../agents.css';

/**
 * Darwin #999: every delegated worker (delegate_agents, the minions of an
 * agent definition, dispatch and swarm workers) and every fan-out candidate
 * gets pip/npm/uv/yarn/pnpm/poetry/go/nuget caches and a temp folder of its
 * own (src/worker_caches.py), so isolated agents cannot hand each other
 * anything through a shared cache. This shows each worker's folder, its size
 * per tool, and a button to empty it.
 */

export function formatBytes(n: number): string {
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)} GB`;
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

function toolsLine(row: WorkerCache): string {
  return Object.entries(row.tools)
    .filter(([, v]) => v > 0)
    .map(([k, v]) => `${k} ${formatBytes(v)}`)
    .join(' · ');
}

function CacheRow({ row, onCleared }: { row: WorkerCache; onCleared: (msg: string) => void }) {
  const [busy, setBusy] = useState(false);
  const clear = async () => {
    if (row.active && !window.confirm(t('This worker is running. Empty its caches anyway?'))) return;
    setBusy(true);
    try {
      const done = await clearWorkerCaches({ root: row.root, force: row.active });
      onCleared(t('{size} freed', { size: formatBytes(done.freedBytes) }));
    } catch {
      onCleared(t('Could not empty the caches.'));
    } finally {
      setBusy(false);
    }
  };
  const tools = toolsLine(row);
  return (
    <li className="fs-agents__cache-row" data-active={row.active || undefined} data-testid="worker-cache-row">
      <div className="fs-agents__cache-main">
        <strong>{row.name || row.sessionId}</strong>
        <span className="fs-agents__note">{row.kind === 'fanout' ? t('fan-out candidate') : row.agent ? t('minion of {agent}', { agent: row.agent }) : t('delegated worker')}</span>
        {row.active && <span className="fs-agents__cache-live">{t('running')}</span>}
        <span>{formatBytes(row.bytes)}{row.approximate ? '+' : ''}</span>
        {row.created && <span className="fs-agents__note">{relativeTime(row.created)}</span>}
        <Button size="sm" icon={Eraser} label={t('Empty caches')} loading={busy} disabled={row.bytes === 0 && !busy} onClick={() => void clear()} />
      </div>
      <code className="fs-agents__cache-path" title={row.root}>{row.root}</code>
      {(tools || row.crossReads > 0) && (
        <span className="fs-agents__note">
          {tools}
          {row.crossReads > 0 ? ` · ${tn(row.crossReads, '{n} read of another worker\'s folder', '{n} reads of other workers\' folders')}` : ''}
        </span>
      )}
    </li>
  );
}

/** Agents screen: every worker's own cache folder, newest first. */
export function WorkerCachesCard() {
  const [snap, setSnap] = useState<WorkerCachesSnapshot | null>(null);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const reload = useCallback(async () => {
    try {
      setSnap(await loadWorkerCaches());
    } catch {
      setSnap({ base: '', workers: [], totalBytes: 0, active: 0 });
    }
  }, []);
  useEffect(() => {
    void reload();
  }, [reload]);
  const clearAll = async () => {
    setBusy(true);
    try {
      const done = await clearWorkerCaches({ all: true });
      setNote(t('{size} freed', { size: formatBytes(done.freedBytes) }) + (done.skipped ? ` · ${tn(done.skipped, '{n} running worker skipped', '{n} running workers skipped')}` : ''));
    } catch {
      setNote(t('Could not empty the caches.'));
    } finally {
      setBusy(false);
      void reload();
    }
  };
  const rows = snap?.workers ?? [];
  return (
    <section className="fs-agents__leases fs-agents__caches" data-testid="worker-caches-card">
      <header className="fs-agents__leases-head">
        <Package size={14} aria-hidden="true" />
        <strong>{t('Worker caches')}</strong>
        <span className="fs-agents__note">
          {snap ? `${tn(rows.length, '{n} worker folder', '{n} worker folders')} · ${formatBytes(snap.totalBytes)}` : t('Loading…')}
        </span>
        <span style={{ flex: 1 }} />
        <Button size="sm" icon={RefreshCw} label={t('Refresh')} onClick={() => void reload()} />
        <Button size="sm" icon={Eraser} label={t('Empty idle workers')} loading={busy} disabled={rows.length === 0} onClick={() => void clearAll()} />
      </header>
      <p className="fs-agents__note">
        {t('Each worker resolves pip, npm, uv, yarn, pnpm, poetry, go and nuget packages and its temp files through a folder of its own, so two workers never share a cache.')}
      </p>
      {note && <p className="fs-agents__note" data-on>{note}</p>}
      {snap && rows.length === 0 && <p className="fs-agents__note">{t('No worker has run a command yet.')}</p>}
      {rows.length > 0 && (
        <ul className="fs-agents__leases-list">
          {rows.slice(0, 50).map((row) => (
            <CacheRow key={row.root} row={row} onCleared={(msg) => { setNote(msg); void reload(); }} />
          ))}
        </ul>
      )}
    </section>
  );
}

/** Sub-agent board card: this worker's cache folder and a button to empty it. */
export function WorkerCacheInline({ sessionId }: { sessionId: string }) {
  const [row, setRow] = useState<WorkerCache | null | undefined>(undefined);
  const [msg, setMsg] = useState('');
  const reload = useCallback(async () => {
    try {
      const snap = await loadWorkerCaches({ sessionId });
      setRow(snap.workers[0] ?? null);
    } catch {
      setRow(null);
    }
  }, [sessionId]);
  useEffect(() => {
    void reload();
  }, [reload]);
  if (row === undefined) return <p className="fs-sa__muted">{t('Loading…')}</p>;
  if (row === null) return <p className="fs-sa__muted" data-testid="worker-cache-inline">{t('This worker has not run a command, so it has no cache folder yet.')}</p>;
  return (
    <ul className="fs-agents__leases-list" data-testid="worker-cache-inline">
      <CacheRow row={row} onCleared={(m) => { setMsg(m); void reload(); }} />
      {msg && <li className="fs-sa__muted">{msg}</li>}
    </ul>
  );
}
