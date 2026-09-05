import type { ReactNode } from 'react';
import { t } from '../../../i18n';
import type { ImageModelOption } from '../../../adapters/imageTools';

/** A labelled range with its live value; the one control the tool pane is made of. */
export function Slider({ id, label, value, min, max, step = 1, onChange, onCommit, format, hint }: { id: string; label: string; value: number; min: number; max: number; step?: number; onChange: (v: number) => void; onCommit?: () => void; format?: (v: number) => string; hint?: string }) {
  return (
    <div className="fs-ed__slider">
      <label htmlFor={id}>
        {label}
        <output htmlFor={id}>{format ? format(value) : String(value)}</output>
      </label>
      <input id={id} type="range" min={min} max={max} step={step} value={value} title={hint} onChange={(e) => onChange(Number(e.target.value))} onPointerUp={onCommit} onKeyUp={onCommit} />
    </div>
  );
}

export function Section({ title, help, children, aside }: { title: string; help?: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="fs-ed__section">
      <header className="fs-ed__section-head">
        <h3>{title}</h3>
        {aside}
      </header>
      {help && <p className="fs-ed__help">{help}</p>}
      {children}
    </section>
  );
}

export function Row({ children, wrap }: { children: ReactNode; wrap?: boolean }) {
  return (
    <div className="fs-ed__row" data-wrap={wrap || undefined}>
      {children}
    </div>
  );
}

/** Three-way choice rendered as a radio group (replace / add / subtract, paint / erase…). */
export function Segmented<T extends string>({ label, value, options, onChange }: { label: string; value: T; options: { value: T; label: string; title?: string }[]; onChange: (v: T) => void }) {
  return (
    <div className="fs-ed__seg" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button key={o.value} type="button" role="radio" aria-checked={value === o.value} title={o.title} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function ModelSelect({ id, value, models, kind, onChange }: { id: string; value: string; models: ImageModelOption[]; kind: 'inpaint' | 'generate'; onChange: (v: string) => void }) {
  const list = models.filter((m) => (kind === 'inpaint' ? m.inpaint : m.generate));
  return (
    <div className="fs-ed__field">
      <label htmlFor={id}>{t('Model')}</label>
      <select id={id} className="fs-field" value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">{t('Auto')}</option>
        {list.map((m) => (
          <option key={m.value} value={m.value} disabled={!m.online}>
            {m.label}
            {m.online ? '' : ` — ${t('offline')}`}
          </option>
        ))}
      </select>
    </div>
  );
}

export function TextField({ id, label, value, placeholder, onChange, onEnter }: { id: string; label: string; value: string; placeholder?: string; onChange: (v: string) => void; onEnter?: () => void }) {
  return (
    <div className="fs-ed__field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        className="fs-field"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && onEnter) {
            e.preventDefault();
            onEnter();
          }
        }}
      />
    </div>
  );
}

export const pct = (v: number) => `${Math.round(v)}%`;
export const px = (v: number) => `${Math.round(v)} px`;
export const signedPx = (v: number) => `${v > 0 ? '+' : ''}${Math.round(v)} px`;
export const dec = (v: number) => (v / 100).toFixed(2);
