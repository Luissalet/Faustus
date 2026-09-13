import { Pencil, Plus, Trash2, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button, IconButton, Skeleton } from '../../components';
import { Field, Select } from '../settings/fields';
import { FormFoot, useMsg } from '../settings/IntegrationForms';
import {
  deleteLaunchProfile,
  listLaunchProfiles,
  saveLaunchProfile,
  type LaunchProfile,
  type LaunchProfileInput,
} from '../../adapters/connectors';
import { t } from '../../i18n';

/**
 * Launch profile editor (CONTRATO_CONECTORES F1.4/F3): executable, argv as
 * an editable LIST (never a shell string the user could slip metacharacters
 * into), cwd and readiness. User-only by construction — this panel is
 * reached from Settings/Connectors, never from anything the agent calls;
 * the model has no tool that creates or edits a profile (principle 4).
 */

const KINDS: { value: LaunchProfile['kind']; label: string }[] = [
  { value: 'process', label: 'Run a program (with readiness check)' },
  { value: 'open_exe', label: 'Launch a program (no readiness check)' },
  { value: 'open_url', label: 'Open a URL in the browser' },
];

export function LaunchProfilesPanel({ onClose }: { onClose: () => void }) {
  const [profiles, setProfiles] = useState<LaunchProfile[] | null>(null);
  const [editing, setEditing] = useState<LaunchProfile | 'new' | null>(null);
  const m = useMsg();

  const reload = () => listLaunchProfiles().then(setProfiles).catch(() => setProfiles([]));
  useEffect(() => {
    void reload();
  }, []);

  // Two-step inline confirmation (as Settings › Behaviour modes does), never
  // window.confirm: a native dialog blocks the whole tab.
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const remove = async (id: string) => {
    setConfirmDelete(null);
    try {
      await deleteLaunchProfile(id);
      await reload();
    } catch (e) {
      m.bad((e as Error).message);
    }
  };

  return (
    <div className="fs-set__card fs-conn__form" data-testid="launch-profiles-panel">
      <h3 className="fs-set__card-title">{t('Launch profiles')}</h3>
      <p className="fs-set__help">{t('Only you can create or edit these. The agent cannot start programs, and cannot create or edit a profile either.')}</p>
      {profiles === null ? (
        <Skeleton label={t('Loading')} count={2} height="36px" />
      ) : profiles.length === 0 ? (
        <p className="fs-set__help">{t('No launch profiles yet.')}</p>
      ) : (
        <ul className="fs-tools">
          {profiles.map((p) => (
            <li key={p.id} className="fs-tools__row">
              <span className="fs-tools__text">
                <strong>{p.name}</strong>
                <span className="fs-set__help">
                  {p.kind === 'open_url' ? t('Opens a URL') : <code className="fs-tools__id">{[p.executable, ...p.argv].filter(Boolean).join(' ')}</code>}
                </span>
              </span>
              <span className="fs-users__actions">
                {confirmDelete === p.id ? (
                  <span className="fs-modes__confirm">
                    <span className="fs-set__help" data-tone="bad">{t('Delete this launch profile?')}</span>
                    <Button variant="danger-solid" size="sm" label={t('Delete')} onClick={() => void remove(p.id)} />
                    <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirmDelete(null)} />
                  </span>
                ) : (
                  <>
                    <IconButton icon={Pencil} label={t('Edit')} size="sm" onClick={() => setEditing(p)} />
                    <IconButton icon={Trash2} label={t('Delete')} size="sm" onClick={() => setConfirmDelete(p.id)} />
                  </>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
      {editing ? (
        <LaunchProfileForm
          existing={editing === 'new' ? undefined : editing}
          onCancel={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void reload();
          }}
        />
      ) : (
        <Button size="sm" variant="secondary" icon={Plus} label={t('New launch profile')} onClick={() => setEditing('new')} testId="launch-profile-new" />
      )}
      <FormFoot msg={m.msg} tone={m.tone}>
        <Button size="sm" variant="ghost" icon={X} label={t('Close')} onClick={onClose} />
      </FormFoot>
    </div>
  );
}

function LaunchProfileForm({ existing, onCancel, onSaved }: { existing?: LaunchProfile; onCancel: () => void; onSaved: (p: LaunchProfile) => void }) {
  const [name, setName] = useState(existing?.name ?? '');
  const [kind, setKind] = useState<LaunchProfile['kind']>(existing?.kind ?? 'process');
  const [executable, setExecutable] = useState(existing?.executable ?? '');
  const [argv, setArgv] = useState<string[]>(existing?.argv ?? []);
  const [cwd, setCwd] = useState(existing?.cwd ?? '');
  const [envText, setEnvText] = useState(JSON.stringify(existing?.env ?? {}, null, 0));
  const [readinessUrl, setReadinessUrl] = useState(existing?.readiness?.url ?? '');
  const [timeoutS, setTimeoutS] = useState(String(existing?.readiness?.timeout_s ?? 20));
  const [busy, setBusy] = useState(false);
  const m = useMsg();

  const setArg = (i: number, v: string) => setArgv((a) => a.map((x, idx) => (idx === i ? v : x)));
  const removeArg = (i: number) => setArgv((a) => a.filter((_, idx) => idx !== i));

  const save = async () => {
    m.clear();
    if (!name.trim()) return m.bad(t('A name is required.'));
    if (kind !== 'open_url' && !executable.trim()) return m.bad(t('An absolute path to the executable is required.'));
    if (kind === 'process' && !cwd.trim()) return m.bad(t('A working folder (cwd) is required for a program with a readiness check.'));
    let env: Record<string, string> = {};
    try {
      env = envText.trim() ? (JSON.parse(envText) as Record<string, string>) : {};
    } catch {
      return m.bad(t('Environment must be a JSON object.'));
    }
    setBusy(true);
    try {
      const body: LaunchProfileInput = {
        name: name.trim(),
        kind,
        executable: executable.trim(),
        argv: argv.map((a) => a.trim()).filter(Boolean),
        cwd: cwd.trim(),
        env,
        readiness: kind === 'process' && readinessUrl.trim() ? { url: readinessUrl.trim(), timeout_s: Number(timeoutS) || 20 } : null,
      };
      const saved = await saveLaunchProfile(existing?.id ?? null, body);
      onSaved(saved);
    } catch (e) {
      m.bad((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fs-conn__profile-form">
      <h4 className="fs-users__h">{existing ? t('Edit launch profile') : t('New launch profile')}</h4>
      <div className="fs-set__grid2">
        <Field label={t('Name')} htmlFor="lp-name">
          <input id="lp-name" className="fs-field" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label={t('Kind')} htmlFor="lp-kind">
          <Select id="lp-kind" value={kind} options={KINDS.map((k) => ({ value: k.value, label: t(k.label) }))} onChange={(v) => setKind(v as LaunchProfile['kind'])} />
        </Field>
      </div>
      <Field label={kind === 'open_url' ? t('URL') : t('Executable (absolute path)')} htmlFor="lp-exe">
        <input id="lp-exe" className="fs-field" value={executable} onChange={(e) => setExecutable(e.target.value)} placeholder={kind === 'open_url' ? 'http://127.0.0.1:5178' : '/usr/bin/node'} />
      </Field>
      {kind !== 'open_url' && (
        <>
          <Field label={t('Arguments (one per box; passed literally, never through a shell)')} htmlFor="lp-argv">
            <div className="fs-conn__argv">
              {argv.map((a, i) => (
                <span key={i} className="fs-conn__argv-row">
                  <input className="fs-field" value={a} onChange={(e) => setArg(i, e.target.value)} aria-label={t('Argument {n}', { n: i + 1 })} />
                  <IconButton icon={Trash2} label={t('Remove argument {n}', { n: i + 1 })} size="sm" onClick={() => removeArg(i)} />
                </span>
              ))}
              <Button size="sm" variant="ghost" icon={Plus} label={t('Add argument')} onClick={() => setArgv((a) => [...a, ''])} />
            </div>
          </Field>
          <div className="fs-set__grid2">
            <Field label={t('Working folder (cwd)')} htmlFor="lp-cwd">
              <input id="lp-cwd" className="fs-field" value={cwd} onChange={(e) => setCwd(e.target.value)} />
            </Field>
            <Field label={t('Environment (JSON object)')} htmlFor="lp-env">
              <input id="lp-env" className="fs-field" value={envText} onChange={(e) => setEnvText(e.target.value)} placeholder="{}" />
            </Field>
          </div>
        </>
      )}
      {kind === 'process' && (
        <div className="fs-set__grid2">
          <Field label={t('Readiness URL (optional)')} htmlFor="lp-ready" help={t('Polled every 0.5s until it answers, or the timeout runs out — nothing is ever killed.')}>
            <input id="lp-ready" className="fs-field" value={readinessUrl} onChange={(e) => setReadinessUrl(e.target.value)} placeholder="http://127.0.0.1:5178/api/health" />
          </Field>
          <Field label={t('Timeout (seconds)')} htmlFor="lp-timeout">
            <input id="lp-timeout" type="number" min={1} className="fs-field" value={timeoutS} onChange={(e) => setTimeoutS(e.target.value)} />
          </Field>
        </div>
      )}
      <FormFoot msg={m.msg} tone={m.tone}>
        <Button size="sm" variant="ghost" label={t('Cancel')} onClick={onCancel} disabled={busy} />
        <Button size="sm" variant="primary" label={t('Save')} loading={busy} onClick={() => void save()} testId="launch-profile-save" />
      </FormFoot>
    </div>
  );
}
