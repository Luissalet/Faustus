/**
 * studio/src/screens/settings/SamplingDefaults.tsx — SET-07's five local
 * sampling defaults (`src/settings.py`, applied to local endpoints only),
 * shared between Settings -> Default AI and Settings -> Local models so
 * neither screen keeps its own copy of the field list or the clamp/patch
 * logic. Both read and write the exact same settings keys
 * (`local_temperature_default`, `local_top_p_default`, `local_top_k_default`,
 * `local_repeat_penalty_default`, `local_min_p_default`); a save from either
 * screen is visible on the other the next time it loads settings.
 *
 * A conversation can still override these for itself from the composer's
 * generation panel (`/temp`, `/topp`, `/topk`) — this group only sets what a
 * new conversation starts from.
 */
import { useState } from 'react';
import { Field, Text } from './fields';
import { t } from '../../i18n';

export interface SamplingField {
  key: string;
  label: string;
  help: string;
  lo: number;
  hi: number;
  decimals: number;
  fallback: number;
}

export const SAMPLING_FIELDS: SamplingField[] = [
  { key: 'local_temperature_default', label: 'Temperature', help: t('Lower = less rambling (0.6 recommended locally)'), lo: 0, hi: 2, decimals: 2, fallback: 0.6 },
  { key: 'local_top_p_default', label: 'top_p', help: t('Trims the tail of the probability distribution (0.8)'), lo: 0, hi: 1, decimals: 2, fallback: 0.8 },
  { key: 'local_top_k_default', label: 'top_k', help: t('How many candidates it considers (20)'), lo: 0, hi: 200, decimals: 0, fallback: 20 },
  { key: 'local_repeat_penalty_default', label: t('Repeat penalty'), help: t('Avoids token loops (1.05)'), lo: 0.5, hi: 2, decimals: 2, fallback: 1.05 },
  { key: 'local_min_p_default', label: 'min_p', help: t('Discards the unlikely (0.05)'), lo: 0, hi: 1, decimals: 2, fallback: 0.05 },
];

export const SAMPLING_KEYS: string[] = SAMPLING_FIELDS.map((f) => f.key);

/**
 * The five fields start EMPTY (the current value shown only as a
 * placeholder) rather than pre-filled, because an empty box means "stop
 * overriding", not "set it to zero" — clearing it must never send zero.
 */
export function useSamplingDraft() {
  const [sampling, setSampling] = useState<Record<string, string>>({});
  const dirty = SAMPLING_FIELDS.some((f) => (sampling[f.key] ?? '').trim() !== '');

  /** Merge the non-empty, clamped fields onto `base` for a save. */
  function withSampling(base: Record<string, unknown>): Record<string, unknown> {
    const patch: Record<string, unknown> = { ...base };
    for (const f of SAMPLING_FIELDS) {
      const raw = (sampling[f.key] ?? '').trim();
      if (raw === '') continue;
      const n = Number(raw);
      if (Number.isNaN(n)) continue;
      const clamped = Math.max(f.lo, Math.min(n, f.hi));
      patch[f.key] = f.decimals === 0 ? Math.round(clamped) : Number(clamped.toFixed(f.decimals));
    }
    return patch;
  }

  function reset(): void {
    setSampling({});
  }

  return { sampling, setSampling, dirty, withSampling, reset };
}

/** The fields themselves — no Save button, so a host screen's own SaveBar
 * covers this group along with whatever else it saves. */
export function SamplingDefaultsFields({
  idPrefix,
  settings,
  sampling,
  setSampling,
}: {
  idPrefix: string;
  settings: Record<string, unknown> | null;
  sampling: Record<string, string>;
  setSampling: (fn: (s: Record<string, string>) => Record<string, string>) => void;
}) {
  return (
    <Field label={t('Local sampling')} help={t('Only for local endpoints (Ollama, LM Studio…). Empty: do not send. A conversation can still override these for itself from the composer\'s generation panel.')}>
      <div className="fs-set__grid2">
        {SAMPLING_FIELDS.map((f) => {
          const current = settings?.[f.key];
          const placeholder = typeof current === 'number' ? String(current) : String(f.fallback);
          return (
            <Field key={f.key} label={f.label} htmlFor={`${idPrefix}-${f.key}`} help={f.help}>
              <Text
                id={`${idPrefix}-${f.key}`}
                type="number"
                value={sampling[f.key] ?? ''}
                onChange={(v) => setSampling((s) => ({ ...s, [f.key]: v }))}
                placeholder={placeholder}
              />
            </Field>
          );
        })}
      </div>
    </Field>
  );
}
