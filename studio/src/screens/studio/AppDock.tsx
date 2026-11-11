import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ExternalLink, LayoutGrid, RotateCw, X } from 'lucide-react';
import { Button, IconButton } from '../../components';
import {
  closeApp, iconUrl, listApps, openApp, postDockResult, postDockState, registerFrame, setSel, useDockSel,
  type DockApp,
} from '../../adapters/appDock';
import { getLang, t } from '../../i18n';
import './app-dock.css';

/**
 * The App tab: one of the family's Hoards in a frame next to the chat. The person uses it by hand; the agent
 * (app_view / app_act) sees the page and acts on it through the bridge the Hoard page loads. This component
 * is the host of the contract in Hoard Hub docs/APP_DOCK.md: it greets the page on every frame load, relays
 * what the server asks for and what the page answers, and checks that every message comes from the frame it
 * made and from that page's own origin. Without a bridge the app is still a plain embedded app.
 */

type Bridge = 'pending' | 'ready' | 'none';
const HANDSHAKE_MS = 3000;

function currentTheme(): 'light' | 'dark' {
  const explicit = document.documentElement.getAttribute('data-theme');
  if (explicit === 'light' || explicit === 'dark') return explicit;
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function AppIcon({ app }: { app: Pick<DockApp, 'id' | 'name' | 'icon'> }) {
  const [broken, setBroken] = useState(false);
  if (!app.icon || broken) return <span className="fs-appdock__glyph" aria-hidden="true">{app.name.slice(0, 1).toUpperCase()}</span>;
  return <img className="fs-appdock__img" src={iconUrl(app.id)} alt="" width={28} height={28} loading="lazy" onError={() => setBroken(true)} />;
}

function stateLabel(state: string): string {
  if (state === 'running') return t('Running');
  if (state === 'starting') return t('Starting');
  return t('Stopped');
}

function Picker({ sessionId, current, onPicked, onCancel }: {
  sessionId: string | null; current: string | null; onPicked: () => void; onCancel?: () => void;
}) {
  const [apps, setApps] = useState<DockApp[] | null>(null);
  const [error, setError] = useState('');
  const [filter, setFilter] = useState('');
  const [busyId, setBusyId] = useState<string | null>(null);
  const [failure, setFailure] = useState('');

  const load = useCallback(() => {
    setError('');
    setApps(null);
    listApps()
      .then(r => { if (r.available) setApps(r.apps); else setError(t('The Hoard Hub did not answer. Start it to open apps beside the chat.')); })
      .catch(e => setError((e as Error).message));
  }, []);
  useEffect(load, [load]);

  const shown = useMemo(() => {
    const q = filter.trim().toLowerCase();
    const rows = (apps ?? []).filter(a => !q || a.name.toLowerCase().includes(q) || a.id.toLowerCase().includes(q) || a.purpose.toLowerCase().includes(q));
    return rows.sort((a, b) => Number(b.state === 'running') - Number(a.state === 'running') || a.name.localeCompare(b.name));
  }, [apps, filter]);

  const pick = async (app: DockApp) => {
    if (!sessionId || busyId) return;
    if (app.id === current) { onPicked(); return; }
    setBusyId(app.id);
    setFailure('');
    try {
      const snap = await openApp(sessionId, app.id);
      setSel(sessionId, { appId: snap.app_id || app.id, name: snap.name || app.name, url: snap.url || app.url });
      onPicked();
    } catch (e) {
      setFailure(t('Could not open {name}: {reason}', { name: app.name, reason: (e as Error).message }));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="fs-appdock__picker" data-testid="app-dock-picker">
      <div className="fs-appdock__pickhead">
        <h3>{t('Open an app beside the chat')}</h3>
        {onCancel && <IconButton icon={X} label={t('Back to the app')} size="sm" onClick={onCancel} />}
      </div>
      {!sessionId ? (
        <p className="fs-appdock__empty">{t('Send a first message to open an app beside this conversation.')}</p>
      ) : error ? (
        <div className="fs-appdock__empty" role="alert">
          <p>{error}</p>
          <Button size="sm" label={t('Try again')} onClick={load} />
        </div>
      ) : apps === null ? (
        <ul className="fs-appdock__list" aria-busy="true" aria-label={t('Loading the apps')}>
          {Array.from({ length: 5 }, (_, i) => <li key={i} className="fs-appdock__row fs-appdock__row--skeleton" aria-hidden="true" />)}
        </ul>
      ) : (
        <>
          <input className="fs-appdock__filter" type="search" value={filter} onChange={e => setFilter(e.target.value)}
            placeholder={t('Filter apps')} aria-label={t('Filter apps')} data-testid="app-dock-filter" />
          {failure && <p className="fs-appdock__fail" role="alert">{failure}</p>}
          {shown.length === 0 ? <p className="fs-appdock__empty">{t('No apps found.')}</p> : (
            <ul className="fs-appdock__list">
              {shown.map(app => (
                <li key={app.id}>
                  <button type="button" className="fs-appdock__row" data-state={app.state} data-current={app.id === current || undefined}
                    disabled={Boolean(busyId)} aria-busy={busyId === app.id || undefined}
                    onClick={() => void pick(app)} data-testid={'app-dock-app-' + app.id}>
                    <AppIcon app={app} />
                    <span className="fs-appdock__titles">
                      <span className="fs-appdock__name">{app.name}</span>
                      {app.purpose && <span className="fs-appdock__purpose">{app.purpose}</span>}
                    </span>
                    {busyId === app.id
                      ? <span className="fs-appdock__starting" role="status"><span className="fs-appdock__spin" aria-hidden="true" />{t('Starting {name}…', { name: app.name })}</span>
                      : <span className="fs-appdock__chip" data-state={app.state}>{stateLabel(app.state)}</span>}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}

export default function AppDock({ sessionId, hidden, onMessage }: {
  sessionId: string | null;
  hidden?: boolean;
  /** `dock/message` from the page: put text in the composer, and send it when asked. */
  onMessage: (text: string, send: boolean) => void;
}) {
  const sel = useDockSel(sessionId);
  const [picking, setPicking] = useState(false);
  const [bridge, setBridge] = useState<Bridge>('pending');
  const [loaded, setLoaded] = useState(false);
  const [reloads, setReloads] = useState(0);
  const frameRef = useRef<HTMLIFrameElement>(null);
  const readyRef = useRef(false);
  const waiters = useRef<Array<(ok: boolean) => void>>([]);
  const timer = useRef<number | undefined>(undefined);
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;

  const origin = useMemo(() => { try { return sel ? new URL(sel.url).origin : ''; } catch { return ''; } }, [sel]);
  const showPicker = !sel || picking;

  const post = useCallback((message: Record<string, unknown>) => {
    const target = frameRef.current?.contentWindow;
    if (target && origin) target.postMessage({ hoardDock: 1, ...message }, origin);
  }, [origin]);

  const settle = useCallback((ok: boolean) => {
    const pending = waiters.current;
    waiters.current = [];
    pending.forEach(fn => fn(ok));
  }, []);

  // The server (and the tools) reach the frame through this handle.
  useEffect(() => {
    if (!sessionId || !origin) return;
    return registerFrame(sessionId, {
      post,
      isReady: () => readyRef.current,
      whenReady: ms => new Promise<boolean>(resolve => {
        if (readyRef.current) { resolve(true); return; }
        const done = (ok: boolean) => { window.clearTimeout(t0); resolve(ok); };
        const t0 = window.setTimeout(() => { waiters.current = waiters.current.filter(fn => fn !== done); resolve(false); }, ms);
        waiters.current.push(done);
      }),
    });
  }, [sessionId, origin, post]);

  // What the page says, only from the frame this tab made and from that page's own origin.
  useEffect(() => {
    if (!sessionId || !origin) return;
    const onWindowMessage = (event: MessageEvent) => {
      const frame = frameRef.current;
      if (!frame || event.source !== frame.contentWindow || event.origin !== origin) return;
      const m = event.data as Record<string, unknown> | null;
      if (!m || typeof m !== 'object' || m.hoardDock !== 1 || typeof m.type !== 'string') return;
      switch (m.type) {
        case 'dock/ready':
          window.clearTimeout(timer.current);
          readyRef.current = true;
          setBridge('ready');
          void postDockState({ session: sessionId, ready: true, tools: m.tools, context: m.context, app: m.app });
          settle(true);
          break;
        case 'dock/context':
          void postDockState({ session: sessionId, context: m.context });
          break;
        case 'dock/result':
          if (typeof m.id === 'string') {
            void postDockResult({ session: sessionId, id: m.id, ok: m.ok === true, result: m.result, error: m.error });
          }
          break;
        case 'dock/message':
          if (typeof m.text === 'string' && m.text.trim()) onMessageRef.current(m.text.slice(0, 20000), m.send === true);
          break;
        default:
      }
    };
    window.addEventListener('message', onWindowMessage);
    return () => window.removeEventListener('message', onWindowMessage);
  }, [sessionId, origin, settle]);

  // Greet the page on every load; if it never answers, the app still works as a plain embedded app.
  const onLoad = () => {
    setLoaded(true);
    readyRef.current = false;
    setBridge('pending');
    window.clearTimeout(timer.current);
    if (sessionId) void postDockState({ session: sessionId, ready: false });
    post({ type: 'dock/hello', host: 'faustus', theme: currentTheme(), locale: getLang(), session: sessionId });
    timer.current = window.setTimeout(() => { if (!readyRef.current) { setBridge('none'); settle(false); } }, HANDSHAKE_MS);
  };
  useEffect(() => () => window.clearTimeout(timer.current), []);
  useEffect(() => { setLoaded(false); setBridge('pending'); readyRef.current = false; }, [sel?.appId, sel?.url, reloads]);

  // The app follows the theme.
  useEffect(() => {
    const send = () => post({ type: 'dock/theme', theme: currentTheme() });
    const observer = new MutationObserver(send);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class'] });
    const media = window.matchMedia?.('(prefers-color-scheme: dark)');
    media?.addEventListener?.('change', send);
    return () => { observer.disconnect(); media?.removeEventListener?.('change', send); };
  }, [post]);

  const close = async () => {
    if (!sessionId) return;
    setSel(sessionId, null);
    setPicking(false);
    await closeApp(sessionId).catch(() => null);
  };

  return (
    <div className="fs-appdock" hidden={hidden} data-testid="app-dock" data-bridge={sel ? bridge : undefined}>
      {sel && (
        <>
          <div className="fs-appdock__bar" hidden={showPicker}>
            <AppIcon key={sel.appId} app={{ id: sel.appId, name: sel.name, icon: true }} />
            <span className="fs-appdock__bartitle" title={sel.name}>{sel.name}</span>
            {bridge === 'ready' && <span className="fs-appdock__chip" data-state="agent" data-testid="app-dock-agent-chip">{t('Agent connected')}</span>}
            <span className="fs-appdock__grow" />
            <IconButton icon={LayoutGrid} label={t('Switch app')} size="sm" onClick={() => setPicking(true)} testId="app-dock-switch" />
            <IconButton icon={RotateCw} label={t('Reload the app')} size="sm" onClick={() => setReloads(n => n + 1)} testId="app-dock-reload" />
            <IconButton icon={ExternalLink} label={t('Open in its own window')} size="sm" onClick={() => window.open(sel.url, '_blank', 'noopener')} testId="app-dock-external" />
            <IconButton icon={X} label={t('Close the app')} size="sm" onClick={() => void close()} testId="app-dock-close" />
          </div>
          {bridge === 'none' && !showPicker && (
            <p className="fs-appdock__note" role="status" data-testid="app-dock-nobridge">{t('The agent cannot act in this app yet. You can still use it here.')}</p>
          )}
          <div className="fs-appdock__stage" hidden={showPicker}>
            <iframe
              key={sel.appId + ':' + sel.url + ':' + reloads}
              ref={frameRef}
              className="fs-appdock__frame"
              src={sel.url}
              title={sel.name}
              allow="clipboard-read; clipboard-write; fullscreen"
              allowFullScreen
              onLoad={onLoad}
              data-testid="app-dock-frame"
            />
            {!loaded && (
              <div className="fs-appdock__loading" role="status">
                <span className="fs-appdock__spin" aria-hidden="true" />
                {t('Loading {name}…', { name: sel.name })}
              </div>
            )}
          </div>
        </>
      )}
      {showPicker && (
        <Picker sessionId={sessionId} current={sel?.appId ?? null} onPicked={() => setPicking(false)} onCancel={sel ? () => setPicking(false) : undefined} />
      )}
    </div>
  );
}
