import { Plus, Repeat, Ruler, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router';
import { Button, EmptyState, Skeleton } from '../components';
import * as api from '../adapters/loopCards';
import { t, tn } from '../i18n';
import './loop-cards.css';

/**
 * Loop Cards: before improving something, the metric that says whether it improved, how much
 * that number moves on its own, and the smallest change that counts. A check is a word first
 * (better / worse / within the noise); the tint only repeats it.
 */

function Pill({ verdict }: { verdict: api.Verdict }) {
  return <span className="fs-loop__pill" data-tone={api.verdictTone(verdict)} data-verdict={verdict}>{api.verdictLabel(verdict)}</span>;
}

function NewCard({ project, onCreated }: { project: string | null; onCreated: () => void }) {
  const [goal, setGoal] = useState('');
  const [command, setCommand] = useState('');
  const [kind, setKind] = useState<'regex' | 'json' | 'time'>('time');
  const [detail, setDetail] = useState('');
  const [direction, setDirection] = useState<'up' | 'down'>('down');
  const [repeats, setRepeats] = useState(5);
  const [cwd, setCwd] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const submit = async () => {
    setBusy(true); setError('');
    try {
      await api.createCard({
        goal, command, direction, repeats, cwd: cwd.trim() || undefined, project: project || undefined,
        extract: kind === 'time' ? { kind } : kind === 'regex' ? { kind, pattern: detail } : { kind, path: detail },
      });
      setGoal(''); setCommand(''); setDetail('');
      onCreated();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="fs-loop__form" aria-label={t('New loop card')} data-testid="loop-form">
      <h2 className="fs-loop__h">{t('New loop card')}</h2>
      <label className="fs-loop__field">{t('What do you want to improve?')}
        <input className="fs-field" value={goal} onChange={(e) => setGoal(e.target.value)} data-testid="loop-goal" />
      </label>
      <label className="fs-loop__field">{t('Command that measures it')}
        <input className="fs-field" value={command} onChange={(e) => setCommand(e.target.value)} data-testid="loop-command" />
      </label>
      <div className="fs-loop__row">
        <label className="fs-loop__field">{t('Read the number from')}
          <select className="fs-field" value={kind} onChange={(e) => setKind(e.target.value as typeof kind)} data-testid="loop-kind">
            <option value="time">{t('Run time')}</option>
            <option value="regex">{t('Text (regex with one group)')}</option>
            <option value="json">{t('JSON path')}</option>
          </select>
        </label>
        {kind !== 'time' && (
          <label className="fs-loop__field fs-loop__grow">{kind === 'regex' ? t('Regex, number in the group') : t('JSON path, e.g. stats.p95')}
            <input className="fs-field" value={detail} onChange={(e) => setDetail(e.target.value)} data-testid="loop-detail" />
          </label>
        )}
        <label className="fs-loop__field">{t('Better is')}
          <select className="fs-field" value={direction} onChange={(e) => setDirection(e.target.value as 'up' | 'down')} data-testid="loop-direction">
            <option value="down">{t('Lower')}</option>
            <option value="up">{t('Higher')}</option>
          </select>
        </label>
        <label className="fs-loop__field">{t('Runs')}
          <input className="fs-field fs-loop__num" type="number" min={2} max={50} value={repeats} onChange={(e) => setRepeats(Number(e.target.value) || 5)} />
        </label>
      </div>
      <label className="fs-loop__field">{t('Folder to run in (optional)')}
        <input className="fs-field" value={cwd} onChange={(e) => setCwd(e.target.value)} />
      </label>
      {error && <p className="fs-loop__why" data-tone="bad" role="alert">{error}</p>}
      <div><Button variant="primary" icon={Ruler} label={t('Measure and save')} loading={busy} disabled={!goal.trim() || !command.trim() || (kind !== 'time' && !detail.trim())}
        onClick={() => { void submit(); }} testId="loop-create" /></div>
    </section>
  );
}

function Card({ card, onChanged }: { card: api.LoopCardRow; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [fresh, setFresh] = useState<api.CheckResult | null>(null);
  const b = card.baseline;
  const last = fresh ? { verdict: fresh.verdict, delta: fresh.delta, delta_pct: fresh.delta_pct, noise_multiple: fresh.noise_multiple, at: fresh.at } : card.last_check;
  const measure = async () => {
    setBusy(true); setError('');
    try { setFresh(await api.checkCard(card.id)); onChanged(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  return (
    <li className="fs-loop__card" data-testid={`loop-card-${card.id}`}>
      <div className="fs-loop__head">
        <div className="fs-loop__titles">
          <span className="fs-loop__goal">{card.goal}</span>
          <code className="fs-loop__cmd">{card.command}</code>
          <span className="fs-loop__muted">{api.metricLabel(card.extract)} · {card.direction === 'up' ? t('higher is better') : t('lower is better')} · {tn(card.repeats, '{n} run', '{n} runs')} · {card.created.replace('T', ' ')}</span>
        </div>
        <button type="button" className="fs-loop__del" aria-label={t('Delete {name}', { name: card.goal })} title={t('Delete')} data-testid={`loop-delete-${card.id}`}
          onClick={async () => {
            if (!window.confirm(t('Delete the loop card "{name}" with its history? The project is not touched.', { name: card.goal }))) return;
            try { await api.deleteCard(card.id); onChanged(); } catch (e) { setError((e as Error).message); }
          }}><Trash2 size={14} aria-hidden="true" /></button>
      </div>
      <dl className="fs-loop__stats">
        <div><dt>{t('Baseline')}</dt><dd data-testid="loop-mean">{api.fmt(b.mean)}</dd></div>
        <div><dt>{t('Noise (std. dev.)')}</dt><dd data-testid="loop-sd">{api.fmt(b.stdev)}{b.cv !== null ? ` · ${api.fmt(b.cv * 100, 3)} %` : ''}</dd></div>
        <div><dt>{t('Range')}</dt><dd>{api.fmt(b.min)} – {api.fmt(b.max)}</dd></div>
        <div><dt>{t('Counts as better from')}</dt><dd data-testid="loop-threshold">± {api.fmt(b.threshold)}</dd></div>
        <div><dt>{t('Last check')}</dt><dd data-testid="loop-last">{last
          ? <><Pill verdict={last.verdict} /> <span className="fs-loop__muted">{last.delta >= 0 ? '+' : ''}{api.fmt(last.delta)}{last.delta_pct !== null && last.delta_pct !== undefined ? ` (${last.delta_pct >= 0 ? '+' : ''}${api.fmt(last.delta_pct, 3)} %)` : ''}{last.noise_multiple !== null && last.noise_multiple !== undefined ? ` · ${api.fmt(last.noise_multiple, 3)} σ` : ''}</span></>
          : <span className="fs-loop__muted">{t('Not checked yet')}</span>}</dd></div>
      </dl>
      {card.noisy && <p className="fs-loop__why" data-tone="warn">{t('This metric is noisy: only large changes will count. Make the measurement steadier before trusting small wins.')}</p>}
      {fresh?.notes.map((n, i) => <p key={i} className="fs-loop__why" data-tone="warn">{n}</p>)}
      {error && <p className="fs-loop__why" data-tone="bad" role="alert">{error}</p>}
      <div><Button variant="secondary" size="sm" icon={Repeat} label={t('Measure again')} loading={busy} onClick={() => { void measure(); }} testId={`loop-check-${card.id}`} /></div>
    </li>
  );
}

export function LoopCardsScreen() {
  const [params] = useSearchParams();
  const project = params.get('project');
  const [rows, setRows] = useState<api.LoopCardRow[] | null>(null);
  const [error, setError] = useState('');
  const [creating, setCreating] = useState(false);
  const reload = useCallback(async () => {
    try { setRows(await api.listCards(project)); setError(''); } catch (e) { setError((e as Error).message); setRows((cur) => cur ?? []); }
  }, [project]);
  useEffect(() => { void reload(); }, [reload]);
  return (
    <div className="fs-screen fs-loop" data-testid="loops">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Loop cards')}</h1>
          <p className="fs-prose fs-loop__lede">{t('Before improving something, find the loop: the number that says whether it got better, how much it moves by itself, and the smallest change that counts. A later measurement is only a win or a loss when it passes that threshold; anything else is noise.')}</p>
        </div>
        <Button variant="primary" icon={Plus} label={t('New loop card')} testId="loop-new" onClick={() => setCreating((v) => !v)} />
      </header>
      {creating && <NewCard project={project} onCreated={() => { setCreating(false); void reload(); }} />}
      {error && <p className="fs-loop__why" data-tone="bad" role="alert">{error}</p>}
      {rows === null && <Skeleton label={t('Reading the loop cards')} count={3} height="96px" />}
      {rows && rows.length === 0 && !creating && <EmptyState icon={Repeat} title={t('No loop cards yet')} body={t('Create one with the command that measures what you want to improve.')} />}
      {rows && rows.length > 0 && <ul className="fs-loop__list" aria-label={t('Loop cards')}>{rows.map((c) => <Card key={c.id} card={c} onChanged={() => { void reload(); }} />)}</ul>}
    </div>
  );
}
