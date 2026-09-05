import { Download, Pencil, Plus, Trash2, Upload } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Button, IconButton, Skeleton } from '../../components';
import {
  addContact,
  addMcpServer,
  contactsConfig,
  deleteContact,
  EMAIL_PROVIDERS,
  exportContacts,
  googleOAuthUrl,
  importContacts,
  listContacts,
  listMcpServers,
  listMcpTools,
  mcpAuthorizeUrl,
  mcpOauthExchange,
  reconnectMcpServer,
  saveCardDav,
  saveEmailAccount,
  setMcpDisabledTools,
  testEmailAccount,
  toggleMcpServer,
  updateContact,
  type Contact,
  type EmailAccount,
  type EmailBody,
  type McpServer,
  type McpTool,
} from '../../adapters/integrations';
import { t, tn } from '../../i18n';
import { Field, Select, Toggle } from './fields';
import { FormFoot, useMsg } from './IntegrationForms';

/* ── mail account ── */

const SMTP_SECURITY = [
  { value: 'ssl', label: 'SSL (465)' },
  { value: 'starttls', label: 'STARTTLS (587)' },
  { value: 'none', label: 'None' },
];

function providerOf(a?: EmailAccount): string {
  if (!a) return '';
  if (a.oauth_provider === 'google') return 'google_workspace';
  const hit = Object.entries(EMAIL_PROVIDERS).find(([, p]) => p.imap.host && p.imap.host === a.imap_host && p.smtp.port === a.smtp_port);
  return hit?.[0] ?? '';
}

export function EmailForm({ existing, onClose, onChanged, say }: { existing?: EmailAccount; onClose: () => void; onChanged: () => void; say: (t: string) => void }) {
  const [provider, setProvider] = useState(providerOf(existing));
  const [f, setF] = useState({
    name: existing?.name ?? '',
    from: existing?.from_address ?? '',
    display: existing?.display_name ?? '',
    imapHost: existing?.imap_host ?? '',
    imapPort: String(existing?.imap_port ?? 993),
    imapUser: existing?.imap_user ?? '',
    imapPw: '',
    starttls: existing?.imap_starttls ?? false,
    smtpHost: existing?.smtp_host ?? '',
    smtpPort: String(existing?.smtp_port ?? 465),
    smtpSecurity: existing?.smtp_security ?? (Number(existing?.smtp_port ?? 465) === 587 ? 'starttls' : 'ssl'),
    smtpSame: !existing?.smtp_user || existing.smtp_user === existing.imap_user,
    smtpUser: existing?.smtp_user ?? '',
    smtpPw: '',
    isDefault: existing?.is_default ?? false,
  });
  const set = (k: keyof typeof f, v: string | boolean) => setF((c) => ({ ...c, [k]: v }));
  const [busy, setBusy] = useState(false);
  const m = useMsg();
  const p = EMAIL_PROVIDERS[provider];
  const oauth = !!p?.oauth;

  const applyProvider = (k: string) => {
    setProvider(k);
    const pr = EMAIL_PROVIDERS[k];
    if (!pr) return;
    setF((c) => ({ ...c, imapHost: pr.imap.host, imapPort: String(pr.imap.port), starttls: pr.imap.starttls, smtpHost: pr.smtp.host, smtpPort: String(pr.smtp.port), smtpSecurity: pr.smtp.port === 587 ? 'starttls' : 'ssl' }));
  };
  const body = (): EmailBody => {
    const b: EmailBody = {
      name: f.name.trim() || f.from.trim(),
      from_address: f.from.trim(),
      display_name: f.display.trim(),
      imap_host: f.imapHost.trim(),
      imap_port: parseInt(f.imapPort) || 993,
      imap_user: f.imapUser.trim(),
      imap_starttls: f.starttls,
      smtp_host: f.smtpHost.trim(),
      smtp_port: parseInt(f.smtpPort) || 465,
      smtp_security: f.smtpSecurity,
      smtp_user: f.smtpSame ? f.imapUser.trim() : f.smtpUser.trim(),
      is_default: f.isDefault,
    };
    if (f.imapPw) b.imap_password = f.imapPw;
    if (f.smtpSame) {
      if (f.imapPw) b.smtp_password = f.imapPw;
    } else if (f.smtpPw) b.smtp_password = f.smtpPw;
    return b;
  };
  const save = async (thenOauth = false) => {
    m.clear();
    const b = body();
    if (!b.name) return m.bad(t('At least a name or an email address.'));
    setBusy(true);
    try {
      const id = await saveEmailAccount(existing?.id ?? null, b);
      if (thenOauth) {
        window.location.href = googleOAuthUrl(id);
        return;
      }
      say(t('Mail account saved.'));
      onClose();
    } catch (e) {
      m.bad((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const test = async () => {
    m.clear();
    setBusy(true);
    try {
      const b = body();
      if (existing && !b.imap_password) b.account_id = existing.id;
      const r = await testEmailAccount(b);
      (r.ok ? m.good : m.bad)(r.message);
    } catch (e) {
      m.bad((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <h3 className="fs-set__card-title">{existing ? t('Mail account') : t('New mail account')}</h3>
      <Field label={t('Provider')} htmlFor="em-prov" help={t('A known provider fills in the IMAP and SMTP hosts; Custom lets you type your own.')}>
        <Select id="em-prov" value={provider} options={[{ value: '', label: t('Custom…') }, ...Object.entries(EMAIL_PROVIDERS).map(([k, v]) => ({ value: k, label: v.label }))]} onChange={applyProvider} />
      </Field>
      {p?.note && <p className="fs-set__help fs-intg__note">{t(p.note)}</p>}
      <div className="fs-set__grid2">
        <Field label={t('Name')} htmlFor="em-name" help={t('Optional label (Work, Personal). Blank uses the address.')}>
          <input id="em-name" className="fs-field" value={f.name} onChange={(e) => set('name', e.target.value)} />
        </Field>
        <Field label={t('Email')} htmlFor="em-from">
          <input id="em-from" className="fs-field" value={f.from} onChange={(e) => set('from', e.target.value)} placeholder={p?.emailEx ?? 'you@example.com'} />
        </Field>
        <Field label={t('Display name')} htmlFor="em-display" help={t('As it appears in From:.')}>
          <input id="em-display" className="fs-field" value={f.display} onChange={(e) => set('display', e.target.value)} />
        </Field>
      </div>
      {oauth && (
        <div className="fs-intg__oauth">
          <span className="fs-set__help">{existing?.oauth_provider === 'google' ? t('Connected through Google OAuth.') : t('Not connected: the account is saved first, then Google asks for consent.')}</span>
          <Button size="sm" variant="secondary" label={existing?.oauth_provider === 'google' ? t('Reconnect with Google') : t('Connect with Google')} loading={busy} onClick={() => void save(true)} />
        </div>
      )}
      <h4 className="fs-users__h">IMAP</h4>
      <div className="fs-set__grid2">
        <Field label={t('Host')} htmlFor="em-ih">
          <input id="em-ih" className="fs-field" value={f.imapHost} onChange={(e) => set('imapHost', e.target.value)} placeholder="imap.example.com" />
        </Field>
        <Field label={t('Port')} htmlFor="em-ip" help={t('993 for IMAPS, 143 for plain or STARTTLS.')}>
          <input id="em-ip" type="number" className="fs-field" value={f.imapPort} onChange={(e) => set('imapPort', e.target.value)} />
        </Field>
        <Field label={t('Username')} htmlFor="em-iu" help={t('Usually the full address.')}>
          <input id="em-iu" className="fs-field" value={f.imapUser} onChange={(e) => set('imapUser', e.target.value)} autoComplete="off" />
        </Field>
        {!oauth && (
          <Field label={t('Password')} htmlFor="em-ipw" help={existing ? t('Leave blank to keep the saved one.') : t('For Gmail, iCloud and Yahoo: an app password, not the account one.')}>
            <input id="em-ipw" type="password" className="fs-field" value={f.imapPw} onChange={(e) => set('imapPw', e.target.value)} autoComplete="new-password" />
          </Field>
        )}
        <Field label="STARTTLS" htmlFor="em-tls" help={t('On for port 143/587; off for 993.')}>
          <Toggle id="em-tls" checked={f.starttls} onChange={(v) => set('starttls', v)} />
        </Field>
      </div>
      <h4 className="fs-users__h">SMTP</h4>
      <div className="fs-set__grid2">
        <Field label={t('Host')} htmlFor="em-sh" help={t('Blank makes the account read-only.')}>
          <input id="em-sh" className="fs-field" value={f.smtpHost} onChange={(e) => set('smtpHost', e.target.value)} placeholder="smtp.example.com" />
        </Field>
        <Field label={t('Port')} htmlFor="em-sp">
          <input id="em-sp" type="number" className="fs-field" value={f.smtpPort} onChange={(e) => set('smtpPort', e.target.value)} />
        </Field>
        <Field label={t('Security')} htmlFor="em-ss">
          <Select id="em-ss" value={f.smtpSecurity} options={SMTP_SECURITY} onChange={(v) => set('smtpSecurity', v)} />
        </Field>
        <Field label={t('Same as IMAP')} htmlFor="em-same" help={t('Use the IMAP username and password for SMTP too.')}>
          <Toggle id="em-same" checked={f.smtpSame} onChange={(v) => set('smtpSame', v)} />
        </Field>
        {!f.smtpSame && (
          <>
            <Field label={t('Username')} htmlFor="em-su">
              <input id="em-su" className="fs-field" value={f.smtpUser} onChange={(e) => set('smtpUser', e.target.value)} autoComplete="off" />
            </Field>
            <Field label={t('Password')} htmlFor="em-spw">
              <input id="em-spw" type="password" className="fs-field" value={f.smtpPw} onChange={(e) => set('smtpPw', e.target.value)} autoComplete="new-password" />
            </Field>
          </>
        )}
      </div>
      <Field label={t('Default account')} htmlFor="em-def" help={t('Used whenever no account is chosen.')}>
        <Toggle id="em-def" checked={f.isDefault} onChange={(v) => set('isDefault', v)} />
      </Field>
      <FormFoot msg={m.msg} tone={m.tone}>
        <Button size="sm" variant="ghost" label={t('Close')} onClick={onClose} />
        <Button size="sm" variant="secondary" label={t('Test')} disabled={busy} onClick={() => void test()} />
        <Button size="sm" variant="primary" label={t('Save')} loading={busy} onClick={() => void save()} />
      </FormFoot>
    </>
  );
}

/* ── MCP ── */

export function McpPanel({ existing, onClose, onChanged, say }: { existing?: McpServer; onClose: () => void; onChanged: () => void; say: (t: string) => void }) {
  return existing ? <McpServerCard server={existing} onClose={onClose} onChanged={onChanged} say={say} /> : <McpNew onClose={onClose} onChanged={onChanged} say={say} />;
}

function McpNew({ onClose, onChanged, say }: { onClose: () => void; onChanged: () => void; say: (t: string) => void }) {
  const [name, setName] = useState('');
  const [transport, setTransport] = useState('stdio');
  const [cmd, setCmd] = useState('');
  const [args, setArgs] = useState('');
  const [env, setEnv] = useState('');
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [authId, setAuthId] = useState<string | null>(null);
  const [callback, setCallback] = useState('');
  const m = useMsg();
  const isUrl = transport === 'sse' || transport === 'http';

  const save = async () => {
    m.clear();
    if (!name.trim()) return m.bad(t('A name is required.'));
    if (isUrl && !url.trim()) return m.bad(t('The URL is required.'));
    if (!isUrl && !cmd.trim()) return m.bad(t('The command is required.'));
    if (!isUrl) {
      try {
        JSON.parse(args.trim() || '[]');
        JSON.parse(env.trim() || '{}');
      } catch {
        return m.bad(t('Args must be a JSON list and Env a JSON object.'));
      }
    }
    setBusy(true);
    try {
      const d = await addMcpServer(isUrl ? { name: name.trim(), transport, url: url.trim() } : { name: name.trim(), transport, command: cmd.trim(), args: args.trim() || '[]', env: env.trim() || '{}' });
      if (d.needs_auth && d.id) {
        setAuthId(d.id);
        m.good(t('Preparing the authorisation…'));
        void waitForAuth(d.id, d.auth_url ?? null);
      } else if (d.connected || d.status === 'connected') {
        say(t('Connected ({n} tools)', { n: d.tool_count ?? 0 }));
        onClose();
      } else {
        say(t('Saved.'));
        onClose();
      }
    } catch (e) {
      m.bad((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const waitForAuth = async (id: string, initialUrl: string | null) => {
    let opened = false;
    const open = (u: string | null | undefined) => {
      if (!opened && u) {
        opened = true;
        window.open(u, '_blank', 'noopener');
      }
    };
    open(initialUrl);
    for (let i = 0; i < 90; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      try {
        const s = (await listMcpServers()).find((x) => x.id === id);
        if (!s) return;
        open(s.auth_url);
        if (s.status === 'connected') {
          say(t('Connected ({n} tools)', { n: s.tool_count ?? 0 }));
          onClose();
          return;
        }
        if (s.status === 'error') {
          m.bad(s.error ?? t('Failed'));
          return;
        }
      } catch {
        /* keep polling */
      }
    }
  };
  return (
    <>
      <h3 className="fs-set__card-title">{t('New MCP server')}</h3>
      <div className="fs-set__grid2">
        <Field label={t('Name')} htmlFor="mcp-name">
          <input id="mcp-name" className="fs-field" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label={t('Transport')} htmlFor="mcp-tr">
          <Select id="mcp-tr" value={transport} options={[{ value: 'stdio', label: 'stdio' }, { value: 'sse', label: 'SSE' }, { value: 'http', label: 'Streamable HTTP' }]} onChange={setTransport} />
        </Field>
      </div>
      {isUrl ? (
        <Field label="URL" htmlFor="mcp-url">
          <input id="mcp-url" className="fs-field" value={url} onChange={(e) => setUrl(e.target.value)} placeholder={transport === 'http' ? 'https://mcp.example.com/mcp' : 'http://localhost:3001/sse'} />
        </Field>
      ) : (
        <>
          <Field label={t('Command')} htmlFor="mcp-cmd">
            <input id="mcp-cmd" className="fs-field" value={cmd} onChange={(e) => setCmd(e.target.value)} placeholder="npx" />
          </Field>
          <div className="fs-set__grid2">
            <Field label={t('Args (JSON list)')} htmlFor="mcp-args">
              <input id="mcp-args" className="fs-field" value={args} onChange={(e) => setArgs(e.target.value)} placeholder='["-y", "@modelcontextprotocol/server-filesystem"]' />
            </Field>
            <Field label={t('Env (JSON object)')} htmlFor="mcp-env">
              <input id="mcp-env" className="fs-field" value={env} onChange={(e) => setEnv(e.target.value)} placeholder='{"KEY": "value"}' />
            </Field>
          </div>
        </>
      )}
      {authId && (
        <Field label={t('Authorise in the tab that opened. If the redirect fails (remote access), paste the resulting URL here:')} htmlFor="mcp-cb">
          <div className="fs-set__inline">
            <input id="mcp-cb" className="fs-field" value={callback} onChange={(e) => setCallback(e.target.value)} placeholder="http://localhost:7000/api/mcp/oauth/callback?code=…" />
            <Button size="sm" variant="secondary" label={t('Submit')} onClick={() => void mcpOauthExchange(authId, callback.trim()).then(() => say(t('Authorised.'))).catch((e: Error) => m.bad(e.message))} />
          </div>
        </Field>
      )}
      <FormFoot msg={m.msg} tone={m.tone}>
        <Button size="sm" variant="ghost" label={t('Close')} onClick={onClose} />
        <Button size="sm" variant="primary" label={t('Add the server')} loading={busy} onClick={() => void save()} />
      </FormFoot>
    </>
  );
}

function McpServerCard({ server, onClose, onChanged, say }: { server: McpServer; onClose: () => void; onChanged: () => void; say: (t: string) => void }) {
  const [srv, setSrv] = useState(server);
  const [tools, setTools] = useState<McpTool[] | null>(null);
  const [busy, setBusy] = useState(false);
  const m = useMsg();
  const refresh = async () => {
    const s = (await listMcpServers()).find((x) => x.id === server.id);
    if (s) setSrv(s);
    onChanged();
  };
  useEffect(() => {
    if (srv.status === 'connected' && srv.tool_count > 0) listMcpTools(srv.id).then(setTools).catch(() => setTools([]));
    else setTools([]);
  }, [srv.id, srv.status, srv.tool_count]);
  const status = srv.needs_oauth ? t('Needs authorisation') : srv.status === 'connected' ? t('Connected ({n} tools)', { n: `${srv.enabled_tool_count}/${srv.tool_count}` }) : srv.status === 'error' ? `${t('Error')}: ${srv.error ?? t('unknown')}` : t('Disconnected');
  const setDisabled = async (disabled: string[]) => {
    try {
      await setMcpDisabledTools(srv.id, disabled);
      setTools((ts) => (ts ?? []).map((x) => ({ ...x, is_disabled: disabled.includes(x.name) })));
      await refresh();
    } catch (e) {
      m.bad((e as Error).message);
    }
  };
  const disabledNow = (tools ?? []).filter((x) => x.is_disabled).map((x) => x.name);
  return (
    <>
      <h3 className="fs-set__card-title fs-tools__cat">
        <span>{srv.name}</span>
        <span className="fs-set__help">{srv.transport} · {srv.env_mode === 'inherited' ? t('inherits the environment') : t('minimal environment')}</span>
      </h3>
      <p className="fs-set__help" data-tone={srv.status === 'connected' ? 'ok' : srv.status === 'error' ? 'bad' : undefined}>
        {status}
      </p>
      {srv.transport === 'stdio' ? <p className="fs-set__help"><code className="fs-tools__id">{[srv.command, ...(srv.args ?? [])].filter(Boolean).join(' ')}</code></p> : <p className="fs-set__help"><code className="fs-tools__id">{srv.url}</code></p>}
      <div className="fs-set__row-end" style={{ justifyContent: 'flex-start' }}>
        {srv.needs_oauth && <Button size="sm" variant="secondary" label={t('Authorise')} onClick={() => window.open(mcpAuthorizeUrl(srv.id), '_blank', 'noopener')} />}
        <Button size="sm" variant="secondary" label={t('Reconnect')} loading={busy} onClick={() => { setBusy(true); reconnectMcpServer(srv.id).then((d) => { (d.connected ? m.good : m.bad)(d.connected ? t('Connected ({n} tools)', { n: d.tool_count ?? 0 }) : `${t('Failed')}: ${d.error ?? t('unknown')}`); return refresh(); }).catch((e: Error) => m.bad(e.message)).finally(() => setBusy(false)); }} />
        <Button size="sm" variant="ghost" label={srv.is_enabled ? t('Disable') : t('Enable')} onClick={() => void toggleMcpServer(srv.id, !srv.is_enabled).then(refresh).catch((e: Error) => m.bad(e.message))} />
      </div>
      {tools === null ? (
        <Skeleton label={t('Loading')} count={2} height="32px" />
      ) : tools.length > 0 ? (
        <>
          <h4 className="fs-users__h">
            {t('Tools')} <span className="fs-set__help">{tools.length - disabledNow.length}/{tools.length} {t('enabled')}</span>
            <span className="fs-users__h-actions">
              <button type="button" className="fs-link" onClick={() => void setDisabled([])}>{t('All')}</button>
              <button type="button" className="fs-link" onClick={() => void setDisabled(tools.map((x) => x.name))}>{t('None')}</button>
            </span>
          </h4>
          <ul className="fs-tools">
            {tools.map((tool) => (
              <li key={tool.name} className="fs-tools__row">
                <span className="fs-tools__text">
                  <strong>{tool.name}</strong>
                  {tool.description && <span className="fs-set__help">{tool.description}</span>}
                </span>
                <Toggle id={`mcpt-${srv.id}-${tool.name}`} checked={!tool.is_disabled} onChange={(v) => void setDisabled(v ? disabledNow.filter((n) => n !== tool.name) : [...disabledNow, tool.name])} />
              </li>
            ))}
          </ul>
        </>
      ) : null}
      <FormFoot msg={m.msg} tone={m.tone}>
        <Button size="sm" variant="ghost" label={t('Close')} onClick={onClose} />
      </FormFoot>
    </>
  );
}

/* ── contacts (import + CardDAV) ── */

export function ContactsPanel({ onClose, onChanged, say }: { onClose: () => void; onChanged: () => void; say: (t: string) => void }) {
  const [url, setUrl] = useState('');
  const [user, setUser] = useState('');
  const [pw, setPw] = useState('');
  const [hasPw, setHasPw] = useState(false);
  const [list, setList] = useState<Contact[] | null>(null);
  const [q, setQ] = useState('');
  const [add, setAdd] = useState({ name: '', email: '', phone: '', address: '' });
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState({ name: '', emails: '', phones: '', address: '' });
  const file = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const m = useMsg();

  const reload = () => listContacts().then((d) => { setList(d.contacts); onChanged(); }).catch(() => setList([]));
  useEffect(() => {
    contactsConfig()
      .then((c) => {
        setUrl(c.url ?? c.carddav_url ?? '');
        setUser(c.username ?? c.carddav_username ?? '');
        setHasPw(!!c.password);
      })
      .catch(() => {});
    void reload();
  }, []);

  const shown = (list ?? []).filter((c) => {
    if (!q) return true;
    const s = q.toLowerCase();
    return [c.name, ...(c.emails ?? []), ...(c.phones ?? []), c.address].filter(Boolean).some((x) => String(x).toLowerCase().includes(s));
  });

  const doImport = async (files: FileList) => {
    setBusy(true);
    m.clear();
    try {
      const vcf: string[] = [];
      const csv: string[] = [];
      for (const f of Array.from(files)) {
        const text = await f.text();
        if (f.name.toLowerCase().endsWith('.csv') || !text.toUpperCase().includes('BEGIN:VCARD')) csv.push(text);
        else vcf.push(text);
      }
      if (!vcf.length && !csv.length) throw new Error(t('No contact data found.'));
      let imported = 0;
      let total = 0;
      let failed = 0;
      for (const b of [vcf.length ? { vcf: vcf.join('\n') } : null, csv.length ? { csv: csv.join('\n') } : null]) {
        if (!b) continue;
        const r = await importContacts(b);
        imported += r.imported;
        total += r.total;
        failed += r.failed;
      }
      m.good(t('Imported {a}/{b}', { a: imported, b: total }) + (failed ? ` (${tn(failed, '{n} failed', '{n} failed')})` : ''));
      await reload();
    } catch (e) {
      m.bad((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <h3 className="fs-set__card-title">{t('Contacts')}</h3>
      <h4 className="fs-users__h">{t('CardDAV sync')}</h4>
      <div className="fs-set__grid2">
        <Field label="URL" htmlFor="cc-url">
          <input id="cc-url" className="fs-field" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="http://localhost:5232/user/contacts/" />
        </Field>
        <Field label={t('Username')} htmlFor="cc-user">
          <input id="cc-user" className="fs-field" value={user} onChange={(e) => setUser(e.target.value)} autoComplete="off" />
        </Field>
        <Field label={t('Password')} htmlFor="cc-pw" help={hasPw ? t('Leave blank to keep the saved one.') : undefined}>
          <input id="cc-pw" type="password" className="fs-field" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="new-password" />
        </Field>
      </div>
      <div className="fs-set__row-end">
        <Button size="sm" variant="primary" label={t('Save the sync')} loading={busy} onClick={() => { setBusy(true); saveCardDav({ carddav_url: url.trim(), carddav_username: user.trim(), ...(pw ? { carddav_password: pw } : {}) }).then(() => { say(t('CardDAV saved.')); setPw(''); setHasPw(true); onChanged(); }).catch((e: Error) => m.bad(e.message)).finally(() => setBusy(false)); }} />
      </div>

      <h4 className="fs-users__h">
        {t('Address book')} <span className="fs-set__help">{tn((list ?? []).length, '{n} contact', '{n} contacts')}</span>
        <span className="fs-users__h-actions">
          <Button size="sm" variant="ghost" icon={Upload} label={t('Import')} onClick={() => file.current?.click()} />
          <Button size="sm" variant="ghost" icon={Download} label=".vcf" onClick={() => void exportContacts('vcf').catch((e: Error) => m.bad(e.message))} />
          <Button size="sm" variant="ghost" icon={Download} label=".csv" onClick={() => void exportContacts('csv').catch((e: Error) => m.bad(e.message))} />
        </span>
      </h4>
      <input ref={file} type="file" accept=".vcf,.csv,text/vcard,text/csv" multiple hidden onChange={(e) => { if (e.target.files?.length) void doImport(e.target.files); e.target.value = ''; }} />
      <div className="fs-set__grid2">
        <input className="fs-field" placeholder={t('Name')} value={add.name} onChange={(e) => setAdd({ ...add, name: e.target.value })} aria-label={t('Name')} />
        <input className="fs-field" placeholder="email@example.com" value={add.email} onChange={(e) => setAdd({ ...add, email: e.target.value })} aria-label={t('Email')} />
        <input className="fs-field" placeholder={t('Phone (optional)')} value={add.phone} onChange={(e) => setAdd({ ...add, phone: e.target.value })} aria-label={t('Phone')} />
        <input className="fs-field" placeholder={t('Address (optional)')} value={add.address} onChange={(e) => setAdd({ ...add, address: e.target.value })} aria-label={t('Address')} />
      </div>
      <div className="fs-set__row-end">
        <Button size="sm" variant="secondary" icon={Plus} label={t('Add the contact')} disabled={!add.name.trim() && !add.email.trim()} onClick={() => void addContact({ name: add.name.trim(), email: add.email.trim(), phone: add.phone.trim(), address: add.address.trim() }).then(() => { setAdd({ name: '', email: '', phone: '', address: '' }); return reload(); }).catch((e: Error) => m.bad(e.message))} />
      </div>
      <input className="fs-field" placeholder={t('Search contacts (name, email, phone, address)')} value={q} onChange={(e) => setQ(e.target.value)} aria-label={t('Search contacts')} />
      {list === null ? (
        <Skeleton label={t('Loading')} count={2} height="36px" />
      ) : shown.length === 0 ? (
        <p className="fs-set__help">{t('No contacts.')}</p>
      ) : (
        <ul className="fs-contacts">
          {shown.map((c) => (
            <li key={c.uid} className="fs-contacts__row">
              {editing === c.uid ? (
                <div className="fs-contacts__edit">
                  <input className="fs-field" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder={t('Name')} aria-label={t('Name')} />
                  <input className="fs-field" value={draft.emails} onChange={(e) => setDraft({ ...draft, emails: e.target.value })} placeholder="email1, email2" aria-label={t('Emails')} />
                  <input className="fs-field" value={draft.phones} onChange={(e) => setDraft({ ...draft, phones: e.target.value })} placeholder="phone1, phone2" aria-label={t('Phones')} />
                  <input className="fs-field" value={draft.address} onChange={(e) => setDraft({ ...draft, address: e.target.value })} placeholder={t('Address')} aria-label={t('Address')} />
                  <span className="fs-set__row-end">
                    <Button size="sm" variant="ghost" label={t('Cancel')} onClick={() => setEditing(null)} />
                    <Button size="sm" variant="primary" label={t('Save')} onClick={() => void updateContact(c.uid, { name: draft.name.trim(), emails: draft.emails.split(',').map((s) => s.trim()).filter(Boolean), phones: draft.phones.split(',').map((s) => s.trim()).filter(Boolean), address: draft.address.trim() }).then(() => { setEditing(null); return reload(); }).catch((e: Error) => m.bad(e.message))} />
                  </span>
                </div>
              ) : (
                <>
                  <span className="fs-tools__text">
                    <strong>{c.name || t('(no name)')}</strong>
                    <span className="fs-set__help">{[...(c.emails ?? []), ...(c.phones ?? []), c.address].filter(Boolean).join(' · ')}</span>
                  </span>
                  <span className="fs-users__actions">
                    <IconButton icon={Pencil} label={t('Edit')} size="sm" onClick={() => { setEditing(c.uid); setDraft({ name: c.name ?? '', emails: (c.emails ?? []).join(', '), phones: (c.phones ?? []).join(', '), address: c.address ?? '' }); }} />
                    <IconButton icon={Trash2} label={t('Delete')} size="sm" onClick={() => { if (!window.confirm(t('Delete this contact?'))) return; void deleteContact(c.uid).then(reload).catch((e: Error) => m.bad(e.message)); }} />
                  </span>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      <FormFoot msg={m.msg} tone={m.tone}>
        <Button size="sm" variant="ghost" label={t('Close')} onClick={onClose} />
      </FormFoot>
    </>
  );
}
