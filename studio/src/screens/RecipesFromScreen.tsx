import { MonitorPlay, Sparkles, Wand2 } from 'lucide-react';
import { useState } from 'react';
import { Button } from '../components';
import * as api from '../adapters/recipes';
import { t } from '../i18n';

/**
 * From the screen (Hangar E40): Talos's Hoard drafts a procedure from what was
 * seen on screen (or from Baird's screenshots of another computer captured over
 * HDMI); here each observed step can be mapped to a Faustus tool and its
 * arguments, with a suggested tool per step that a person confirms, and the
 * mapped steps become a learned recipe. Other observations of the same
 * procedure turn what changed into parameters and extra steps into branches.
 * Unmapped steps and what the recording could not see become the recipe's
 * limits. Nothing runs on the observed computer.
 */
type Origin = 'talos' | 'baird';

/** The "other observations" box: a JSON list whose items are Talos drafts or event lists. */
function parseObservations(text: string): unknown[] {
  if (!text.trim()) return [];
  const parsed: unknown = JSON.parse(text);
  const list = Array.isArray(parsed) ? parsed : [parsed];
  return list.map((item) => (Array.isArray(item) ? { events: item } : item));
}

export function FromScreen({ onCreated }: { onCreated: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const [origin, setOrigin] = useState<Origin>('talos');
  const [start, setStart] = useState('-30m');
  const [end, setEnd] = useState('');
  const [pasted, setPasted] = useState('');
  const [others, setOthers] = useState('');
  const [title, setTitle] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [source, setSource] = useState<api.ScreenSource | null>(null);
  const [summary, setSummary] = useState<api.ScreenSummary | null>(null);
  const [suggestions, setSuggestions] = useState<api.ScreenSuggestion[]>([]);
  const [comparison, setComparison] = useState<api.ScreenComparison | null>(null);
  const [bairdEvents, setBairdEvents] = useState<unknown[] | null>(null);
  const [tools, setTools] = useState<Record<number, string>>({});
  const [args, setArgs] = useState<Record<number, string>>({});
  const [made, setMade] = useState<{ id: string; manual: number[]; limits: string[] } | null>(null);

  const read = async () => {
    setError(''); setMade(null); setBairdEvents(null); setBusy(true);
    try {
      let src: api.ScreenSource = { start, end, title, source: origin };
      if (pasted.trim()) {
        const parsed = JSON.parse(pasted);
        src = Array.isArray(parsed) ? { events: parsed, title } : { draft: parsed, title };
      }
      const observations = parseObservations(others);
      if (observations.length) src.observations = observations;
      const out = await api.screenDraft(src);
      setSource({ draft: out.draft, title, observations: observations.length ? observations : undefined });
      setSummary(out.summary);
      setSuggestions(out.suggestions?.suggestions ?? []);
      setComparison(out.comparison ?? null);
      setTools({}); setArgs({});
    } catch (e) {
      setSummary(null); setSource(null); setSuggestions([]); setComparison(null);   // never leave an earlier draft on show
      const events = (e as { body?: { events?: unknown[] } }).body?.events;
      if (Array.isArray(events) && events.length) setBairdEvents(events);
      setError(e instanceof SyntaxError ? t('The pasted text is not JSON (a Talos draft or an event list).') : (e as Error).message);
    } finally { setBusy(false); }
  };

  /** Fill the tool and arguments of every step that has a suggested tool; the person still reviews them. */
  const applySuggestions = () => {
    const nextTools = { ...tools };
    const nextArgs = { ...args };
    for (const s of suggestions) {
      if (s.action !== 'map' || !s.tool) continue;
      nextTools[s.step] = s.tool;
      nextArgs[s.step] = JSON.stringify(s.args);
    }
    setTools(nextTools); setArgs(nextArgs);
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
  const suggestionFor = (n: number) => suggestions.find((s) => s.step === n);
  const suggestable = suggestions.filter((s) => s.action === 'map').length;
  const sugLabel = (s: api.ScreenSuggestion) => (s.action === 'map'
    ? `${t('Suggested')}: ${s.tool} (${s.confidence === 'high' ? t('sure') : s.confidence === 'medium' ? t('likely') : t('check it')})`
    : s.action === 'skip' ? t('No tool needed') : t('Stays manual'));
  return (
    <section className="fs-rcp__block" aria-label={t('Learn from the screen')} data-testid="rcp-screen">
      <h2 className="fs-rcp__title">{t('Learn from the screen')}</h2>
      <p className="fs-rcp__muted">{t("Talos's Hoard drafts the steps it saw on screen (windows, clipboard, screenshots, files), with secrets masked and what it could not see listed. Map each step a Faustus tool should repeat; the rest stays manual and is written into the recipe's limits. Then try the recipe on a second case.")}</p>
      <div className="fs-rcp__row">
        <label className="fs-rcp__field">{t('Source')}
          <select className="fs-field" value={origin} onChange={(e) => setOrigin(e.target.value as Origin)} data-testid="rcp-screen-source">
            <option value="talos">{t('This computer (Talos)')}</option>
            <option value="baird">{t('Other computer over HDMI (Baird)')}</option>
          </select>
        </label>
        <label className="fs-rcp__field">{t('From')}<input className="fs-field" value={start} onChange={(e) => setStart(e.target.value)} data-testid="rcp-screen-start" /></label>
        <label className="fs-rcp__field">{t('To')}<input className="fs-field" value={end} onChange={(e) => setEnd(e.target.value)} placeholder={t('now')} /></label>
        <label className="fs-rcp__field">{t('Title')}<input className="fs-field" value={title} onChange={(e) => setTitle(e.target.value)} data-testid="rcp-screen-title" /></label>
      </div>
      {origin === 'baird' && <p className="fs-rcp__muted">{t("Baird's screenshots of the other computer in that range become the steps. Only pictures: no window titles, keystrokes or clipboard, and nothing is done on that computer.")}</p>}
      <textarea className="fs-field" rows={3} value={pasted} onChange={(e) => setPasted(e.target.value)} data-testid="rcp-screen-paste"
        placeholder={t('Or paste a draft exported from Talos, or an event list [{ts, type, app, title, text, field}]')} />
      <textarea className="fs-field" rows={2} value={others} onChange={(e) => setOthers(e.target.value)} data-testid="rcp-screen-others"
        placeholder={t('Other observations of the same procedure (optional): a JSON list of Talos drafts or event lists. What changes between them becomes a parameter.')} />
      <div><Button icon={MonitorPlay} label={busy && !summary ? t('Reading…') : t('Read the steps')} disabled={busy} onClick={() => { void read(); }} testId="rcp-screen-read" /></div>
      {error && <p className="fs-rcp__why" data-tone="bad" role="alert">{error}</p>}
      {bairdEvents && (
        <div><Button variant="secondary" size="sm" label={`${t('Put the screenshot events in the box')} (${bairdEvents.length})`}
          onClick={() => { setPasted(JSON.stringify(bairdEvents, null, 1)); setOrigin('talos'); }} testId="rcp-screen-use-events" /></div>
      )}
      {summary && (
        <>
          <p className="fs-rcp__muted">{summary.title} · {summary.window.events ?? 0} {t('events')}{summary.masked_values ? ` · ${summary.masked_values} ${t('masked')}` : ''}</p>
          {suggestions.length > 0 && (
            <div className="fs-rcp__row">
              <Button variant="secondary" size="sm" icon={Wand2} label={`${t('Use the suggested tools')} (${suggestable})`} disabled={busy || suggestable === 0}
                onClick={applySuggestions} testId="rcp-screen-suggest" />
              <span className="fs-rcp__muted">{t('Suggestions come from the app and the kind of each step; check every tool and its arguments before proposing the recipe.')}</span>
            </div>
          )}
          <table className="fs-rcp__table" data-testid="rcp-screen-steps">
            <thead><tr><th>#</th><th>{t('Observed step')}</th><th>{t('Faustus tool')}</th><th>{t('Arguments (JSON)')}</th></tr></thead>
            <tbody>
              {summary.steps.map((s) => {
                const sg = suggestionFor(s.n);
                const seen = comparison?.steps.find((x) => x.n === s.n)?.seen_in;
                return (
                  <tr key={s.n}>
                    <td>{s.n}</td>
                    <td>{s.title}<div className="fs-rcp__muted">{s.app ?? '—'}{s.inputs.length ? ` · ${t('inputs')}: ${s.inputs.join(', ')}` : ''}
                      {comparison && seen !== undefined && seen < comparison.observations ? ` · ${t('seen in')} ${seen}/${comparison.observations}` : ''}</div></td>
                    <td><input className="fs-field" value={tools[s.n] ?? ''} onChange={(e) => setTools({ ...tools, [s.n]: e.target.value })} placeholder={t('manual')} data-testid={`rcp-screen-tool-${s.n}`} />
                      {sg && <div className="fs-rcp__muted" title={sg.why} data-testid={`rcp-screen-sug-${s.n}`}>{sugLabel(sg)}<br />{sg.why}</div>}</td>
                    <td><textarea className="fs-field fs-rcp__mono" rows={2} value={args[s.n] ?? ''} onChange={(e) => setArgs({ ...args, [s.n]: e.target.value })} placeholder="{}" data-testid={`rcp-screen-args-${s.n}`} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {summary.inputs.length > 0 && <p className="fs-rcp__muted">{t('Inputs seen')}: {summary.inputs.map((i) => `${i.name}${i.masked ? ` (${t('masked')})` : i.example ? ` = ${i.example}` : ''}`).join(' · ')}</p>}
          {comparison && (
            <div className="fs-rcp__muted" data-testid="rcp-screen-compare">
              <strong>{comparison.observations} {t('observations')}</strong>
              {' · '}{t('Become parameters')}: {comparison.parameters.join(', ') || '—'}
              {' · '}{t('Stay as seen')}: {comparison.constants.join(', ') || '—'}
              {comparison.branches.length > 0 && <> {' · '}{t('Branches')}: {comparison.branches.map((b) => `${b.title} (${t('observation')} ${b.observation})`).join('; ')}</>}
            </div>
          )}
          {summary.unknowns.length > 0 && <ul className="fs-rcp__muted" data-testid="rcp-screen-unknowns">{summary.unknowns
            .filter((u) => !(comparison && u.code === 'single_run'))   // several observations answer that one
            .map((u, i) => <li key={i}>{u.message}</li>)}</ul>}
          <div><Button variant="primary" icon={Sparkles} label={t('Propose recipe')} disabled={busy || mappedCount === 0} onClick={() => { void propose(); }} testId="rcp-screen-propose" /></div>
        </>
      )}
      {made && <p className="fs-rcp__muted" data-testid="rcp-screen-made">{t('Recipe proposed')}: {made.id}{made.manual.length ? ` · ${t('manual steps')}: ${made.manual.join(', ')}` : ''}</p>}
    </section>
  );
}
