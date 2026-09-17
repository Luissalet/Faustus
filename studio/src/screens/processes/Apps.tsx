import { LayoutGrid, Pencil, Play, Plug, Power, RotateCw, Terminal, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, EmptyState, IconButton, Skeleton } from '../../components';
import {
  appIconUrl,
  appLog,
  appStatuses,
  deleteApp,
  listApps,
  openApp,
  restartApp,
  startApp,
  stopApp,
  type AppProfile,
  type AppStatus,
} from '../../adapters/apps';
import { AppForm } from './AppForm';
import { t } from '../../i18n';

/**
 * Apps — the first block of the Processes screen (apps_contract.md, lot B
 * as amended: "mételo en el menú que habías hecho de procesos abiertos en
 * puertos"). A control-center card grid for the owner's own local
 * projects: configure, launch, watch their console, stop, restart, add and
 * remove freely — on top of `/api/launch-profiles*`
 * (`src/launch_profiles.py`, `routes/connector_routes.py`).
 *
 * Statuses come from ONE `GET /api/launch-profiles/status` call, polled
 * every 5s (and right after any action) rather than one request per card.
 */

function statusLabel(s: AppStatus | undefined, pending: 'starting' | 'stopping' | null): string {
  if (pending === 'starting') return t('Starting…');
  if (pending === 'stopping') return t('Stopping…');
  if (!s || !s.running) return t('Stopped');
  const bits = [t('Running')];
  if (s.pid != null) bits.push(t('pid {pid}', { pid: s.pid }));
  if (s.port != null) bits.push(`:${s.port}`);
  return bits.join(' · ');
}

function statusTone(s: AppStatus | undefined, pending: 'starting' | 'stopping' | null): 'ok' | 'warn' | 'muted' {
  if (pending) return 'warn';
  if (s?.running) return 'ok';
  return 'muted';
}

function initial(name: string): string {
  return (name.trim()[0] || '?').toUpperCase();
}

function AppIcon({ app }: { app: AppProfile }) {
  const [broken, setBroken] = useState(!app.icon);
  useEffect(() => setBroken(!app.icon), [app.icon, app.id]);
  if (broken) {
    return (
      <span className="fs-apps__icon fs-apps__icon--fallback" aria-hidden="true">
        {initial(app.name)}
      </span>
    );
  }
  return <img className="fs-apps__icon" src={appIconUrl(app.id)} alt="" onError={() => setBroken(true)} />;
}

function AppConsole({ appId, onClose }: { appId: string; onClose: () => void }) {
  const [log, setLog] = useState<string>('');
  const [failed, setFailed] = useState(false);
  const boxRef = useRef<HTMLPreElement | null>(null);

  const reload = useCallback(() => {
    appLog(appId, 300)
      .then((text) => {
        setLog(text);
        setFailed(false);
      })
      .catch(() => setFailed(true));
  }, [appId]);

  useEffect(() => {
    reload();
    const id = window.setInterval(reload, 3000);
    return () => window.clearInterval(id);
  }, [reload]);

  useEffect(() => {
    const el = boxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [log]);

  return (
    <div className="fs-apps__console" data-testid="apps-console-panel">
      <div className="fs-apps__console-head">
        <span className="fs-set__help">
          <Terminal size={13} aria-hidden="true" /> {t('Console')}
        </span>
        <span className="fs-apps__console-actions">
          <Button size="sm" variant="ghost" label={t('Refresh')} onClick={reload} />
          <Button size="sm" variant="ghost" label={t('Close')} onClick={onClose} />
        </span>
      </div>
      <pre ref={boxRef} className="fs-apps__console-body">
        {failed ? t('Could not read the log.') : log || t('No log output yet.')}
      </pre>
    </div>
  );
}

function AppCard({
  app,
  status,
  pending,
  onAction,
  onEdit,
  onRemoved,
  consoleOpen,
  onToggleConsole,
}: {
  app: AppProfile;
  status: AppStatus | undefined;
  pending: 'starting' | 'stopping' | null;
  onAction: (kind: 'start' | 'stop' | 'restart' | 'open', id: string) => Promise<void>;
  onEdit: () => void;
  onRemoved: () => void;
  consoleOpen: boolean;
  onToggleConsole: () => void;
}) {
  const running = !!status?.running;
  const [confirmStop, setConfirmStop] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (kind: 'start' | 'stop' | 'restart' | 'open') => {
    // Open: the desktop window when the profile wants one (`POST /open`);
    // otherwise its `open_url`/`url` in a plain browser tab — the backend's
    // own `open_desktop` has nowhere to point a window it was told not to
    // open, so the "in a new tab" half of Open is a frontend decision.
    if (kind === 'open' && !app.desktop) {
      const target = app.open_url || app.url;
      if (target) window.open(target, '_blank', 'noopener,noreferrer');
      else setError(t('This app has no address to open.'));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onAction(kind, app.id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setBusy(true);
    try {
      await deleteApp(app.id);
      onRemoved();
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <li className="fs-apps__card" data-testid="apps-card" data-app-id={app.id} data-console-open={consoleOpen || undefined}>
      <div className="fs-apps__card-head">
        <AppIcon app={app} />
        <span className="fs-apps__card-title">
          <strong>{app.name}</strong>
          {app.description && <span className="fs-apps__card-desc">{app.description}</span>}
        </span>
      </div>
      <span className="fs-apps__pill" data-tone={statusTone(status, pending)} data-testid="apps-status">
        <span className="fs-apps__pill-dot" aria-hidden="true" />
        {statusLabel(status, pending)}
      </span>
      <div className="fs-apps__card-actions">
        <Button
          size="sm"
          variant="primary"
          icon={running ? Plug : Play}
          label={running ? t('Open') : t('Start')}
          disabled={busy}
          onClick={() => void run(running ? 'open' : 'start')}
          testId={running ? 'apps-open' : 'apps-start'}
        />
        {app.open_url && (
          <a className="fs-apps__browser-link" href={app.open_url} target="_blank" rel="noreferrer">
            {t('in browser')}
          </a>
        )}
        {running &&
          (!confirmStop ? (
            <Button size="sm" variant="danger" label={t('Stop')} disabled={busy} onClick={() => setConfirmStop(true)} testId="apps-stop" />
          ) : (
            <span className="fs-modes__confirm" data-testid="apps-stop-confirm">
              {t('Stop {name}?', { name: app.name })}
              <Button size="sm" variant="danger" label={t('Confirm')} loading={busy} onClick={() => { setConfirmStop(false); void run('stop'); }} />
              <Button size="sm" variant="ghost" label={t('Cancel')} disabled={busy} onClick={() => setConfirmStop(false)} />
            </span>
          ))}
        <Button size="sm" variant="ghost" icon={RotateCw} label={t('Restart')} disabled={busy} onClick={() => void run('restart')} testId="apps-restart" />
        <Button size="sm" variant="ghost" icon={Terminal} label={t('Console')} onClick={onToggleConsole} testId="apps-console" />
        <IconButton icon={Pencil} label={t('Edit')} size="sm" onClick={onEdit} testId="apps-edit" />
        {!confirmRemove ? (
          <IconButton icon={Trash2} label={t('Remove')} size="sm" onClick={() => setConfirmRemove(true)} testId="apps-remove" />
        ) : (
          <span className="fs-modes__confirm" data-testid="apps-remove-confirm">
            {t('Remove {name}?', { name: app.name })}
            <Button size="sm" variant="danger-solid" label={t('Remove')} loading={busy} onClick={() => void remove()} />
            <Button size="sm" variant="ghost" label={t('Cancel')} disabled={busy} onClick={() => setConfirmRemove(false)} />
          </span>
        )}
      </div>
      {error && <p className="fs-set__err" data-tone="bad">{error}</p>}
      {consoleOpen && <AppConsole appId={app.id} onClose={onToggleConsole} />}
    </li>
  );
}

export function AppsSection({ say }: { say: (msg: string) => void }) {
  const [apps, setApps] = useState<AppProfile[] | null>(null);
  const [statuses, setStatuses] = useState<Record<string, AppStatus>>({});
  const [failed, setFailed] = useState(false);
  const [editing, setEditing] = useState<AppProfile | 'new' | null>(null);
  const [openConsole, setOpenConsole] = useState<string | null>(null);
  const [pending, setPending] = useState<Record<string, 'starting' | 'stopping'>>({});

  const reloadApps = useCallback(() => {
    listApps()
      .then((rows) => {
        setApps(rows);
        setFailed(false);
      })
      .catch(() => setFailed(true));
  }, []);

  const reloadStatuses = useCallback(() => {
    appStatuses()
      .then(setStatuses)
      .catch(() => {
        /* the grid still shows names/actions even if a status refresh fails */
      });
  }, []);

  useEffect(() => {
    reloadApps();
    reloadStatuses();
  }, [reloadApps, reloadStatuses]);

  useEffect(() => {
    const id = window.setInterval(reloadStatuses, 5000);
    return () => window.clearInterval(id);
  }, [reloadStatuses]);

  const setPendingFor = (id: string, kind: 'starting' | 'stopping' | null) => {
    setPending((p) => {
      const next = { ...p };
      if (kind) next[id] = kind;
      else delete next[id];
      return next;
    });
  };

  const onAction = async (kind: 'start' | 'stop' | 'restart' | 'open', id: string) => {
    if (kind === 'start' || kind === 'restart') setPendingFor(id, 'starting');
    if (kind === 'stop') setPendingFor(id, 'stopping');
    try {
      if (kind === 'start') await startApp(id);
      else if (kind === 'stop') await stopApp(id);
      else if (kind === 'restart') await restartApp(id);
      else await openApp(id);
    } catch (e) {
      say((e as Error).message);
      throw e;
    } finally {
      setPendingFor(id, null);
      reloadStatuses();
    }
  };

  const cards = useMemo(() => apps ?? [], [apps]);

  return (
    <section id="apps" className="fs-proc__section fs-apps" data-testid="processes-section-apps">
      <div className="fs-proc__section-head">
        <h2 className="fs-proc__section-title">
          {t('Apps')} <span className="fs-set__help">({cards.length})</span>
        </h2>
        {editing === null && (
          <Button size="sm" variant="secondary" icon={LayoutGrid} label={t('Add app')} onClick={() => setEditing('new')} testId="apps-add" />
        )}
      </div>

      {editing !== null && (
        <AppForm
          existing={editing === 'new' ? undefined : editing}
          onCancel={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reloadApps();
            reloadStatuses();
          }}
        />
      )}

      {failed && apps === null ? (
        <p className="fs-set__help">{t('Could not load apps.')}</p>
      ) : apps === null ? (
        <Skeleton label={t('Loading apps')} count={2} height="88px" />
      ) : cards.length === 0 ? (
        <EmptyState
          icon={Power}
          title={t('No apps yet')}
          body={t('An app is one of your own local projects — a server, a desktop program or a web address — that you want to launch, watch and stop from here.')}
          primaryAction={{ label: t('Add app'), onClick: () => setEditing('new') }}
        />
      ) : (
        <ul className="fs-apps__grid" data-testid="apps-grid">
          {cards.map((app) => (
            <AppCard
              key={app.id}
              app={app}
              status={statuses[app.id]}
              pending={pending[app.id] ?? null}
              onAction={onAction}
              onEdit={() => setEditing(app)}
              onRemoved={() => {
                setOpenConsole((c) => (c === app.id ? null : c));
                reloadApps();
                reloadStatuses();
              }}
              consoleOpen={openConsole === app.id}
              onToggleConsole={() => setOpenConsole((c) => (c === app.id ? null : app.id))}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
