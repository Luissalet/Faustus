import { MessageCircleQuestion, X } from 'lucide-react';
import { useState } from 'react';
import { Button, IconButton } from '../../components';
import { askGraph, type AskResult, type AskStep } from '../../adapters/brain';
import { t, tn } from '../../i18n';

/**
 * Ask the Cerebro a question that needs two or three hops ("in which city
 * does the person who created X live?"). The question is planned as steps
 * bound by aliases, steps with no dependency run together, each one reads the
 * graph edges and the text linked to the entities, and the answer comes with
 * the plan and the evidence of every step (`src/brain/graph_ask.py`).
 */

function opLabel(op: string): string {
  if (op === 'retrieval') return t('Look up');
  if (op === 'math') return t('Calculate');
  if (op === 'deduce') return t('Deduce');
  return t('Answer');
}

function viaLabel(via: string | undefined): string {
  if (via === 'graph') return t('from the graph');
  if (via === 'text') return t('read from the texts');
  if (via === 'arithmetic') return t('calculated');
  if (via === 'model') return t('worked out');
  return t('nothing found');
}

function statusText(result: AskResult): string {
  if (result.status === 'no_model') return t('No model is configured for questions to the Cerebro.');
  if (result.status === 'empty') return t('The Cerebro has no entities yet. Run an extraction first.');
  if (result.status === 'degraded') return t('No usable plan was made, so this answer comes from a plain search of the texts.');
  if (result.status === 'error') return result.error || t('The question could not be answered.');
  return '';
}

function StepCard({ step, plan }: { step: AskStep; plan?: string }) {
  return (
    <li className="fs-brain__ask-step" data-testid={`brain-ask-step-${step.n}`} data-via={step.via ?? ''}>
      <div className="fs-brain__ask-step-head">
        <span className="fs-chip" data-on>{opLabel(step.op)}</span>
        <span className="fs-muted">{t('step {n}', { n: step.n })} · {t('level {n}', { n: step.level + 1 })}</span>
        {step.op !== 'output' && <span className="fs-muted">{viaLabel(step.via)}</span>}
      </div>
      <p>{step.rewritten || step.question}</p>
      {plan && <code className="fs-brain__ask-plan">{plan}</code>}
      {step.values.length > 0 && <p><strong>{step.values.join(', ')}</strong></p>}
      {step.error && <p className="fs-muted" data-tone="warn">{step.error}</p>}
      {step.near && step.near.length > 0 && (
        <p className="fs-muted">{t('What the graph does hold there')}: {step.near.map((n) => `${n.s} ${n.p} ${n.o}`).join(' · ')}</p>
      )}
      {((step.triples?.length ?? 0) > 0 || (step.fragments?.length ?? 0) > 0) && (
        <details>
          <summary>{tn((step.triples?.length ?? 0) + (step.fragments?.length ?? 0), '{n} piece of evidence', '{n} pieces of evidence')}</summary>
          <ul className="fs-brain__ask-evidence">
            {(step.triples ?? []).map((tr, i) => (
              <li key={`t${i}`}><code>{`${tr.s} — ${tr.p} → ${tr.o}`}</code> <span className="fs-muted">{tr.evidence.join(', ')}</span></li>
            ))}
            {(step.fragments ?? []).map((f) => (
              <li key={f.ref}><code>{f.ref}</code> <span className="fs-muted">{f.entities.join(', ')}</span> <q>{f.text}</q></li>
            ))}
          </ul>
        </details>
      )}
    </li>
  );
}

export function AskPanel({ onClose }: { onClose: () => void }) {
  const [question, setQuestion] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<AskResult | null>(null);
  const [error, setError] = useState('');

  async function run(planOnly: boolean) {
    const q = question.trim();
    if (!q) return;
    setBusy(true);
    setError('');
    try {
      setResult(await askGraph(q, { planOnly }));
    } catch (e) {
      setError((e as Error).message);
      setResult(null);
    } finally {
      setBusy(false);
    }
  }

  const planByN = new Map((result?.plan ?? []).map((p) => [p.n, p]));
  const steps: AskStep[] = result && result.steps.length > 0
    ? result.steps
    : (result?.plan ?? []).map((p) => ({ n: p.n, op: p.op, question: p.question, level: p.level, values: [] }));
  const note = result ? statusText(result) : '';

  return (
    <div className="fs-brain__drawer-backdrop" role="presentation">
      <button type="button" className="fs-brain__scrim" aria-label={t('Close')} tabIndex={-1} onClick={onClose} />
      <aside className="fs-brain__drawer fs-brain__drawer--wide" role="dialog" aria-modal="true" aria-label={t('Ask the graph')} onClick={(e) => e.stopPropagation()} data-testid="brain-ask-panel">
        <header className="fs-brain__drawer-head">
          <h3><MessageCircleQuestion size={16} aria-hidden="true" /> {t('Ask the graph')}</h3>
          <IconButton icon={X} label={t('Close')} size="sm" onClick={onClose} />
        </header>
        <p className="fs-muted">{t('For questions that need two or three hops, such as the city where the person who created a project lives. The question is split into steps, each one reads the graph and the texts linked to it, and nothing is written.')}</p>
        <textarea
          className="fs-field"
          rows={3}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder={t('Your question')}
          aria-label={t('Your question')}
          data-testid="brain-ask-input"
          onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) void run(false); }}
        />
        <div className="fs-brain__ask-actions">
          <Button variant="primary" size="sm" label={t('Answer')} loading={busy} disabled={!question.trim()} onClick={() => void run(false)} testId="brain-ask-run" />
          <Button variant="ghost" size="sm" label={t('Only show the plan')} disabled={busy || !question.trim()} onClick={() => void run(true)} testId="brain-ask-plan" />
        </div>
        {error && <p className="fs-notice" data-tone="danger">{error}</p>}
        {result && (
          <div className="fs-brain__drawer-body" data-testid="brain-ask-result" data-status={result.status}>
            {note && <p className="fs-notice" data-tone={result.status === 'degraded' ? 'warn' : 'danger'}>{note}</p>}
            {result.answer && (
              <section>
                <h4>{t('Answer')}</h4>
                <p data-testid="brain-ask-answer">{result.answer}</p>
                {result.values.length > 0 && <p className="fs-muted">{t('Values')}: {result.values.join(', ')}</p>}
              </section>
            )}
            {steps.length > 0 && (
              <section>
                <h4>{t('Plan and steps')}</h4>
                <ol className="fs-brain__ask-steps">
                  {steps.map((s) => {
                    const p = planByN.get(s.n);
                    const line = p && p.op === 'retrieval' ? `Retrieval(s=${p.s}, p=${p.p}, o=${p.o})` : p && (p.op === 'math' || p.op === 'deduce') ? `${p.op === 'math' ? 'Math' : 'Deduce'}(${(p.content ?? []).join(', ')})->${p.alias}` : p ? `Output(${(p.outputs ?? []).join(', ')})` : undefined;
                    return <StepCard key={s.n} step={s} plan={line} />;
                  })}
                </ol>
              </section>
            )}
            {result.sources.length > 0 && (
              <p className="fs-muted">{t('Sources')}: {result.sources.join(', ')}</p>
            )}
            {result.model_calls > 0 && (
              <p className="fs-muted">{tn(result.model_calls, '{n} model call', '{n} model calls')} · {(result.ms / 1000).toFixed(1)} s</p>
            )}
          </div>
        )}
      </aside>
    </div>
  );
}
