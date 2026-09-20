import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { Button } from '../../components';
import type { Settings } from '../../adapters/settings';
import { getDefaultResidency, setDefaultResidency, type DefaultResidency } from '../../adapters/localModels';
import { t } from '../../i18n';

/* ── Field primitives ── */

export type Opt = { value: string; label: string };

export function Field({ label, help, htmlFor, children }: { label: string; help?: string; htmlFor?: string; children: ReactNode }) {
  return (
    <div className="fs-set__field">
      <label className="fs-set__label" htmlFor={htmlFor}>
        {label}
      </label>
      <div className="fs-set__control">{children}</div>
      {help && <p className="fs-set__help">{help}</p>}
    </div>
  );
}

export function Toggle({ id, checked, onChange, label, disabled }: { id: string; checked: boolean; onChange: (v: boolean) => void; label?: string; disabled?: boolean }) {
  return (
    <label className="fs-set__toggle" htmlFor={id} data-disabled={disabled || undefined}>
      <input id={id} type="checkbox" role="switch" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span className="fs-set__toggle-track" aria-hidden="true" />
      {label && <span>{label}</span>}
    </label>
  );
}

export function Select({ id, value, options, onChange, allowEmpty }: { id: string; value: string; options: Opt[]; onChange: (v: string) => void; allowEmpty?: string }) {
  const known = options.some((o) => o.value === value);
  return (
    <select id={id} className="fs-field" value={value} onChange={(e) => onChange(e.target.value)}>
      {allowEmpty !== undefined && <option value="">{allowEmpty}</option>}
      {!known && value && <option value={value}>{value}</option>}
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

export function Text({ id, value, onChange, type = 'text', placeholder, secret, disabled }: { id: string; value: string; onChange: (v: string) => void; type?: string; placeholder?: string; secret?: boolean; disabled?: boolean }) {
  return <input id={id} type={secret ? 'password' : type} className="fs-field" value={value} placeholder={placeholder} disabled={disabled} onChange={(e) => onChange(e.target.value)} autoComplete={secret ? 'new-password' : 'off'} />;
}

/* A section with its own draft, dirty flag and Save. */
export function useDraft(settings: Settings | null, keys: string[]) {
  const [draft, setDraft] = useState<Settings>({});
  useEffect(() => {
    if (!settings) return;
    const next: Settings = {};
    for (const k of keys) next[k] = settings[k];
    setDraft(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings]);
  const set = (k: string, v: unknown) => setDraft((d) => ({ ...d, [k]: v }));
  const changed = useMemo(() => {
    const out: Settings = {};
    if (!settings) return out;
    for (const k of keys) if (JSON.stringify(draft[k]) !== JSON.stringify(settings[k])) out[k] = draft[k];
    return out;
  }, [draft, settings, keys]);
  return { draft, set, changed, dirty: Object.keys(changed).length > 0 };
}

export function str(v: unknown, fallback = ''): string {
  return v === null || v === undefined ? fallback : String(v);
}
export function bool(v: unknown): boolean {
  return v === true || v === 'true' || v === 1;
}
export function list(v: unknown): string {
  return Array.isArray(v) ? v.join(', ') : str(v);
}
export function fromList(s: string): string[] {
  return s.split(/[,\n]/).map((x) => x.trim()).filter(Boolean);
}

export function SaveBar({ dirty, saving, onSave, note }: { dirty: boolean; saving: boolean; onSave: () => void; note?: string }) {
  return (
    <div className="fs-set__save" data-dirty={dirty || undefined}>
      <span className="fs-set__save-note">{dirty ? t('There are unsaved changes.') : note ?? t('No changes.')}</span>
      <Button variant="primary" size="sm" label={t('Save')} disabled={!dirty} loading={saving} onClick={onSave} testId="settings-save" />
    </div>
  );
}

/**
 * "Load the default model at startup and keep it loaded" — the residency
 * switch (src/model_warmup.py, `warm_default_model`, `GET`/`POST
 * /api/models/default/residency`). Shared between Settings → Default AI and
 * Settings → Local models so both screens read/act on the exact same
 * backend state, never a local copy of it. Saves and takes effect
 * immediately on toggle (unlike the rest of the Defaults form, which
 * batches changes behind a Save button) — this is a live switch, not a
 * draft field.
 */
export function DefaultResidencyField({ testId }: { testId?: string }) {
  const [state, setState] = useState<DefaultResidency | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const refresh = useCallback(() => {
    void getDefaultResidency().then(setState).catch(() => {
      /* best-effort: a failed read just leaves the last known state up */
    });
  }, []);

  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, 20000);
    return () => window.clearInterval(id);
  }, [refresh]);

  const toggle = async (enabled: boolean) => {
    setBusy(true);
    setErr('');
    // Optimistic: the switch itself should feel instant even though the
    // load/release it triggers can take a few seconds.
    setState((cur) => (cur ? { ...cur, enabled } : cur));
    try {
      const next = await setDefaultResidency(enabled);
      setState(next);
    } catch (e) {
      setErr((e as Error).message || t('Could not change this.'));
      refresh();
    } finally {
      setBusy(false);
    }
  };

  if (!state) return null;

  const statusText = !state.enabled
    ? t('Off — the model loads on first use instead.')
    : state.loaded
      ? state.since
        ? t('Loaded ({backend}) since {time}.', { backend: state.backendLabel ?? t('unknown server'), time: new Date(state.since * 1000).toLocaleTimeString() })
        : t('Loaded ({backend}).', { backend: state.backendLabel ?? t('unknown server') })
      : t('Not loaded yet — starting…');

  return (
    <Field label={t('Keep the default model loaded')} htmlFor="default-residency" help={t('Loads the default chat model when Faustus starts and keeps it resident, so the first answer of the day is not slowed by a cold load.')}>
      <div className="fs-set__inline" data-testid={testId}>
        <Toggle id="default-residency" checked={state.enabled} onChange={(v) => void toggle(v)} disabled={busy} label={state.enabled ? t('On') : t('Off')} />
        <span className="fs-set__help" data-testid="default-residency-status">{statusText}</span>
      </div>
      {err && <p className="fs-set__help" role="alert">{err}</p>}
    </Field>
  );
}

