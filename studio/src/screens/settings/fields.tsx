import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Button } from '../../components';
import type { Settings } from '../../adapters/settings';
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

export function Toggle({ id, checked, onChange, label }: { id: string; checked: boolean; onChange: (v: boolean) => void; label?: string }) {
  return (
    <label className="fs-set__toggle" htmlFor={id}>
      <input id={id} type="checkbox" role="switch" checked={checked} onChange={(e) => onChange(e.target.checked)} />
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

export function Text({ id, value, onChange, type = 'text', placeholder, secret }: { id: string; value: string; onChange: (v: string) => void; type?: string; placeholder?: string; secret?: boolean }) {
  return <input id={id} type={secret ? 'password' : type} className="fs-field" value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} autoComplete={secret ? 'new-password' : 'off'} />;
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

