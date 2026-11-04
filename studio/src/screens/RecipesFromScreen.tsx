import { MonitorPlay, Sparkles } from 'lucide-react';
import { useState } from 'react';
import { Button } from '../components';
import * as api from '../adapters/recipes';
import { t } from '../i18n';

/**
 * From the screen (Hangar E40): Talos's Hoard drafts a procedure from what was
 * seen on screen; here each observed step can be mapped to a Faustus tool and
 * its arguments, and the mapped steps become a learned recipe. Unmapped steps
 * and what the recording could not see become the recipe's limits. Nothing
 * runs on the observed computer.
 */
export function FromScreen({ onCreated }: { onCreated: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const [start, setStart] = useState('-30m');
  const [end, setEnd] = useState('');
  const [pasted, setPasted] = useState('');
  const [title, setTitle] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [source, setSource] = useState<api.ScreenSource | null>(null);
  const [summary, setSummary] = useState<api.ScreenSummary | null>(null);
  const [tools, setTools] = useState<Record<number, string>>({});
  const [args, setArgs] = useState<Record<number, string>>({});
  const [made, setMade] = useState<{ id: string; manual: number[]; limits: string[] } | null>(null);

  const read = async () => {
    setError(''); setMade(null); setBusy(true);
    try {
      let src: api.ScreenSource = { start, end, title };
      if (pasted.trim()) {
        const parsed = JSON.parse(pasted);
        src = Array.isArray(parsed) ? { events: parsed, title } : { draft: parsed, title };
      }
      const out = await api.screenDraft(src);
      setSource({ draft: out.draft, title });
      setSummary(out.summary);
      setTools({}); setArgs({});
    } catch (e) {
      setError(e instanceof SyntaxError ? t('The pasted text is not JSON (a Talos draft or an event list).') : (e as Error).message);
    } finally { setBusy(false); }
  };

  const propose = async () => {
    if (!source) return;
    setError(''); setBusy(true);
    try {
      const mapping = Object.entries(tools).filter(([, tool]) => tool.trim()).map(([n, tool]) => {
        const raw = (args[Number(n)] ?? '').trim();
        return { step: Number(n), tool: tool.trim(), args: raw ? JSON.parse(raw) as Record<string, unknown> : {} };
      });
      const out = await api.recipeFromScreen({ ...source, title: title || undefined }, mapping);
      setMade({ id: out.recipe.id, manual: out.manual_steps.map((m) => m.n), limits: out.recipe.limits });
      onCreated(out.recipe.id);
    } catch (e) {
      setError(e instanceof SyntaxError ? t('The arguments of a step are not valid JSON.') : (e as Error).message);
    } finally { setBusy(false); }
  };

  if (!open) {
    return <div><Button variant="secondary" size="sm" icon={MonitorPlay} label={t('Learn from the screen')} onClick={() => setOpen(true)} testId="rcp-screen-open" /></div>;
  }
  const mappedCount = Object.values(tools).filter((x) => x.trim()).length;
  return (
    <section className="fs-rcp__block" aria-label={t('Learn from the screen')} data-testid="rcp-screen">
      <h2 className="fs-rcp__title">{t('Learn from the screen')}</h2>
      <p className="fs-rcp__muted">{t("Talos's Hoard drafts the steps it saw on screen (windows, clipboard, screenshots, files), with secrets masked and what it could not see listed. Map each step a Faustus tool should repeat; the rest stays manual and is written into the recipe's limits. Then try the recipe on a second case.")}</p>
      <div className="fs-rcp__row">
        <label className="fs-rcp__field">{t('From')}<input className="fs-field" value={start} onChange={(e) => setStart(e.target.value)} data-testid="rcp-screen-start" /></label>
        <label className="fs-rcp__field">{t('To')}<input className="fs-field" value={end} onChange={(e) => setEnd(e.target.value)} placeholder={t('now')} /></label>
        <label className="fs-rcp__field">{t('Title')}<input className="fs-field" value={title} onChange={(e) => setTitle(e.target.value)} data-testid="rcp-screen-title" /></label>
      </div>
      <textarea className="fs-field" rows={3} value={pasted} onChange={(e) => setPasted(e.target.value)} data-testid="rcp-screen-paste"
        placeholder={t('Or paste a draft exported from Talos, or an event list [{ts, type, app, title, text, field}]')} />
      <div><Button icon={MonitorPlay} label={busy && !summary ? t('Reading…') : t('Read the steps')} disabled={busy} onClick={() => { void read(); }} testId="rcp-screen-read" /></div>
      {error && <p className="fs-rcp__why" data-tone="bad" role="alert">{error}</p>}
      {summary && (
        <>
          <p className="fs-rcp__muted">{summary.title} · {summary.window.events ?? 0} {t('events')}{summary.masked_values ? ` · ${summary.masked_values} ${t('masked')}` : ''}</p>
          <table className="fs-rcp__table" data-testid="rcp-screen-steps">
            <thead><tr><th>#</th><th>{t('Observed step')}</th><th>{t('Faustus tool')}</th><th>{t('Arguments (JSON)')}</th></tr></thead>
            <tbody>
              {summary.steps.map((s) => (
                <tr key={s.n}>
                  <td>{s.n}</td>
                  <td>{s.title}<div className="fs-rcp__muted">{s.app ?? '—'}{s.inputs.length ? ` · ${t('inputs')}: ${s.inputs.join(', ')}` : ''}</div></td>
                  <td><input className="fs-field" value={tools[s.n] ?? ''} onChange={(e) => setTools({ ...tools, [s.n]: e.target.value })} placeholder={t('manual')} data-testid={`rcp-screen-tool-${s.n}`} /></td>
                  <td><textarea className="fs-field fs-rcp__mono" rows={2} value={args[s.n] ?? ''} onChange={(e) => setArgs({ ...args, [s.n]: e.target.value })} placeholder="{}" data-testid={`rcp-screen-args-${s.n}`} /></td>
                </tr>
              ))}
            </tbody>
          </table>
          {summary.inputs.length > 0 && <p className="fs-rcp__muted">{t('Inputs seen')}: {summary.inputs.map((i) => `${i.name}${i.masked ? ` (${t('masked')})` : i.example ? ` = ${i.example}` : ''}`).join(' · ')}</p>}
          {summary.unknowns.length > 0 && <ul className="fs-rcp__muted" data-testid="rcp-screen-unknowns">{summary.unknowns.map((u, i) => <li key={i}>{u.message}</li>)}</ul>}
          <div><Button variant="primary" icon={Sparkles} label={t('Propose recipe')} disabled={busy || mappedCount === 0} onClick={() => { void propose(); }} testId="rcp-screen-propose" /></div>
        </>
      )}
      {made && <p className="fs-rcp__muted" data-testid="rcp-screen-made">{t('Recipe proposed')}: {made.id}{made.manual.length ? ` · ${t('manual steps')}: ${made.manual.join(', ')}` : ''}</p>}
    </section>
  );
}
