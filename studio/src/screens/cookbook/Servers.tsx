import { Check, Key, Plus, Server as ServerIcon, Trash2, Wrench } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button, Dialog, IconButton } from '../../components';
import { generateSshKey, isLocal, serverKey, setHfToken, setupServer, sshKey, testSsh, updateState, useCookbookState, type Server } from '../../adapters/cookbook';
import { t, tn } from '../../i18n';
import { SERVER_COLORS as COLORS } from '../../lib/cookbook/colors';
import { CopyButton, Field, Switch } from './parts';

/**
 * Servers: this machine plus every SSH box models can be pulled to and
 * launched on — its Python environment, its model folders (and which one
 * downloads go to), a colour; test the connection, install the basics
 * (tmux, build tools) with one call, and the SSH key the app uses. The HF
 * token and GPU pin live here too because they apply everywhere.
 */

const DEFAULT_DIR = '~/.cache/huggingface/hub';


export function Servers({ say }: { say: (m: string) => void }) {
  const { env, tasks } = useCookbookState();
  const [drafts, setDrafts] = useState<Record<string, Server>>({});
  const [adding, setAdding] = useState<Server | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<Record<string, string>>({});
  const [key, setKey] = useState<{ public_key: string; exists: boolean } | null>(null);
  const [token, setToken] = useState('');
  const [remove, setRemove] = useState<Server | null>(null);
  const [newDir, setNewDir] = useState<Record<string, string>>({});

  useEffect(() => {
    sshKey().then(setKey).catch(() => setKey({ public_key: '', exists: false }));
  }, []);

  const draftOf = (s: Server): Server => drafts[serverKey(s)] ?? s;
  const edit = (s: Server, patch: Partial<Server>) => setDrafts((d) => ({ ...d, [serverKey(s)]: { ...draftOf(s), ...patch } }));
  const dirty = (s: Server) => Boolean(drafts[serverKey(s)]);
  const save = (s: Server) => {
    const next = draftOf(s);
    updateState((st) => ({ ...st, env: { ...st.env, servers: st.env.servers.map((x) => (serverKey(x) === serverKey(s) ? next : x)) } }));
    setDrafts((d) => {
      const c = { ...d };
      delete c[serverKey(s)];
      return c;
    });
    say(t('Saved'));
  };
  const addServer = () => {
    if (!adding?.host.trim()) return;
    const s: Server = { ...adding, host: adding.host.trim(), modelDirs: adding.modelDirs.length ? adding.modelDirs : [DEFAULT_DIR] };
    updateState((st) => ({ ...st, env: { ...st.env, servers: [...st.env.servers, s] } }));
    setAdding(null);
    say(t('Server added'));
  };
  const deleteServer = (s: Server) => {
    updateState((st) => ({ ...st, env: { ...st.env, servers: st.env.servers.filter((x) => serverKey(x) !== serverKey(s)), ...(st.env.remoteHost === s.host ? { remoteHost: '', remoteServerKey: '' } : {}) } }));
    setRemove(null);
    say(t('Server removed'));
  };
  const test = async (s: Server) => {
    setBusy(`test-${serverKey(s)}`);
    try {
      const r = await testSsh(s.host, s.port);
      setResult((x) => ({ ...x, [serverKey(s)]: r.exit_code === 0 ? `✓ ${r.stdout.trim().split('\n')[0] || t('connected')}` : `✗ ${(r.stderr || r.stdout).trim().split('\n').pop() || t('failed')}` }));
    } catch (e) {
      setResult((x) => ({ ...x, [serverKey(s)]: `✗ ${(e as Error).message}` }));
    } finally {
      setBusy(null);
    }
  };
  const setup = async (s: Server) => {
    setBusy(`setup-${serverKey(s)}`);
    try {
      const r = await setupServer(s.host, s.port);
      const platform = String(r.platform || '');
      if (platform) edit(s, { platform });
      setResult((x) => ({ ...x, [serverKey(s)]: r.ok === false ? `✗ ${String(r.error || '')}` : `✓ ${String(r.message || r.output || t('set up'))}`.slice(0, 300) }));
      if (platform) save({ ...s, platform });
    } catch (e) {
      setResult((x) => ({ ...x, [serverKey(s)]: `✗ ${(e as Error).message}` }));
    } finally {
      setBusy(null);
    }
  };
  const genKey = async () => {
    setBusy('key');
    try {
      const r = await generateSshKey();
      setKey({ public_key: r.public_key, exists: true });
      say(t('Key generated'));
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };
  const copyCommand = (s: Server) => {
    const port = s.port && s.port !== '22' ? ` -p ${s.port}` : '';
    return key?.public_key ? `echo ${JSON.stringify(key.public_key.trim())} | ssh${port} ${s.host} 'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys'` : '';
  };

  const liveOn = (s: Server) => tasks.filter((x) => (x.remoteHost || '') === (isLocal(s) ? '' : s.host) && (x.status === 'running' || x.status === 'ready')).length;

  const card = (s: Server, isNew = false) => {
    const d = isNew ? (adding as Server) : draftOf(s);
    const set = (patch: Partial<Server>) => (isNew ? setAdding((a) => ({ ...(a as Server), ...patch })) : edit(s, patch));
    const local = isLocal(s) && !isNew;
    const k = serverKey(s);
    return (
      <li key={isNew ? 'new' : k} className="fs-ck__server" data-local={local || undefined} style={d.color ? ({ '--fs-ck-server': d.color } as never) : undefined}>
        <div className="fs-ck__server-head">
          <span className="fs-ck__server-dot" aria-hidden="true" />
          <ServerIcon size={14} aria-hidden="true" />
          <strong>{local ? t('This machine') : d.name || d.host || t('New server')}</strong>
          {local && <span className="fs-muted">{env.hostPlatform}</span>}
          {!isNew && liveOn(s) > 0 && <span className="fs-ck__tag" data-kind="serving">{tn(liveOn(s), '{n} live', '{n} live#')}</span>}
          <span className="fs-spacer" />
          {!local && !isNew && <Button size="sm" variant="ghost" label={t('Test SSH')} loading={busy === `test-${k}`} onClick={() => void test(s)} />}
          {!local && !isNew && <Button size="sm" variant="ghost" icon={Wrench} label={t('Set up')} title={t('Install tmux and the build basics over SSH; detects the platform')} loading={busy === `setup-${k}`} onClick={() => void setup(s)} />}
          {!local && !isNew && <IconButton icon={Trash2} size="sm" label={t('Remove server')} onClick={() => setRemove(s)} />}
        </div>
        <div className="fs-ck__grid">
          {!local && (
            <>
              <Field label={t('Name')}>
                <input className="fs-field" value={d.name ?? ''} onChange={(e) => set({ name: e.target.value })} placeholder={t('gpu-box')} />
              </Field>
              <Field label={t('SSH host')}>
                <input className="fs-field" value={d.host} onChange={(e) => set({ host: e.target.value })} placeholder="user@host" data-testid="server-host" />
              </Field>
              <Field label={t('SSH port')}>
                <input className="fs-field" value={d.port ?? ''} onChange={(e) => set({ port: e.target.value })} placeholder="22" inputMode="numeric" />
              </Field>
              <Field label={t('Platform')}>
                <select className="fs-field" value={d.platform} onChange={(e) => set({ platform: e.target.value })}>
                  <option value="">{t('detect on set up')}</option>
                  {['linux', 'darwin', 'windows', 'termux'].map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
              </Field>
            </>
          )}
          <Field label={t('Python environment')}>
            <select className="fs-field" value={d.env || 'none'} onChange={(e) => set({ env: e.target.value })} data-field="env_type">
              <option value="none">{t('System Python')}</option>
              <option value="venv">venv</option>
              <option value="conda">conda</option>
            </select>
          </Field>
          <Field label={t('Environment path')} wide>
            <input className="fs-field" value={d.envPath} onChange={(e) => set({ envPath: e.target.value })} placeholder={d.env === 'conda' ? 'myenv' : d.platform === 'windows' || (local && env.hostPlatform === 'windows') ? 'C:\\venvs\\vllm' : '~/venv'} />
          </Field>
          <Field label={t('Colour')}>
            <select className="fs-field" value={d.color ?? ''} onChange={(e) => set({ color: e.target.value })}>
              {COLORS.map(([v, l]) => (
                <option key={v} value={v}>
                  {l}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <div className="fs-ck__dirs">
          <span className="fs-ck__label">{t('Model folders')} · {t('the checked one receives downloads')}</span>
          <ul>
            {d.modelDirs.map((dir) => (
              <li key={dir}>
                <label className="fs-switch" title={t('Download here')}>
                  <input type="radio" name={`dl-${k}`} checked={(d.downloadDir || '') === (dir === DEFAULT_DIR ? '' : dir)} onChange={() => set({ downloadDir: dir === DEFAULT_DIR ? undefined : dir })} />
                  <code>{dir}</code>
                </label>
                {dir !== DEFAULT_DIR && <IconButton icon={Trash2} size="sm" label={t('Remove folder')} onClick={() => set({ modelDirs: d.modelDirs.filter((x) => x !== dir), downloadDir: d.downloadDir === dir ? undefined : d.downloadDir })} />}
              </li>
            ))}
          </ul>
          <form
            className="fs-inline"
            onSubmit={(e) => {
              e.preventDefault();
              const v = (newDir[k] || '').trim();
              if (!v || d.modelDirs.includes(v)) return;
              set({ modelDirs: [...d.modelDirs, v] });
              setNewDir((x) => ({ ...x, [k]: '' }));
            }}
          >
            <input className="fs-field" value={newDir[k] || ''} onChange={(e) => setNewDir((x) => ({ ...x, [k]: e.target.value }))} placeholder={t('/mnt/models')} aria-label={t('Add a model folder')} />
            <Button type="submit" size="sm" variant="ghost" icon={Plus} label={t('Add folder')} />
          </form>
        </div>
        {!isNew && result[k] && <p className="fs-ck__result">{result[k]}</p>}
        {!local && !isNew && key?.public_key && (
          <details className="fs-ck__keybox">
            <summary>{t('Authorise the app’s SSH key on this server')}</summary>
            <p className="fs-muted">{t('Run this once in a terminal that can already reach the server:')}</p>
            <pre className="fs-ck__recipe-cmds">{copyCommand(s)}</pre>
            <CopyButton text={copyCommand(s)} say={say} />
          </details>
        )}
        <div className="fs-inline">
          {isNew ? (
            <>
              <Button size="sm" variant="primary" icon={Check} label={t('Add server')} disabled={!d.host.trim()} onClick={addServer} testId="server-add" />
              <Button size="sm" variant="ghost" label={t('Cancel')} onClick={() => setAdding(null)} />
            </>
          ) : (
            dirty(s) && <Button size="sm" variant="primary" icon={Check} label={t('Save')} onClick={() => save(s)} testId="server-save" />
          )}
        </div>
      </li>
    );
  };

  return (
    <div className="fs-ck__servers" data-testid="cookbook-servers">
      <ul className="fs-ck__server-list">
        {env.servers.map((s) => card(s))}
        {adding && card(adding, true)}
      </ul>
      {!adding && <Button variant="secondary" icon={Plus} label={t('Add an SSH server')} onClick={() => setAdding({ host: '', env: 'none', envPath: '', platform: '', modelDirs: [DEFAULT_DIR] })} testId="server-new" />}

      <section className="fs-ck__panel">
        <h3 className="fs-ck__h">
          <Key size={14} aria-hidden="true" /> {t('Everywhere')}
        </h3>
        <div className="fs-ck__grid">
          <Field label={t('Hugging Face token')} wide hint={t('Sent with downloads and serves; needed for gated repos')}>
            <div className="fs-inline">
              <input className="fs-field" type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder={env.hfTokenConfigured ? t('Set ({masked}) — type a new one to replace it', { masked: env.hfTokenMasked || '••••' }) : 'hf_…'} autoComplete="off" data-field="hf_token" />
              <Button size="sm" variant="secondary" label={t('Save token')} disabled={!token.trim()} onClick={() => { setHfToken(token.trim()); setToken(''); say(t('Token saved')); }} />
              {env.hfTokenConfigured && <Button size="sm" variant="ghost" label={t('Clear')} onClick={() => { setHfToken(''); say(t('Token cleared')); }} />}
            </div>
          </Field>
          <Field label={t('Pin GPUs by default')} hint={t('CUDA_VISIBLE_DEVICES / HIP_VISIBLE_DEVICES for every launch; empty = all')}>
            <input className="fs-field" value={env.gpus} onChange={(e) => updateState((st) => ({ ...st, env: { ...st.env, gpus: e.target.value } }))} placeholder={t('all')} data-field="gpus" />
          </Field>
          <div className="fs-ck__field" data-wide>
            <span className="fs-ck__label">{t('SSH key')}</span>
            {key === null && <span className="fs-muted">{t('Reading…')}</span>}
            {key && key.exists && key.public_key && (
              <div className="fs-inline">
                <code className="fs-ck__pubkey">{key.public_key.slice(0, 60)}…</code>
                <CopyButton text={key.public_key} label={t('Copy public key')} say={say} />
              </div>
            )}
            {key && !key.exists && (
              <div className="fs-inline">
                <span className="fs-muted">{t('No key yet; the app needs one to reach servers without a password.')}</span>
                <Button size="sm" variant="secondary" icon={Key} label={t('Generate key')} loading={busy === 'key'} onClick={() => void genKey()} />
              </div>
            )}
          </div>
          <Switch label={t('Use the first remote as default target')} checked={Boolean(env.defaultServer)} onChange={(v) => updateState((st) => ({ ...st, env: { ...st.env, defaultServer: v ? (st.env.servers.find((x) => !isLocal(x))?.host ?? '') : '' } }))} />
        </div>
      </section>

      {remove && (
        <Dialog
          open
          onOpenChange={(o) => {
            if (!o) setRemove(null);
          }}
          title={t('Remove {name}?', { name: remove.name || remove.host })}
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setRemove(null)} />
              <Button variant="danger-solid" size="sm" label={t('Remove')} onClick={() => deleteServer(remove)} />
            </>
          }
        >
          <p className="fs-prose">{t('Sessions already running there keep running; the app just stops listing the server.')}</p>
        </Dialog>
      )}
    </div>
  );
}
