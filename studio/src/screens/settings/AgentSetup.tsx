import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button } from '../../components';
import { t } from '../../i18n';
import {
  applyImport,
  getDelegationRoutes,
  previewImport,
  resyncImport,
  saveDelegationRoutes,
  spotifyLogout,
  spotifyStatus,
  xchatSetup,
  xchatStatus,
  type DelegationRoute,
  type ImportAgent,
  type ImportResult,
  type ImportSource,
  type SpotifyStatus as SpotifyStatusT,
  type XChatStatus as XChatStatusT,
} from '../../adapters/agentSetup';

/* ── Named delegation routes (sub-agents group of the agent settings) ── */

const EMPTY_ROUTE: DelegationRoute = { name: '', description: '', model: '', endpoint_id: '', effort: '', agent: '', category: '', skills: [] };
const EFFORTS = ['', 'low', 'medium', 'high', 'max'];

export function DelegationRoutesPanel() {
  const [routes, setRoutes] = useState<DelegationRoute[]>([]);
  const [def, setDef] = useState('');
  const [preview, setPreview] = useState('');
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    getDelegationRoutes()
      .then((d) => {
        setRoutes(d.routes);
        setDef(d.default);
        setPreview(d.schema?.description ?? '');
      })
      .catch((e: Error) => setMsg({ ok: false, text: e.message }));
  }, []);

  const edit = (i: number, key: keyof DelegationRoute, value: string) =>
    setRoutes((rs) => rs.map((r, j) => (j !== i ? r : { ...r, [key]: key === 'skills' ? value.split(',').map((s) => s.trim()).filter(Boolean) : value })));

  const save = async () => {
    setSaving(true);
    setMsg(null);
    try {
      const d = await saveDelegationRoutes(routes, def);
      setRoutes(d.routes);
      setDef(d.default);
      setPreview(d.schema?.description ?? '');
      setMsg({ ok: true, text: t('Routes saved. New turns offer them to the coordinator.') });
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fs-set__card" data-testid="delegation-routes">
      <div className="fs-set__row-between">
        <h3 className="fs-set__card-title">{t('Delegation routes')}</h3>
        <Button size="sm" variant="ghost" label={t('Add route')} onClick={() => setRoutes((rs) => [...rs, { ...EMPTY_ROUTE }])} />
      </div>
      <p className="fs-set__help">
        {t('The coordinator picks one per sub-agent by name. Only the name and the description reach the model; the rest says what the worker runs on.')}
      </p>
      {routes.map((r, i) => (
        <div key={i} className="fs-set__route" data-testid="delegation-route" style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: '6px', marginBottom: '10px' }}>
          <input className="fs-field" aria-label={t('Name')} placeholder={t('Name')} value={r.name} onChange={(e) => edit(i, 'name', e.target.value)} />
          <input className="fs-field" style={{ gridColumn: 'span 3' }} aria-label={t('Description')} placeholder={t('One line: when to use it')} value={r.description} onChange={(e) => edit(i, 'description', e.target.value)} />
          <input className="fs-field" aria-label={t('Model')} placeholder={t('Model')} value={r.model} onChange={(e) => edit(i, 'model', e.target.value)} />
          <input className="fs-field" aria-label={t('Endpoint id')} placeholder={t('Endpoint id')} value={r.endpoint_id} onChange={(e) => edit(i, 'endpoint_id', e.target.value)} />
          <select className="fs-field" aria-label={t('Effort')} value={r.effort} onChange={(e) => edit(i, 'effort', e.target.value)}>
            {EFFORTS.map((x) => (
              <option key={x} value={x}>{x || t('Effort: inherit')}</option>
            ))}
          </select>
          <input className="fs-field" aria-label={t('Agent profile')} placeholder={t('Agent profile')} value={r.agent} onChange={(e) => edit(i, 'agent', e.target.value)} />
          <input className="fs-field" aria-label={t('Tool categories')} placeholder={t('Tool categories')} value={r.category} onChange={(e) => edit(i, 'category', e.target.value)} />
          <input className="fs-field" style={{ gridColumn: 'span 2' }} aria-label={t('Skills')} placeholder={t('Skills, comma-separated')} value={r.skills.join(', ')} onChange={(e) => edit(i, 'skills', e.target.value)} />
          <Button size="sm" variant="ghost" label={t('Remove')} onClick={() => setRoutes((rs) => rs.filter((_, j) => j !== i))} />
        </div>
      ))}
      <div className="fs-set__row-between">
        <label className="fs-set__label">
          {t('Default route')}{' '}
          <select className="fs-field" aria-label={t('Default route')} value={def} onChange={(e) => setDef(e.target.value)}>
            <option value="">{t("None: the coordinator's model")}</option>
            {routes.filter((r) => r.name).map((r) => (
              <option key={r.name} value={r.name}>{r.name}</option>
            ))}
          </select>
        </label>
        <Button size="sm" label={saving ? t('Saving…') : t('Save routes')} loading={saving} onClick={() => void save()} />
      </div>
      {msg && <p className="fs-set__help" data-tone={msg.ok ? 'ok' : 'bad'} role="status">{msg.text}</p>}
      {preview && <p className="fs-set__help" data-testid="delegation-routes-preview">{t('What the model reads: {text}', { text: preview })}</p>}
    </div>
  );
}

/* ── Import from Claude Code, Codex CLI and Cursor ── */

const AGENTS: { value: ImportAgent; label: string }[] = [
  { value: 'claude-code', label: 'Claude Code' },
  { value: 'codex', label: 'Codex CLI' },
  { value: 'cursor', label: 'Cursor' },
];
const KIND_LABEL: Record<string, string> = { instructions: 'Instructions', memory: 'Memory', mcp: 'MCP server', skill: 'Skill', agent: 'Agent' };
const STATUS_LABEL: Record<string, string> = { new: 'new', changed: 'changed', unchanged: 'unchanged', exists: 'already exists' };

export function AgentImportSection() {
  const [agents, setAgents] = useState<ImportAgent[]>(['claude-code', 'codex', 'cursor']);
  const [sources, setSources] = useState<ImportSource[] | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState('');
  const [result, setResult] = useState<ImportResult | null>(null);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setBusy('preview');
    setError('');
    setResult(null);
    try {
      const out = await previewImport(agents);
      setSources(out.sources);
      setPicked(new Set(out.sources.flatMap((s) => s.items.filter((i) => i.status === 'new' || i.status === 'changed').map((i) => i.key))));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy('');
    }
  }, [agents]);

  const total = useMemo(() => (sources ?? []).reduce((n, s) => n + s.items.length, 0), [sources]);

  const run = async (what: 'apply' | 'resync') => {
    setBusy(what);
    setError('');
    try {
      setResult(what === 'apply' ? await applyImport(agents, [...picked]) : await resyncImport(false));
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  const toggle = (key: string) => setPicked((p) => {
    const next = new Set(p);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    return next;
  });

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-agent-import" data-testid="agent-import">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-agent-import" className="fs-set__title">{t('Import from other agents')}</h2>
          <p className="fs-prose">
            {t('Brings the instructions, memories, MCP servers, skills and agents of Claude Code, Codex CLI and Cursor into Faustus. Preview first; a re-sync imports again what changed there.')}
          </p>
        </div>
      </header>
      <div className="fs-set__card">
        <div className="fs-set__row-between">
          <div>
            {AGENTS.map((a) => (
              <label key={a.value} style={{ marginRight: '12px', whiteSpace: 'nowrap' }}>
                <input type="checkbox" checked={agents.includes(a.value)} onChange={(e) => setAgents((xs) => (e.target.checked ? [...xs, a.value] : xs.filter((x) => x !== a.value)))} /> {a.label}
              </label>
            ))}
          </div>
          <div>
            <Button size="sm" variant="ghost" label={t('Preview')} loading={busy === 'preview'} disabled={!agents.length} onClick={() => void load()} />{' '}
            <Button size="sm" variant="ghost" label={t('Re-sync')} loading={busy === 'resync'} onClick={() => void run('resync')} />{' '}
            <Button size="sm" label={t('Import {n} selected', { n: picked.size })} loading={busy === 'apply'} disabled={!picked.size} onClick={() => void run('apply')} />
          </div>
        </div>
        {error && <p className="fs-set__help" data-tone="bad" role="alert">{error}</p>}
        {result && (
          <p className="fs-set__help" data-tone={result.failed?.length ? 'warn' : 'ok'} role="status" data-testid="agent-import-result">
            {t('Imported {n}, failed {f}.', { n: result.imported?.length ?? 0, f: result.failed?.length ?? 0 })}
            {result.failed?.length ? ' ' + result.failed.map((f) => `${f.key}: ${f.error}`).join('; ') : ''}
          </p>
        )}
        {sources && total === 0 && <p className="fs-set__help">{t('Nothing to import was found.')}</p>}
        {sources?.map((s) => (
          <div key={s.agent} data-testid={`agent-import-${s.agent}`}>
            <h4 className="fs-set__card-title">
              {AGENTS.find((a) => a.value === s.agent)?.label} <code className="fs-set__key">{s.root}</code>
            </h4>
            {!s.found && <p className="fs-set__help">{t('Not installed here.')}</p>}
            {s.items.map((i) => (
              <label key={i.key} className="fs-set__help" style={{ display: 'block' }} data-status={i.status}>
                <input type="checkbox" checked={picked.has(i.key)} onChange={() => toggle(i.key)} /> <strong>{t(KIND_LABEL[i.kind])}</strong> {i.name}{' '}
                <em>({t(STATUS_LABEL[i.status])})</em> — {i.summary}
                {i.warnings.length > 0 && <span data-tone="warn"> · {i.warnings.join('; ')}</span>}
              </label>
            ))}
            {s.skipped.map((x) => (
              <p key={x} className="fs-set__help">{x}</p>
            ))}
          </div>
        ))}
      </div>
    </section>
  );
}

/* ── Spotify (its group of the agent settings) ── */

export function SpotifyStatusCard({ savedKey }: { savedKey: string }) {
  const [status, setStatus] = useState<SpotifyStatusT | null>(null);
  const [error, setError] = useState('');
  const refresh = useCallback(() => {
    spotifyStatus().then(setStatus, (e: Error) => setError(e.message));
  }, []);
  useEffect(() => refresh(), [refresh, savedKey]);
  let line = t('Loading…');
  if (status) {
    line = status.logged_in ? t('Connected.') : status.configured ? t('Client ID saved; not connected yet.') : t('Not configured.');
  }
  return (
    <div className="fs-set__card" data-testid="spotify-status">
      <div className="fs-set__row-between">
        <h3 className="fs-set__card-title">Spotify</h3>
        <div>
          <a className="fs-btn" data-size="sm" data-variant="primary" href="/api/spotify/login" target="_blank" rel="noreferrer" aria-disabled={!status?.configured || undefined}>
            {status?.logged_in ? t('Connect again') : t('Connect')}
          </a>{' '}
          {status?.logged_in && <Button size="sm" variant="ghost" label={t('Disconnect')} onClick={() => void spotifyLogout().then(refresh)} />}
        </div>
      </div>
      {error ? <p className="fs-set__help" data-tone="bad">{error}</p> : <p className="fs-set__help" role="status" data-tone={status?.logged_in ? 'ok' : 'warn'}>{line}</p>}
      {status && <p className="fs-set__help">{t('Redirect URI to register in your Spotify app: {uri}', { uri: status.redirect_uri })}</p>}
      {status?.setup && <p className="fs-set__help">{status.setup}</p>}
    </div>
  );
}

/* ── X self-chat (its group of the agent settings) ── */

export function XChatStatusCard({ savedKey }: { savedKey: string }) {
  const [status, setStatus] = useState<XChatStatusT | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const refresh = useCallback(() => {
    xchatStatus().then(setStatus, (e: Error) => setMsg({ ok: false, text: e.message }));
  }, []);
  useEffect(() => refresh(), [refresh, savedKey]);
  const setup = async () => {
    setBusy(true);
    try {
      const out = await xchatSetup();
      setMsg({ ok: true, text: t('Encryption identity ready for account {id}.', { id: out.username ? '@' + out.username : out.user_id }) });
      refresh();
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };
  let line = t('Loading…');
  if (status) {
    line = status.running ? t('Running: answering your self-chat.') : status.enabled ? t('Enabled, not running.') : t('Off.');
  }
  return (
    <div className="fs-set__card" data-testid="xchat-status">
      <div className="fs-set__row-between">
        <h3 className="fs-set__card-title">{t('X self-chat')}</h3>
        <Button size="sm" variant="ghost" label={t('Register encryption identity')} loading={busy} onClick={() => void setup()} />
      </div>
      <p className="fs-set__help" role="status" data-tone={status?.running ? 'ok' : 'warn'}>{line}</p>
      {status && status.missing.length > 0 && <p className="fs-set__help">{t('Missing: {what}', { what: status.missing.join('; ') })}</p>}
      {status?.last_error && <p className="fs-set__help" data-tone="bad">{t('Last error: {error}', { error: status.last_error })}</p>}
      {msg && <p className="fs-set__help" data-tone={msg.ok ? 'ok' : 'bad'}>{msg.text}</p>}
    </div>
  );
}
