import { Copy } from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';
import { Button } from '../../components';
import {
  AGENT_CONFIGS,
  AGENT_SCOPES,
  apiPresets,
  createToken,
  deleteToken,
  saveApiIntegration,
  saveCalDav,
  saveVaultConfig,
  testApiIntegration,
  testCalDav,
  updateToken,
  vaultConfig,
  vaultLock,
  vaultLogin,
  vaultLogout,
  vaultUnlock,
  type ApiIntegration,
  type ApiPreset,
  type ApiToken,
  type CalDavAccount,
  type VaultConfig,
} from '../../adapters/integrations';
import { locale, t } from '../../i18n';
import { Field, Select, Toggle } from './fields';

export function FormFoot({ msg, tone, children }: { msg?: string | null; tone?: 'ok' | 'bad'; children: ReactNode }) {
  return (
    <div className="fs-set__row-end">
      {msg && (
        <span className="fs-set__err" data-tone={tone}>
          {msg}
        </span>
      )}
      {children}
    </div>
  );
}

export function useMsg() {
  const [msg, setMsg] = useState<{ text: string; tone: 'ok' | 'bad' } | null>(null);
  return { msg: msg?.text ?? null, tone: msg?.tone, good: (s: string) => setMsg({ text: s, tone: 'ok' }), bad: (s: string) => setMsg({ text: s, tone: 'bad' }), clear: () => setMsg(null) };
}

/* ── API key ── */

const AUTH_TYPES = [
  { value: 'bearer', label: 'Bearer (most common)' },
  { value: 'header', label: 'Header' },
  { value: 'basic', label: 'Basic' },
  { value: 'none', label: 'None' },
];

export function ApiForm({ existing, onClose, onChanged, say }: { existing?: ApiIntegration; onClose: () => void; onChanged: () => void; say: (t: string) => void }) {
  const [presets, setPresets] = useState<Record<string, ApiPreset>>({});
  const [preset, setPreset] = useState(existing?.preset ?? '');
  const [name, setName] = useState(existing?.name ?? '');
  const [url, setUrl] = useState(existing?.base_url ?? '');
  const [auth, setAuth] = useState(existing?.auth_type ?? 'bearer');
  const [header, setHeader] = useState(existing?.auth_header ?? '');
  const [key, setKey] = useState('');
  const [id, setId] = useState<string | null>(existing?.id ?? null);
  const [busy, setBusy] = useState(false);
  const m = useMsg();
  useEffect(() => {
    apiPresets().then(setPresets).catch(() => {});
  }, []);
  const applyPreset = (k: string) => {
    setPreset(k);
    const p = presets[k];
    if (!p) return;
    setName(p.name ?? '');
    setUrl(p.base_url ?? '');
    setAuth(p.auth_type ?? 'none');
    setHeader(p.auth_header ?? '');
  };
  const urlAuth = preset === 'discord_webhook';
  const options = [{ value: '', label: t('Custom (no preset)') }, ...Object.entries(presets).sort((a, b) => (a[1].name || a[0]).localeCompare(b[1].name || b[0])).map(([k, p]) => ({ value: k, label: p.name || k }))];

  const save = async () => {
    m.clear();
    if (!name.trim()) return m.bad(t('A name is required.'));
    if (!url.trim()) return m.bad(t('The base URL is required.'));
    setBusy(true);
    try {
      const saved = await saveApiIntegration(id, { name: name.trim(), base_url: url.trim(), auth_type: auth, auth_header: header, preset: preset || undefined, ...(key ? { api_key: key } : {}) });
      setId(saved || id);
      setKey('');
      m.good(t('Saved.'));
      say(t('Integration saved.'));
      onChanged();
    } catch (e) {
      m.bad((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const test = async () => {
    if (!id) return m.bad(t('Save first.'));
    setBusy(true);
    try {
      const r = await testApiIntegration(id);
      (r.ok ? m.good : m.bad)(r.message ?? (r.ok ? 'OK' : t('Failed')));
    } catch {
      m.bad(t('Connection failed.'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <h3 className="fs-set__card-title">{existing ? t('API integration') : t('New API integration')}</h3>
      <Field label={t('Preset')} htmlFor="api-preset">
        <Select id="api-preset" value={preset} options={options} onChange={applyPreset} />
      </Field>
      <div className="fs-set__grid2">
        <Field label={t('Name')} htmlFor="api-name">
          <input id="api-name" className="fs-field" value={name} onChange={(e) => setName(e.target.value)} placeholder="My service" />
        </Field>
        <Field label={t('Base URL')} htmlFor="api-url">
          <input id="api-url" className="fs-field" value={url} onChange={(e) => setUrl(e.target.value)} placeholder={preset === 'ntfy' ? 'http://127.0.0.1:8091' : urlAuth ? 'https://discord.com/api/webhooks/…' : 'http://localhost:8080'} />
        </Field>
      </div>
      {!urlAuth && (
        <div className="fs-set__grid2">
          <Field label={t('Auth')} htmlFor="api-auth" help={t('Bearer sends "Authorization: Bearer KEY"; Header sends the key under a header you name; Basic is user:pass; None for open APIs.')}>
            <Select id="api-auth" value={auth} options={AUTH_TYPES.map((o) => ({ value: o.value, label: t(o.label) }))} onChange={setAuth} />
          </Field>
          {auth === 'header' && (
            <Field label={t('Header')} htmlFor="api-header" help={t('Miniflux: X-Auth-Token; most others: Authorization.')}>
              <input id="api-header" className="fs-field" value={header} onChange={(e) => setHeader(e.target.value)} />
            </Field>
          )}
        </div>
      )}
      {!urlAuth && (
        <Field label={t('API key')} htmlFor="api-key" help={existing?.has_key !== false && existing ? t('Leave blank to keep the saved key.') : t('The secret the service issued you.')}>
          <input id="api-key" type="password" className="fs-field" value={key} onChange={(e) => setKey(e.target.value)} autoComplete="new-password" />
        </Field>
      )}
      {preset === 'ntfy' && <p className="fs-set__help">{t('ntfy: the base URL is the server; the topic goes in the reminders settings.')}</p>}
      <FormFoot msg={m.msg} tone={m.tone}>
        <Button size="sm" variant="ghost" label={t('Close')} onClick={onClose} />
        <Button size="sm" variant="secondary" label={t('Test')} disabled={busy} onClick={() => void test()} />
        <Button size="sm" variant="primary" label={t('Save')} loading={busy} onClick={() => void save()} />
      </FormFoot>
    </>
  );
}

/* ── CalDAV ── */

export function CalDavForm({ existing, onClose, onChanged, say }: { existing?: CalDavAccount; onClose: () => void; onChanged: () => void; say: (t: string) => void }) {
  const [label, setLabel] = useState(existing?.label ?? '');
  const [url, setUrl] = useState(existing?.url ?? '');
  const [user, setUser] = useState(existing?.username ?? '');
  const [pw, setPw] = useState('');
  const [busy, setBusy] = useState(false);
  const m = useMsg();
  const test = async () => {
    m.clear();
    setBusy(true);
    try {
      const r = await testCalDav({ url: url.trim(), username: user.trim(), password: pw || undefined, ...(existing && !pw ? { account_id: existing.id } : {}) });
      (r.ok ? m.good : m.bad)(r.message ?? (r.ok ? t('Connected: {n} calendars', { n: r.calendars?.length ?? 0 }) : t('Failed')));
    } catch (e) {
      m.bad((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const save = async () => {
    m.clear();
    if (!url.trim()) return m.bad(t('The server URL is required.'));
    setBusy(true);
    try {
      await saveCalDav(existing?.id ?? null, { label: label.trim(), url: url.trim(), username: user.trim(), ...(pw ? { password: pw } : {}) });
      say(t('CalDAV account saved.'));
      onClose();
    } catch (e) {
      m.bad((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <h3 className="fs-set__card-title">{existing ? t('CalDAV account') : t('New CalDAV account')}</h3>
      <div className="fs-set__grid2">
        <Field label={t('Label')} htmlFor="cd-label">
          <input id="cd-label" className="fs-field" value={label} onChange={(e) => setLabel(e.target.value)} placeholder={t('e.g. Work, Personal')} />
        </Field>
        <Field label={t('Server URL')} htmlFor="cd-url">
          <input id="cd-url" className="fs-field" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://www.google.com/calendar/dav/you@gmail.com/user" />
        </Field>
        <Field label={t('Username')} htmlFor="cd-user">
          <input id="cd-user" className="fs-field" value={user} onChange={(e) => setUser(e.target.value)} autoComplete="off" />
        </Field>
        <Field label={t('Password')} htmlFor="cd-pw" help={existing ? t('Leave blank to keep the saved one.') : undefined}>
          <input id="cd-pw" type="password" className="fs-field" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="new-password" />
        </Field>
      </div>
      <FormFoot msg={m.msg} tone={m.tone}>
        <Button size="sm" variant="ghost" label={t('Close')} onClick={onClose} />
        <Button size="sm" variant="secondary" label={t('Test')} disabled={busy} onClick={() => void test()} />
        <Button size="sm" variant="primary" label={t('Save')} loading={busy} onClick={() => void save()} />
      </FormFoot>
    </>
  );
}

/* ── Vault ── */

export function VaultForm({ onClose, onChanged, say }: { onClose: () => void; onChanged: () => void; say: (t: string) => void }) {
  const [cfg, setCfg] = useState<VaultConfig | null>(null);
  const [url, setUrl] = useState('');
  const [email, setEmail] = useState('');
  const [pw, setPw] = useState('');
  const [busy, setBusy] = useState(false);
  const m = useMsg();
  const refresh = () =>
    vaultConfig()
      .then((c) => {
        setCfg(c);
        setUrl(c.server_url ?? '');
        setEmail(c.email ?? '');
      })
      .catch(() => m.bad(t('Could not read the vault status.')));
  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const run = async (fn: () => Promise<string>) => {
    m.clear();
    setBusy(true);
    try {
      m.good(await fn());
      await refresh();
      onChanged();
    } catch (e) {
      m.bad((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const status = cfg ? [cfg.bw_installed ? t('bw CLI installed') : t('bw CLI NOT installed (install nodejs-bitwarden-cli)'), cfg.unlocked ? t('unlocked') : t('locked'), cfg.unlocked_at ? t('last unlock {when}', { when: new Date(cfg.unlocked_at).toLocaleString(locale()) }) : ''].filter(Boolean).join(' — ') : t('Loading');
  return (
    <>
      <h3 className="fs-set__card-title">{t('Vault (Bitwarden / Vaultwarden)')}</h3>
      <p className="fs-set__help" data-tone={cfg && !cfg.bw_installed ? 'bad' : cfg?.unlocked ? 'ok' : undefined}>
        {status}
      </p>
      <div className="fs-set__grid2">
        <Field label={t('Server URL')} htmlFor="v-url">
          <input id="v-url" className="fs-field" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://vault.example.com" />
        </Field>
        <Field label={t('Email')} htmlFor="v-email">
          <input id="v-email" className="fs-field" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" />
        </Field>
      </div>
      <Field label={t('Master password')} htmlFor="v-pw" help={t('Only for Log in / Unlock; it is never stored.')}>
        <input id="v-pw" type="password" className="fs-field" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="off" />
      </Field>
      <FormFoot msg={m.msg} tone={m.tone}>
        <Button size="sm" variant="ghost" label={t('Close')} onClick={onClose} />
        <Button size="sm" variant="secondary" label={t('Save config')} disabled={busy} onClick={() => void run(async () => { await saveVaultConfig({ server_url: url, email }); return t('Saved.'); })} />
        <Button size="sm" variant="secondary" label={t('Log in')} disabled={busy || !pw} onClick={() => void run(async () => { const d = await vaultLogin(email, pw); if (d.error) throw new Error(d.error); return d.already ? t('Already logged in — use Unlock') : t('Logged in'); })} />
        <Button size="sm" variant="primary" label={t('Unlock')} disabled={busy || !pw} onClick={() => void run(async () => { const d = await vaultUnlock(pw); if (d.error) throw new Error(d.error); setPw(''); return t('Vault unlocked'); })} />
        <Button size="sm" variant="ghost" label={t('Lock')} disabled={busy} onClick={() => void run(async () => { await vaultLock(); return t('Locked'); })} />
        <Button size="sm" variant="danger" label={t('Log out')} disabled={busy} onClick={() => void run(async () => { await vaultLogout(); say(t('Vault logged out.')); return t('Logged out'); })} />
      </FormFoot>
    </>
  );
}

/* ── Codex / Claude agent tokens ── */

export function AgentForm({ kind, existing, onClose, onChanged, say }: { kind: 'codex' | 'claude'; existing?: ApiToken; onClose: () => void; onChanged: () => void; say: (t: string) => void }) {
  const cfg = AGENT_CONFIGS[kind];
  const [name, setName] = useState(existing?.name ?? cfg.defaultName);
  const [scopes, setScopes] = useState<Set<string>>(new Set(existing?.scopes ?? []));
  const [created, setCreated] = useState<{ id: string; token: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const m = useMsg();
  const id = existing?.id ?? created?.id ?? null;
  const origin = window.location.origin;

  const copy = async (text: string, what: string) => {
    try {
      await navigator.clipboard.writeText(text);
      say(t('{what} copied.', { what }));
    } catch {
      say(t('Could not copy.'));
    }
  };
  const toggleScope = async (key: string, on: boolean) => {
    const next = new Set(scopes);
    if (on) next.add(key);
    else next.delete(key);
    setScopes(next);
    if (!id) return;
    try {
      await updateToken(id, { scopes: ['chat', ...AGENT_SCOPES.filter((s) => next.has(s.key)).map((s) => s.key)] });
      onChanged();
    } catch (e) {
      m.bad((e as Error).message);
    }
  };
  const create = async () => {
    m.clear();
    setBusy(true);
    try {
      const d = await createToken(name.trim() || cfg.defaultName, ['chat']);
      setCreated(d);
      say(t('Token created. Copy it now; it is not shown again.'));
      onChanged();
    } catch (e) {
      m.bad((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const rename = async () => {
    if (!id || !existing) return;
    try {
      await updateToken(id, { name: name.trim() || cfg.defaultName });
      m.good(t('Renamed.'));
      onChanged();
    } catch (e) {
      m.bad((e as Error).message);
    }
  };
  const revoke = async () => {
    if (!id) return;
    if (!window.confirm(t('Revoke this {word} agent token? Whatever uses it loses access.', { word: cfg.word }))) return;
    try {
      await deleteToken(id);
      say(t('Token revoked.'));
      onClose();
    } catch (e) {
      m.bad((e as Error).message);
    }
  };

  return (
    <>
      <h3 className="fs-set__card-title">{t(cfg.label)}</h3>
      <p className="fs-set__help">{t('A token for {word} to call this Faustus from outside: notes, documents, mail, calendar, memory, cookbook and workers, each behind its own scope.', { word: cfg.word })}</p>
      <Field label={t('Token name')} htmlFor="ag-name">
        <div className="fs-set__inline">
          <input id="ag-name" className="fs-field" value={name} onChange={(e) => setName(e.target.value)} onBlur={() => void rename()} />
          {existing && (
            <span className="fs-set__help">
              {existing.token_prefix ?? 'token'}… {existing.last_used_at ? `· ${t('last used {when}', { when: new Date(existing.last_used_at).toLocaleDateString(locale()) })}` : `· ${t('never used')}`}
            </span>
          )}
        </div>
      </Field>
      {!id && (
        <FormFoot msg={m.msg} tone={m.tone}>
          <Button size="sm" variant="ghost" label={t('Close')} onClick={onClose} />
          <Button size="sm" variant="primary" label={t('Create the token')} loading={busy} onClick={() => void create()} />
        </FormFoot>
      )}
      {created && (
        <>
          <p className="fs-set__help">{t('Copy this token; it will not be shown again.')}</p>
          <div className="fs-set__inline">
            <code className="fs-set__secret fs-set__secret--wide">{created.token}</code>
            <Button size="sm" variant="secondary" icon={Copy} label={t('Copy')} onClick={() => void copy(created.token, t('Token'))} />
          </div>
          <p className="fs-set__help">{t('Set-up: downloads the plugin bundle and registers it.')}</p>
          <pre className="fs-set__pre">{cfg.buildSetup(origin, created.token)}</pre>
          <div className="fs-set__row-end">
            <Button size="sm" variant="secondary" icon={Copy} label={t('Copy the set-up')} onClick={() => void copy(cfg.buildSetup(origin, created.token), t('Set-up'))} />
          </div>
        </>
      )}
      {id && (
        <>
          <h4 className="fs-users__h">{t('Scopes')}</h4>
          <p className="fs-set__help">{t('"chat" is always on. Turn on only what {word} needs.', { word: cfg.word })}</p>
          <ul className="fs-scopes">
            {AGENT_SCOPES.map((s) => (
              <li key={s.key} className="fs-scopes__row">
                <span>
                  <strong>{t(s.label)}</strong> <code className="fs-tools__id">{s.key}</code>
                  <span className="fs-set__help">{t(s.detail)}</span>
                </span>
                <Toggle id={`sc-${s.key}`} checked={scopes.has(s.key)} onChange={(v) => void toggleScope(s.key, v)} />
              </li>
            ))}
          </ul>
          <FormFoot msg={m.msg} tone={m.tone}>
            <Button size="sm" variant="danger" label={t('Revoke')} onClick={() => void revoke()} />
            <Button size="sm" variant="ghost" label={t('Close')} onClick={onClose} />
          </FormFoot>
        </>
      )}
    </>
  );
}
