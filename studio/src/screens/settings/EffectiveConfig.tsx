import { RefreshCw } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Button } from '../../components';
import { getJson } from '../../adapters/api';
import { t } from '../../i18n';
import { Field, Text } from './fields';

/** ARCH-03: the effective-configuration inspector — what actually governs a
 *  turn, resolved global -> project -> role/preset -> model -> turn, with
 *  each value's source, what every weaker level wanted instead, same-level
 *  conflicts and the prompt blocks a turn would actually get. Everything
 *  here comes straight from GET /api/config/effective(/prompt)
 *  (src/effective_config.py); this component computes nothing on its own. */

interface EffectiveValueRow {
  field: string;
  value: unknown;
  source: { layer: string; path: string; line: number | null } | null;
  overridden: { layer: string; value: unknown; path: string; line: number | null }[];
}

interface EffectiveConfigPayload {
  owner: string;
  session_id: string;
  project_id: string;
  model: string;
  computed_at: string;
  hash: string;
  values: Record<string, EffectiveValueRow>;
  conflicts: { field: string; layer: string; reason: string; candidates?: { path: string; line?: number }[] }[];
}

interface PromptBlock { kind: string; name: string; chars: number; text: string }
interface PromptPayload { prompt_chars: number; blocks: PromptBlock[]; note: string }

export function showValue(v: unknown): string {
  if (v === null || v === undefined) return t('(unset)');
  if (typeof v === 'boolean') return v ? t('on') : t('off');
  if (typeof v === 'string') return v.length > 240 ? v.slice(0, 240) + '…' : v;
  return JSON.stringify(v);
}

export function EffectiveConfigSection({ say }: { say: (t: string) => void }) {
  const [sessionId, setSessionId] = useState('');
  const [projectId, setProjectId] = useState('');
  const [model, setModel] = useState('');
  const [cfg, setCfg] = useState<EffectiveConfigPayload | null>(null);
  const [prompt, setPrompt] = useState<PromptPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setErr(null);
    const qs = new URLSearchParams();
    if (sessionId.trim()) qs.set('session', sessionId.trim());
    if (projectId.trim()) qs.set('project', projectId.trim());
    if (model.trim()) qs.set('model', model.trim());
    const suffix = qs.toString() ? `?${qs.toString()}` : '';
    Promise.all([
      getJson<EffectiveConfigPayload>(`/api/config/effective${suffix}`),
      getJson<PromptPayload>(`/api/config/effective/prompt${sessionId.trim() ? `?session=${encodeURIComponent(sessionId.trim())}` : ''}`),
    ])
      .then(([c, p]) => {
        setCfg(c);
        setPrompt(p);
      })
      .catch((e: Error) => {
        setErr(e.message);
        say(e.message);
      })
      .finally(() => setLoading(false));
  }, [sessionId, projectId, model, say]);

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const rows = cfg ? Object.values(cfg.values).sort((a, b) => a.field.localeCompare(b.field)) : [];

  return (
    <section aria-labelledby="fs-effcfg-title">
      <h2 id="fs-effcfg-title" className="fs-set__section-title">{t('Effective config')}</h2>
      <p className="fs-set__help">
        {t('What actually governs a turn, resolved global -> project -> role/preset -> model -> turn. Every value below names the level that won and what every weaker level wanted instead.')}
      </p>

      <div className="fs-set__card">
        <div className="fs-set__grid2">
          <Field label={t('Session id (optional)')} htmlFor="effcfg-session" help={t('Narrows the project/role/model layers to that chat.')}>
            <Text id="effcfg-session" value={sessionId} onChange={setSessionId} placeholder={t('leave empty for the global view')} />
          </Field>
          <Field label={t('Project id (optional)')} htmlFor="effcfg-project">
            <Text id="effcfg-project" value={projectId} onChange={setProjectId} />
          </Field>
          <Field label={t('Model (optional)')} htmlFor="effcfg-model" help={t('Used to look up per-model load options.')}>
            <Text id="effcfg-model" value={model} onChange={setModel} />
          </Field>
        </div>
        <div className="fs-set__row-end">
          <Button size="sm" variant="secondary" icon={RefreshCw} label={t('Recompute')} loading={loading} onClick={load} />
        </div>
      </div>

      {err && <p className="fs-set__err">{err}</p>}

      {cfg && (
        <>
          <div className="fs-set__card">
            <h3 className="fs-set__card-title">{t('Hash')}</h3>
            <p className="fs-set__help">
              {t('Stable fingerprint of the resolved values below — changes when any value changes, not when they are only reordered.')}
            </p>
            <code>{cfg.hash}</code>
          </div>

          {cfg.conflicts.length > 0 && (
            <div className="fs-set__card">
              <h3 className="fs-set__card-title">{t('Conflicts')}</h3>
              <ul>
                {cfg.conflicts.map((c, i) => (
                  <li key={i} className="fs-set__err">
                    <strong>{c.field}</strong> ({c.layer}): {c.reason}
                    {c.candidates && c.candidates.length > 0 && (
                      <> — {c.candidates.map((x) => x.path + (x.line ? `:${x.line}` : '')).join(', ')}</>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="fs-set__card">
            <h3 className="fs-set__card-title">{t('Resolved values')}</h3>
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85em' }}>
                <thead>
                  <tr style={{ textAlign: 'left', borderBottom: '1px solid var(--fs-border, #444)' }}>
                    <th style={{ padding: '4px 8px' }}>{t('Field')}</th>
                    <th style={{ padding: '4px 8px' }}>{t('Value')}</th>
                    <th style={{ padding: '4px 8px' }}>{t('Source')}</th>
                    <th style={{ padding: '4px 8px' }}>{t('Overridden')}</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.field} style={{ borderBottom: '1px solid var(--fs-border, #2a2a2a)' }}>
                      <td style={{ padding: '4px 8px' }}>{r.field}</td>
                      <td style={{ padding: '4px 8px' }}>{showValue(r.value)}</td>
                      <td style={{ padding: '4px 8px' }}>{r.source ? `${r.source.layer} — ${r.source.path}` : t('(no level stated it)')}</td>
                      <td style={{ padding: '4px 8px' }}>
                        {r.overridden.length === 0
                          ? '—'
                          : r.overridden.map((o) => `${o.layer}: ${showValue(o.value)}`).join('; ')}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}

      {prompt && (
        <div className="fs-set__card">
          <h3 className="fs-set__card-title">{t('Prompt blocks for the next turn')}</h3>
          <p className="fs-set__help">{prompt.note}</p>
          {prompt.blocks.map((b, i) => (
            <details key={i}>
              <summary>
                <code>{b.kind}</code> — {b.name} ({t('{n} chars', { n: b.chars })})
              </summary>
              <pre className="fs-set__help" style={{ whiteSpace: 'pre-wrap' }}>{b.text}</pre>
            </details>
          ))}
        </div>
      )}
    </section>
  );
}
