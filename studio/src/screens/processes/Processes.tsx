import { AlertTriangle, RefreshCw, Search, XOctagon } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, EmptyState, IconButton, Skeleton, Toast } from '../../components';
import {
  fetchProcesses,
  stopProcess,
  type BgJobRow,
  type ProcOrigin,
  type ProcRow,
  type ProcessSnapshot,
  type StopResult,
} from '../../adapters/processCenter';
import { t, tn } from '../../i18n';
import { AppsSection } from './Apps';
import '../projects.css';
import '../settings.css';
import '../connectors/connectors.css';
import './processes.css';

/**
 * Processes — the "control center": what is running on this machine
 * because of Faustus (listening ports, MCP/agent children, background
 * jobs, launched profiles, watched apps) and a Stop for each, backed by
 * `GET/POST /api/process-center*` (routes/process_center_routes.py,
 * src/process_center.py).
 *
 * Stop is deliberately a *person* action: the backend refuses the internal
 * agent token on `/stop`, and this screen never offers a way to kill
 * something without a click and — for anything not already confirmed by a
 * protected flag — an inline "Stop X? Confirm/Cancel" step first, never a
 * native dialog, same rule Connectors.tsx follows for Remove.
 */

const ORIGIN_LABEL: Record<ProcOrigin, string> = {
  self: 'Faustus',
  faustus: 'Faustus started it',
  bg_job: 'Background job',
  launch_profile: 'Launch profile',
  connector: 'Connector app',
  ollama: 'Ollama',
  other: 'Other',
};

function humanizeUptime(s: number | null): string {
  if (s == null || s < 0) return '';
  if (s < 60) return `${Math.round(s)}s`;
  const totalMin = Math.floor(s / 60);
  if (totalMin < 60) return `${totalMin}m`;
  const h = Math.floor(totalMin / 60);
  const remM = totalMin % 60;
  if (h < 24) return remM > 0 ? `${h}h ${remM}m` : `${h}h`;
  const d = Math.floor(h / 24);
  const remH = h % 24;
  return remH > 0 ? `${d}d ${remH}h` : `${d}d`;
}

function formatStarted(started: number | string | null): string {
  if (started == null || started === '') return '';
  try {
    const ms = typeof started === 'number' ? (started > 1e12 ? started : started * 1000) : Date.parse(started);
    if (!Number.isFinite(ms)) return String(started);
    return new Date(ms).toLocaleTimeString();
  } catch {
    return String(started);
  }
}

function outcomeText(outcome: StopResult): string {
  if (outcome.ok) {
    return outcome.signalled.length > 1 ? t('Stopped ({n} processes).', { n: outcome.signalled.length }) : t('Stopped.');
  }
  if (outcome.code === 'recycled') return t('Refresh the list — the pid changed.');
  return outcome.reason || t('Could not stop it.');
}

/** The Stop control shared by a port/Faustus/watched row and a bg job row:
 *  a plain button that turns into an inline "Stop X? Confirm/Cancel" (or,
 *  for the one protected-but-stoppable origin (Ollama), "Stop Ollama?
 *  Yes/No") before it ever calls `stopProcess`. */
function StopControl({
  pid,
  createdAt,
  name,
  protectedFlag,
  protectedReason,
  origin,
  onStopped,
  onBusyChange,
}: {
  pid: number;
  createdAt: number | null;
  name: string;
  protectedFlag: boolean;
  protectedReason: string;
  origin: ProcOrigin;
  onStopped: () => void;
  onBusyChange: (busy: boolean) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<StopResult | null>(null);
  const isOllama = origin === 'ollama';
  const disabled = protectedFlag && !isOllama;

  const doStop = async () => {
    setBusy(true);
    onBusyChange(true);
    try {
      const r = await stopProcess({ pid, created_at: createdAt, allow_protected: isOllama });
      setOutcome(r);
      setConfirming(false);
      if (r.ok) onStopped();
    } catch (e) {
      setOutcome({ ok: false, code: 'error', reason: (e as Error).message, signalled: [], refused: [] });
    } finally {
      setBusy(false);
      onBusyChange(false);
    }
  };

  return (
    <div className="fs-proc__stop">
      {!confirming ? (
        <Button
          size="sm"
          variant={disabled ? 'ghost' : 'danger'}
          label={isOllama ? t('Stop…') : t('Stop')}
          disabled={disabled}
          title={disabled ? protectedReason : undefined}
          onClick={() => setConfirming(true)}
          testId="process-stop"
        />
      ) : (
        <span className="fs-modes__confirm" data-testid="process-stop-confirm">
          {isOllama ? t('Stop Ollama?') : t('Stop {name}?', { name })}
          <Button size="sm" variant="danger" label={isOllama ? t('Yes') : t('Confirm')} loading={busy} onClick={() => void doStop()} />
          <Button size="sm" variant="ghost" label={isOllama ? t('No') : t('Cancel')} disabled={busy} onClick={() => setConfirming(false)} />
        </span>
      )}
      {outcome && (
        <p className="fs-proc__outcome" data-ok={outcome.ok} data-testid="process-stop-outcome">
          {outcomeText(outcome)}
        </p>
      )}
    </div>
  );
}

function ProcessRow({ row, onStopped, onBusyChange }: { row: ProcRow; onStopped: () => void; onBusyChange: (busy: boolean) => void }) {
  return (
    <li className="fs-proc__row" data-testid="process-row" data-origin={row.origin}>
      <div className="fs-proc__row-main">
        <span className="fs-proc__identity">
          <strong>{row.name}</strong> <span className="fs-proc__pid">#{row.pid}</span>
        </span>
        <span className="fs-proc-origin" data-origin={row.origin}>{t(ORIGIN_LABEL[row.origin] ?? ORIGIN_LABEL.other)}</span>
      </div>
      {row.label && <p className="fs-set__help">{row.label}</p>}
      <div className="fs-proc__row-detail" title={row.cmdline || undefined}>
        {row.ports.length > 0 && (
          <span className="fs-proc__ports">
            {row.ports.map((p) => (
              <span key={p} className="fs-chip fs-proc__port">:{p}</span>
            ))}
          </span>
        )}
        {row.rss_mb != null && <span className="fs-proc__mem">{t('{n} MB', { n: Math.round(row.rss_mb) })}</span>}
        {row.uptime_s != null && <span className="fs-proc__uptime">{humanizeUptime(row.uptime_s)}</span>}
        {row.cwd && <span className="fs-proc__cwd">{row.cwd}</span>}
        {row.children > 0 && <span className="fs-proc__children">{t('+{n} children', { n: row.children })}</span>}
      </div>
      <div className="fs-proc__actions">
        <StopControl
          pid={row.pid}
          createdAt={row.created_at}
          name={row.name}
          protectedFlag={row.protected}
          protectedReason={row.protected_reason}
          origin={row.origin}
          onStopped={onStopped}
          onBusyChange={onBusyChange}
        />
      </div>
    </li>
  );
}

function JobRow({ job, match, onStopped, onBusyChange }: { job: BgJobRow; match: ProcRow | null; onStopped: () => void; onBusyChange: (busy: boolean) => void }) {
  return (
    <li className="fs-proc__job-row" data-testid="process-job-row">
      <div className="fs-proc__row-main">
        <span className="fs-proc__identity">
          <strong>{job.id}</strong> {job.pid != null && <span className="fs-proc__pid">#{job.pid}</span>}
        </span>
      </div>
      <p className="fs-proc__job-command" title={job.command}>{job.command}</p>
      <div className="fs-proc__row-detail">
        {job.cwd && <span className="fs-proc__cwd">{job.cwd}</span>}
        {job.started_at != null && <span className="fs-proc__uptime">{formatStarted(job.started_at)}</span>}
      </div>
      <div className="fs-proc__actions">
        {match ? (
          <StopControl
            pid={match.pid}
            createdAt={match.created_at}
            name={job.id}
            protectedFlag={match.protected}
            protectedReason={match.protected_reason}
            origin={match.origin}
            onStopped={onStopped}
            onBusyChange={onBusyChange}
          />
        ) : job.pid != null ? (
          <span className="fs-set__help" data-testid="process-job-not-visible">{t('Not visible in the process list.')}</span>
        ) : null}
      </div>
    </li>
  );
}

function Section({ id, title, count, children, headerExtra }: { id: string; title: string; count: number; children: React.ReactNode; headerExtra?: React.ReactNode }) {
  return (
    <section className="fs-proc__section" data-testid={`process-section-${id}`}>
      <div className="fs-proc__section-head">
        <h2 className="fs-proc__section-title">{title} <span className="fs-set__help">({count})</span></h2>
        {headerExtra}
      </div>
      {children}
    </section>
  );
}

export function ProcessesScreen() {
  const [snap, setSnap] = useState<ProcessSnapshot | null>(null);
  const [failed, setFailed] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [busyCount, setBusyCount] = useState(0);
  const [query, setQuery] = useState('');
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [stopAllConfirm, setStopAllConfirm] = useState(false);
  const [stopAllBusy, setStopAllBusy] = useState(false);
  const noticeTimer = useRef<number | null>(null);

  const say = useCallback((msg: string) => {
    setNotice(msg);
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), 2600);
  }, []);

  const reload = useCallback(() => {
    fetchProcesses(true)
      .then((s) => {
        setSnap(s);
        setFailed(false);
        setLastUpdated(Date.now());
      })
      .catch(() => setFailed(true));
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  // Auto-refresh every 5s, paused while any stop (or the "stop all") is in
  // flight — polling mid-kill just re-races the same rows.
  useEffect(() => {
    if (!autoRefresh || busyCount > 0) return;
    const id = window.setInterval(reload, 5000);
    return () => window.clearInterval(id);
  }, [autoRefresh, busyCount, reload]);

  // Tick once a second so "Last updated Xs ago" stays current without a
  // network call.
  useEffect(() => {
    const id = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, []);

  const onBusyChange = useCallback((busy: boolean) => {
    setBusyCount((n) => Math.max(0, n + (busy ? 1 : -1)));
  }, []);

  const matches = useCallback(
    (r: ProcRow) => {
      const q = query.trim().toLowerCase();
      if (!q) return true;
      return (
        r.name.toLowerCase().includes(q) ||
        r.cwd.toLowerCase().includes(q) ||
        r.cmdline.toLowerCase().includes(q) ||
        r.ports.some((p) => String(p).includes(q))
      );
    },
    [query],
  );

  const portsSorted = useMemo(() => {
    if (!snap) return [];
    return [...snap.ports].filter(matches).sort((a, b) => {
      const pa = a.ports.length ? Math.min(...a.ports) : Number.MAX_SAFE_INTEGER;
      const pb = b.ports.length ? Math.min(...b.ports) : Number.MAX_SAFE_INTEGER;
      return pa - pb;
    });
  }, [snap, matches]);

  const faustusRows = useMemo(() => (snap ? snap.faustus.filter(matches) : []), [snap, matches]);
  const watchedRows = useMemo(() => (snap ? snap.watched.filter(matches) : []), [snap, matches]);
  const jobs = useMemo(() => (snap ? snap.jobs : []), [snap]);
  const allRows = useMemo(() => (snap ? [...snap.ports, ...snap.faustus, ...snap.watched] : []), [snap]);
  const jobMatch = useCallback((job: BgJobRow) => (job.pid != null ? allRows.find((r) => r.pid === job.pid) ?? null : null), [allRows]);

  const secondsAgo = lastUpdated != null ? Math.max(0, Math.round((Date.now() - lastUpdated) / 1000)) : null;

  const stopAllFaustus = async () => {
    if (!snap) return;
    setStopAllBusy(true);
    onBusyChange(true);
    let stopped = 0;
    for (const row of snap.faustus) {
      if (row.protected) continue;
      try {
        const r = await stopProcess({ pid: row.pid, created_at: row.created_at });
        if (r.ok) stopped += 1;
      } catch {
        /* keep going — one row's failure should not block the rest */
      }
    }
    setStopAllBusy(false);
    onBusyChange(false);
    setStopAllConfirm(false);
    say(tn(stopped, t('{n} process stopped.'), t('{n} processes stopped.'), { n: stopped }));
    reload();
  };

  return (
    <div className="fs-screen fs-proc" data-testid="processes-screen">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Processes')}</h1>
          <p className="fs-prose">{t('What is running because of Faustus — ports, background jobs, launched apps — and a Stop for each.')}</p>
        </div>
        <div className="fs-set__row-actions">
          <label className="fs-proc__toggle">
            <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} data-testid="processes-autorefresh" />
            {t('Auto-refresh')}
          </label>
          <span className="fs-set__help" data-testid="processes-updated">
            {secondsAgo == null ? t('Loading') : secondsAgo === 0 ? t('Last updated just now') : t('Last updated {n}s ago', { n: secondsAgo })}
          </span>
          <IconButton icon={RefreshCw} label={t('Refresh')} onClick={reload} testId="processes-refresh" />
        </div>
      </header>

      <label className="fs-search" data-testid="processes-filter">
        <Search size={15} aria-hidden="true" />
        <input
          type="search"
          value={query}
          placeholder={t('Filter by name, port or path…')}
          aria-label={t('Filter')}
          onChange={(e) => setQuery(e.target.value)}
        />
      </label>

      {snap && snap.available === false && (
        <div className="fs-proc__banner" role="status" data-testid="processes-unavailable-banner">
          <AlertTriangle size={14} aria-hidden="true" /> {t('psutil is not installed on the server; only ports are listed.')}
        </div>
      )}

      <AppsSection say={say} />

      {failed && snap === null ? (
        <EmptyState
          icon={XOctagon}
          tone="error"
          title={t('Could not read the process list.')}
          body={t('GET /api/process-center failed.')}
          primaryAction={{ label: t('Try again'), onClick: reload }}
        />
      ) : snap === null ? (
        <Skeleton label={t('Loading')} count={4} height="72px" />
      ) : (
        <>
          <Section id="ports" title={t('Listening ports')} count={portsSorted.length}>
            {portsSorted.length === 0 ? (
              <p className="fs-set__help">{t('Nothing is listening on a port right now.')}</p>
            ) : (
              <ul className="fs-proc__list">
                {portsSorted.map((row) => (
                  <ProcessRow key={row.pid} row={row} onStopped={reload} onBusyChange={onBusyChange} />
                ))}
              </ul>
            )}
          </Section>

          <Section
            id="faustus"
            title={t('Started by Faustus')}
            count={faustusRows.length}
            headerExtra={
              faustusRows.some((r) => !r.protected) ? (
                !stopAllConfirm ? (
                  <Button
                    size="sm"
                    variant="danger"
                    label={t('Stop all started by Faustus')}
                    onClick={() => setStopAllConfirm(true)}
                    testId="processes-stop-all"
                  />
                ) : (
                  <span className="fs-modes__confirm" data-testid="processes-stop-all-confirm">
                    {t('Stop everything Faustus started?')}
                    <Button size="sm" variant="danger" label={t('Confirm')} loading={stopAllBusy} onClick={() => void stopAllFaustus()} />
                    <Button size="sm" variant="ghost" label={t('Cancel')} disabled={stopAllBusy} onClick={() => setStopAllConfirm(false)} />
                  </span>
                )
              ) : undefined
            }
          >
            {faustusRows.length === 0 ? (
              <p className="fs-set__help">{t('Faustus has not started anything else right now.')}</p>
            ) : (
              <ul className="fs-proc__list">
                {faustusRows.map((row) => (
                  <ProcessRow key={row.pid} row={row} onStopped={reload} onBusyChange={onBusyChange} />
                ))}
              </ul>
            )}
          </Section>

          <Section id="jobs" title={t('Background jobs')} count={jobs.length}>
            {jobs.length === 0 ? (
              <p className="fs-set__help">{t('No background jobs are running.')}</p>
            ) : (
              <ul className="fs-proc__list">
                {jobs.map((job) => (
                  <JobRow key={job.id} job={job} match={jobMatch(job)} onStopped={reload} onBusyChange={onBusyChange} />
                ))}
              </ul>
            )}
          </Section>

          <Section id="watched" title={t('Other apps')} count={watchedRows.length}>
            {watchedRows.length === 0 ? (
              <p className="fs-set__help">{t('Nothing else is being watched right now.')}</p>
            ) : (
              <ul className="fs-proc__list">
                {watchedRows.map((row) => (
                  <ProcessRow key={row.pid} row={row} onStopped={reload} onBusyChange={onBusyChange} />
                ))}
              </ul>
            )}
          </Section>
        </>
      )}

      {notice && <Toast>{notice}</Toast>}
    </div>
  );
}
