import {
  AlertCircle, Calendar, CheckCircle2, ChevronDown, ExternalLink, HelpCircle, Loader2,
  Mail, MinusCircle, Play, Plug, Plus, PowerOff, RefreshCw, Settings2, Wrench, XCircle,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router';
import { Button, EmptyState, IconButton, QuickMenu, Skeleton, Toast } from '../../components';
import {
  checkConnector,
  connectConnector,
  deleteConnector,
  disconnectConnector,
  launchConnector,
  listConnectors,
  listPresets,
  openConnector,
  stateLabel,
  stateTone,
  statusLine,
  type Connector,
  type ConnectorPreset,
  type ConnectorState,
} from '../../adapters/connectors';
import { t, tn } from '../../i18n';
import { NewConnectorForm } from './NewConnectorForm';
import { LaunchProfilesPanel } from './LaunchProfiles';
import { ConnectorToolsDrawer } from './ToolsDrawer';
import '../projects.css';
import '../settings.css';
import './connectors.css';

/**
 * Connectors (CONTRATO_CONECTORES Lote F3, Fase C UI).
 *
 * One unified list: Hoard presets (Jobhunter, Writer…) alongside every other
 * MCP server, exactly as `GET /api/app-connectors` returns them (F1.5) — a
 * server without a sidecar shows up with `preset: null` and still gets the
 * same row. Mail and Calendar are NOT duplicated here: they already have a
 * real form in Settings → Integrations, and this screen only links to it
 * (contract: "no duplicar formularios").
 *
 * Every state shown is a state F1 computed from a real check — this screen
 * never infers "connected" from a tool list existing, and never hides
 * `unknown` inside a friendlier bucket.
 */

const STATE_ICON: Record<ConnectorState, typeof CheckCircle2> = {
  unconfigured: HelpCircle,
  app_off: PowerOff,
  connecting: Loader2,
  available: CheckCircle2,
  error: XCircle,
  disabled: MinusCircle,
  unknown: AlertCircle,
};

function StateChip({ state }: { state: ConnectorState }) {
  const Icon = STATE_ICON[state] ?? HelpCircle;
  return (
    <span className="fs-conn-status" data-tone={stateTone(state)} data-testid={`connector-state-${state}`}>
      <Icon size={13} aria-hidden="true" className={state === 'connecting' ? 'fs-status__spin' : undefined} />
      {stateLabel(state)}
    </span>
  );
}

function reasonForDisabledAction(c: Connector, action: 'launch' | 'open' | 'connect'): string | null {
  if (action === 'launch' && !c.launch_profile_id) return t('Configure a launch profile first.');
  if (action === 'connect' && c.status.state === 'unconfigured') return t('Finish setup first — some placeholders are still missing.');
  if (action === 'connect' && c.status.state === 'disabled') return t('This connector is disabled.');
  return null;
}

function ConnectorRow({
  connector,
  onChanged,
  onOpenTools,
  onEdit,
  onNotice,
}: {
  connector: Connector;
  onChanged: () => void;
  onOpenTools: () => void;
  onEdit: () => void;
  onNotice: (msg: string) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const c = connector;
  // The toggle is about the ADAPTER (the MCP session), not the whole
  // connector: with the app off the adapter can still be connected (a stdio
  // bridge answers tools/list on its own), and the honest label then is
  // "Disconnect", not an invitation to connect what already is.
  const connected = c.status.adapter.mcp_status === 'connected' || c.server.status === 'connected';

  const run = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key);
    try {
      await fn();
      onChanged();
    } catch (e) {
      onNotice((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const canLaunch = !!c.launch_profile_id;
  const canOpen = !!(c.app_url || c.ui_url || c.preset?.ui_url_default);

  return (
    <li className="fs-conn__row" data-testid="connector-row" data-state={c.status.state}>
      <div className="fs-conn__row-main">
        <span className="fs-conn__identity">
          <strong>{c.server.name || c.preset?.name || c.id}</strong>
          {c.preset?.purpose && <span className="fs-set__help">{t(c.preset.purpose)}</span>}
        </span>
        <StateChip state={c.status.state} />
      </div>

      <div className="fs-conn__row-detail">
        <span className="fs-set__help">{statusLine(c.status)}</span>
        {c.status.reasons.length > 0 && (
          <button type="button" className="fs-link" onClick={() => setExpanded((v) => !v)} data-testid="connector-reasons-toggle">
            <ChevronDown size={12} aria-hidden="true" style={{ rotate: expanded ? '180deg' : '0deg' }} /> {tn(c.status.reasons.length, '{n} reason', '{n} reasons')}
          </button>
        )}
      </div>
      {expanded && c.status.reasons.length > 0 && (
        <ul className="fs-conn__reasons" data-testid="connector-reasons">
          {c.status.reasons.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      )}

      <div className="fs-conn__actions">
        <Button
          size="sm"
          variant={connected ? 'secondary' : 'primary'}
          label={connected ? t('Disconnect') : t('Connect')}
          loading={busy === 'toggle'}
          disabled={!!busy || !!reasonForDisabledAction(c, 'connect')}
          title={reasonForDisabledAction(c, 'connect') ?? undefined}
          onClick={() => void run('toggle', () => (connected ? disconnectConnector(c.id) : connectConnector(c.id)))}
          testId="connector-toggle"
        />
        <IconButton
          icon={RefreshCw}
          label={t('Check now')}
          size="sm"
          disabled={!!busy}
          onClick={() => void run('check', () => checkConnector(c.id))}
          testId="connector-check"
        />
        <QuickMenu
          label={t('More actions')}
          icon={Settings2}
          testId="connector-menu"
          items={[
            {
              label: t('Start the app'),
              icon: Play,
              disabled: !canLaunch || !!busy,
              onSelect: () =>
                void run('launch', async () => {
                  const r = await launchConnector(c.id);
                  onNotice(r.already_running ? t('Already running.') : r.ready === false ? t('Started, but not answering yet.') : t('Started.'));
                }),
            },
            !canLaunch ? { label: reasonForDisabledAction(c, 'launch') ?? '', disabled: true, onSelect: () => {} } : null,
            {
              label: t('Open the app'),
              icon: ExternalLink,
              disabled: !canOpen || !!busy,
              onSelect: () =>
                void run('open', async () => {
                  const r = await openConnector(c.id);
                  if (r.kind === 'url') window.open(r.url, '_blank', 'noopener');
                  else onNotice(r.already_running ? t('Already running.') : t('Launched.'));
                }),
            },
            { label: t('View tools'), icon: Wrench, disabled: !!busy, onSelect: onOpenTools },
            c.preset ? { label: t('Edit'), onSelect: onEdit } : null,
            null,
            {
              label: t('Remove'),
              variant: 'danger',
              disabled: !!busy,
              onSelect: () => {
                if (!window.confirm(t('Remove "{name}"?', { name: c.server.name }))) return;
                void run('delete', () => deleteConnector(c.id));
              },
            },
          ]}
        />
      </div>
    </li>
  );
}

export function ConnectorsScreen() {
  const [params, setParams] = useSearchParams();
  const [connectors, setConnectors] = useState<Connector[] | null>(null);
  const [presets, setPresets] = useState<ConnectorPreset[]>([]);
  const [failed, setFailed] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [form, setForm] = useState<{ preset: ConnectorPreset; existing?: Connector } | null>(null);
  const [showProfiles, setShowProfiles] = useState(false);
  const noticeTimer = useRef<number | null>(null);

  const say = useCallback((msg: string) => {
    setNotice(msg);
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), 2600);
  }, []);

  const reload = useCallback((check = false) => {
    Promise.all([listConnectors(check), listPresets()])
      .then(([c, p]) => {
        setConnectors(c);
        setPresets(p);
        setFailed(false);
      })
      .catch(() => {
        setConnectors([]);
        setFailed(true);
      });
  }, []);

  useEffect(() => {
    reload(true);
  }, [reload]);

  const drawerId = params.get('id');
  const drawerConnector = useMemo(() => (connectors ?? []).find((c) => c.id === drawerId) ?? null, [connectors, drawerId]);
  const closeDrawer = () => {
    const next = new URLSearchParams(params);
    next.delete('id');
    setParams(next, { replace: true });
  };

  const presetsInUse = new Set((connectors ?? []).map((c) => c.preset_id).filter(Boolean) as string[]);
  const availablePresets = presets.filter((p) => !presetsInUse.has(p.id));

  return (
    <div className="fs-screen fs-conn" data-testid="connectors-screen">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Connectors')}</h1>
          <p className="fs-prose">{t('Every app the agent can reach through MCP, in one place: what it does, whether it answers, and how many tools are enabled.')}</p>
        </div>
        <div className="fs-set__row-actions">
          <IconButton icon={RefreshCw} label={t('Refresh (check now)')} onClick={() => reload(true)} testId="connectors-refresh" />
          <Button size="sm" variant="ghost" icon={Settings2} label={t('Launch profiles')} onClick={() => setShowProfiles(true)} testId="connectors-profiles" />
          <Button size="sm" variant="primary" icon={Plus} label={t('Add')} onClick={() => setAddOpen((v) => !v)} testId="connectors-add" />
        </div>
      </header>

      {addOpen && (
        <div className="fs-conn__add" role="group" aria-label={t('Add a connector')}>
          {availablePresets.map((p) => (
            <button key={p.id} type="button" className="fs-chip" onClick={() => { setAddOpen(false); setForm({ preset: p }); }} data-testid={`connector-add-${p.id}`}>
              <Plug size={13} aria-hidden="true" /> {p.name}
            </button>
          ))}
          <Link to="/settings?section=integrations" className="fs-chip" onClick={() => setAddOpen(false)}>
            <Wrench size={13} aria-hidden="true" /> {t('Other MCP server…')}
          </Link>
          <Link to="/settings?section=integrations" className="fs-chip" onClick={() => setAddOpen(false)}>
            <Mail size={13} aria-hidden="true" /> {t('Mail account…')}
          </Link>
          <Link to="/calendar" className="fs-chip" onClick={() => setAddOpen(false)}>
            <Calendar size={13} aria-hidden="true" /> {t('Calendar…')}
          </Link>
        </div>
      )}

      {form && (
        <div className="fs-set__card fs-conn__form" data-testid="connector-form">
          <NewConnectorForm
            preset={form.preset}
            existing={form.existing}
            onClose={() => setForm(null)}
            onSaved={(c) => {
              setForm(null);
              say(t('Saved.'));
              reload(true);
              const next = new URLSearchParams(params);
              next.set('id', c.id);
              setParams(next, { replace: true });
            }}
          />
        </div>
      )}

      {showProfiles && <LaunchProfilesPanel onClose={() => setShowProfiles(false)} />}

      {failed ? (
        <EmptyState
          icon={Plug}
          tone="error"
          title={t('Could not read the connector list.')}
          body={t('GET /api/app-connectors failed — it may not be wired up yet on this build.')}
          primaryAction={{ label: t('Try again'), onClick: () => reload(true) }}
        />
      ) : connectors === null ? (
        <Skeleton label={t('Loading')} count={3} height="72px" />
      ) : connectors.length === 0 ? (
        <EmptyState icon={Plug} title={t('No connectors yet')} body={t('Add a Hoard preset or another MCP server above.')} />
      ) : (
        <ul className="fs-conn__list" data-testid="connectors-list">
          {connectors.map((c) => (
            <ConnectorRow
              key={c.id}
              connector={c}
              onChanged={() => reload(true)}
              onOpenTools={() => {
                const next = new URLSearchParams(params);
                next.set('id', c.id);
                setParams(next, { replace: true });
              }}
              onEdit={() => {
                const preset = presets.find((p) => p.id === c.preset_id);
                if (preset) setForm({ preset, existing: c });
              }}
              onNotice={say}
            />
          ))}
        </ul>
      )}

      {drawerConnector && <ConnectorToolsDrawer connector={drawerConnector} onClose={closeDrawer} />}

      {notice && (
        <Toast>
          <CheckCircle2 size={12} aria-hidden="true" /> {notice}
        </Toast>
      )}
    </div>
  );
}
