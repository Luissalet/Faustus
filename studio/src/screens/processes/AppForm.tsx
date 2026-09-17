import { useState } from 'react';
import { Button } from '../../components';
import { Field, Select } from '../settings/fields';
import { FormFoot, useMsg } from '../settings/IntegrationForms';
import { appIconUrl, saveApp, type AppKind, type AppProfile, type AppProfileInput } from '../../adapters/apps';
import { t } from '../../i18n';

/**
 * The "Add app"/Edit inline panel — never a native dialog. All the fields
 * `src/launch_profiles.py` validates: name, description, kind, executable,
 * argv (textarea, one per line), cwd, env (textarea, KEY=value per line),
 * readiness url + timeout, open_url, icon path (with a live preview once
 * saved, via the icon route), stop command, and the desktop-window toggle.
 */

const KINDS: { value: AppKind; label: string }[] = [
  { value: 'process', label: 'Server process (with readiness check)' },
  { value: 'open_exe', label: 'Desktop program (no readiness check)' },
  { value: 'open_url', label: 'Web address' },
];

function linesToArgv(s: string): string[] {
  return s.split('\n').map((x) => x.trim()).filter(Boolean);
}
function argvToLines(a: string[]): string {
  return a.join('\n');
}
function envToLines(env: Record<string, string>): string {
  return Object.entries(env).map(([k, v]) => `${k}=${v}`).join('\n');
}
function linesToEnv(s: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of s.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const eq = trimmed.indexOf('=');
    if (eq <= 0) continue;
    out[trimmed.slice(0, eq).trim()] = trimmed.slice(eq + 1).trim();
  }
  return out;
}

export function AppForm({ existing, onCancel, onSaved }: { existing?: AppProfile; onCancel: () => void; onSaved: (p: AppProfile) => void }) {
  const [name, setName] = useState(existing?.name ?? '');
  const [description, setDescription] = useState(existing?.description ?? '');
  const [kind, setKind] = useState<AppKind>(existing?.kind ?? 'process');
  const [executable, setExecutable] = useState(existing?.executable ?? '');
  const [argvText, setArgvText] = useState(argvToLines(existing?.argv ?? []));
  const [cwd, setCwd] = useState(existing?.cwd ?? '');
  const [envText, setEnvText] = useState(envToLines(existing?.env ?? {}));
  const [readinessUrl, setReadinessUrl] = useState(existing?.readiness?.url ?? '');
  const [timeoutS, setTimeoutS] = useState(String(existing?.readiness?.timeout_s ?? 20));
  const [openUrl, setOpenUrl] = useState(existing?.open_url ?? '');
  const [url, setUrl] = useState(existing?.url ?? '');
  const [icon, setIcon] = useState(existing?.icon ?? '');
  const [stopExe, setStopExe] = useState(existing?.stop_cmd?.executable ?? '');
  const [stopArgvText, setStopArgvText] = useState(argvToLines(existing?.stop_cmd?.argv ?? []));
  const [stopCwd, setStopCwd] = useState(existing?.stop_cmd?.cwd ?? '');
  const [desktop, setDesktop] = useState(existing?.desktop ?? true);
  const [busy, setBusy] = useState(false);
  const [iconBroken, setIconBroken] = useState(false);
  const [saved, setSaved] = useState<AppProfile | undefined>(existing);
  const m = useMsg();

  const save = async () => {
    m.clear();
    if (!name.trim()) return m.bad(t('A name is required.'));
    if (kind !== 'open_url' && !executable.trim()) return m.bad(t('An absolute path to the executable is required.'));
    if (kind === 'process' && !cwd.trim()) return m.bad(t('A working folder is required for a server process.'));
    setBusy(true);
    try {
      const body: AppProfileInput = {
        name: name.trim(),
        kind,
        executable: executable.trim(),
        argv: linesToArgv(argvText),
        cwd: cwd.trim(),
        env: linesToEnv(envText),
        readiness: readinessUrl.trim() ? { url: readinessUrl.trim(), timeout_s: Number(timeoutS) || 20 } : null,
        url: kind === 'open_url' ? url.trim() || null : null,
        icon: icon.trim() || null,
        open_url: openUrl.trim() || null,
        stop_cmd: stopExe.trim() ? { executable: stopExe.trim(), argv: linesToArgv(stopArgvText), cwd: stopCwd.trim() || null } : null,
        description: description.trim() || null,
        desktop,
      };
      const result = await saveApp(existing?.id ?? null, body);
      setSaved(result);
      setIconBroken(false);
      onSaved(result);
    } catch (e) {
      m.bad((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fs-apps__form" data-testid="apps-form">
      <h4 className="fs-users__h">{existing ? t('Edit app') : t('Add app')}</h4>
      <div className="fs-set__grid2">
        <Field label={t('Name')} htmlFor="app-name">
          <input id="app-name" className="fs-field" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label={t('Kind')} htmlFor="app-kind">
          <Select id="app-kind" value={kind} options={KINDS.map((k) => ({ value: k.value, label: t(k.label) }))} onChange={(v) => setKind(v as AppKind)} />
        </Field>
      </div>
      <Field label={t('Description')} htmlFor="app-desc">
        <input id="app-desc" className="fs-field" value={description} onChange={(e) => setDescription(e.target.value)} placeholder={t('What this app is, in a few words')} />
      </Field>
      {kind === 'open_url' ? (
        <Field label={t('URL')} htmlFor="app-url">
          <input id="app-url" className="fs-field" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="http://127.0.0.1:5178" />
        </Field>
      ) : (
        <>
          <Field label={t('Executable (absolute path)')} htmlFor="app-exe" help={t('The exact program to run — never a shell string.')}>
            <input id="app-exe" className="fs-field" value={executable} onChange={(e) => setExecutable(e.target.value)} placeholder="/usr/bin/node" />
          </Field>
          <Field label={t('Arguments (one per line)')} htmlFor="app-argv">
            <textarea id="app-argv" className="fs-field fs-apps__textarea" rows={3} value={argvText} onChange={(e) => setArgvText(e.target.value)} placeholder={'--port\n5178'} />
          </Field>
          <div className="fs-set__grid2">
            <Field label={t('Working folder')} htmlFor="app-cwd">
              <input id="app-cwd" className="fs-field" value={cwd} onChange={(e) => setCwd(e.target.value)} />
            </Field>
            <Field label={t('Environment (KEY=value per line)')} htmlFor="app-env">
              <textarea id="app-env" className="fs-field fs-apps__textarea" rows={3} value={envText} onChange={(e) => setEnvText(e.target.value)} placeholder="PORT=5178" />
            </Field>
          </div>
        </>
      )}
      <div className="fs-set__grid2">
        <Field label={t('Readiness URL (optional)')} htmlFor="app-ready" help={t('Polled until it answers, or the timeout runs out — nothing is ever killed.')}>
          <input id="app-ready" className="fs-field" value={readinessUrl} onChange={(e) => setReadinessUrl(e.target.value)} placeholder="http://127.0.0.1:5178/api/health" />
        </Field>
        <Field label={t('Timeout (seconds)')} htmlFor="app-timeout">
          <input id="app-timeout" type="number" min={1} className="fs-field" value={timeoutS} onChange={(e) => setTimeoutS(e.target.value)} />
        </Field>
      </div>
      <Field label={t('Open URL (optional)')} htmlFor="app-openurl" help={t('Opened once the app is running — its own browser tab or desktop window.')}>
        <input id="app-openurl" className="fs-field" value={openUrl} onChange={(e) => setOpenUrl(e.target.value)} placeholder="http://127.0.0.1:5178" />
      </Field>
      <Field label={t('Icon (absolute path to .png/.ico/.svg/.jpg)')} htmlFor="app-icon">
        <span className="fs-apps__icon-row">
          <input id="app-icon" className="fs-field" value={icon} onChange={(e) => setIcon(e.target.value)} placeholder="/path/to/icon.png" />
          {saved?.id && icon && !iconBroken && (
            <img className="fs-apps__icon-preview" src={appIconUrl(saved.id)} alt="" onError={() => setIconBroken(true)} />
          )}
        </span>
      </Field>
      <Field label={t('Stop command (optional)')} htmlFor="app-stop-exe" help={t('Run instead of the default stop, e.g. a stop script.')}>
        <input id="app-stop-exe" className="fs-field" value={stopExe} onChange={(e) => setStopExe(e.target.value)} placeholder="/usr/bin/pwsh" />
      </Field>
      {stopExe.trim() && (
        <div className="fs-set__grid2">
          <Field label={t('Stop arguments (one per line)')} htmlFor="app-stop-argv">
            <textarea id="app-stop-argv" className="fs-field fs-apps__textarea" rows={2} value={stopArgvText} onChange={(e) => setStopArgvText(e.target.value)} />
          </Field>
          <Field label={t('Stop working folder')} htmlFor="app-stop-cwd">
            <input id="app-stop-cwd" className="fs-field" value={stopCwd} onChange={(e) => setStopCwd(e.target.value)} />
          </Field>
        </div>
      )}
      <label className="fs-set__toggle" htmlFor="app-desktop">
        <input id="app-desktop" type="checkbox" role="switch" checked={desktop} onChange={(e) => setDesktop(e.target.checked)} />
        <span className="fs-set__toggle-track" aria-hidden="true" />
        <span>{t('Open as a desktop window')}</span>
      </label>
      <FormFoot msg={m.msg} tone={m.tone}>
        <Button size="sm" variant="ghost" label={t('Cancel')} onClick={onCancel} disabled={busy} />
        <Button size="sm" variant="primary" label={t('Save')} loading={busy} onClick={() => void save()} testId="apps-form-save" />
      </FormFoot>
    </div>
  );
}
