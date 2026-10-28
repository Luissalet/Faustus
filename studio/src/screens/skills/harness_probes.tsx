import { useCallback, useEffect, useState } from 'react';
import { Skeleton } from '../../components';
import {
  getLessons,
  getProbe,
  listLessonDomains,
  listProbes,
  type HarnessLesson,
  type LessonDomain,
  type ProbeCandidate,
  type ProbeCitation,
  type ProbeRecord,
  type ProbeSummary,
} from '../../adapters/harness_proposals';
import { t, tn } from '../../i18n';
import '../settings.css';

/**
 * The probed rounds behind the harness proposals (`src/harness_refinement/probe.py`)
 * and the lessons they leave (`EXPERIENCE.md`). A round repeats the task read-only
 * with the harness as it is and with each candidate edit, an aggregator accepts an
 * edit only citing a moment of a trajectory, a blind selection compares the
 * answers, and the winner is applied by itself (or waits, with auto-apply off).
 */

function lineLabel(line: string): string {
  return line === 'inv' ? t('How it works') : t('What it knows');
}

function statusLabel(status: string): string {
  if (status === 'running') return t('Running');
  if (status === 'done') return t('Finished');
  if (status === 'skipped') return t('Skipped');
  if (status === 'aborted') return t('Cut off');
  return t('Failed');
}

function phaseLabel(phase: string | undefined): string {
  if (phase === 'proposing') return t('Proposing');
  if (phase === 'probing') return t('Repeating the task');
  if (phase === 'composing') return t('Comparing the repeats');
  if (phase === 'selecting') return t('Choosing blind');
  return '';
}

function resultLabel(result: string | null | undefined): string {
  if (result === 'adopted') return t('Won');
  if (result === 'pending') return t('Won, waiting for approval');
  if (result === 'discarded') return t('Discarded');
  if (result === 'invalid') return t('Invalid');
  if (result === 'none') return t('Nothing to change');
  if (result === 'error') return t('Failed');
  return '';
}

function reasonLabel(code: string | null | undefined): string {
  switch (code) {
    case 'keep_base': return t('The unmodified setup was kept.');
    case 'not_accepted': return t('The comparison did not accept it.');
    case 'failure_not_repeated': return t('The failure it cites did not repeat across runs (noise).');
    case 'no_valid_citation': return t('It cited no moment of a run that exists.');
    case 'candidate_run_delivered_nothing': return t('Its repeat delivered nothing.');
    case 'no_decision': return t('No decision was given.');
    case 'compose_unparseable': return t('The comparison could not be read.');
    default: return '';
  }
}

function when(ts: number | null | undefined): string {
  if (!ts) return '';
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch {
    return '';
  }
}

function Citations({ items }: { items: ProbeCitation[] }) {
  if (!items.length) return null;
  return (
    <ul className="fs-hp__cites">
      {items.map((c, i) => (
        <li key={i}>
          <code>{`${c.run} · ${t('step {n}', { n: c.step })}`}</code> {c.quote ? <q>{c.quote}</q> : null}
        </li>
      ))}
    </ul>
  );
}

function CandidateRow({ c }: { c: ProbeCandidate }) {
  return (
    <li className="fs-hp__cand" data-result={c.result ?? ''} data-testid={`probe-candidate-${c.id}`}>
      <span className="fs-chip" data-on>{lineLabel(c.line)}</span>
      {c.target && <code className="fs-hp__target">{c.target}</code>}
      <span className="fs-hp__status" data-status={c.result === 'adopted' ? 'applied' : 'pending'}>{resultLabel(c.result)}</span>
      {c.reason && <span className="fs-set__help">{reasonLabel(c.rejected_because) || c.reason}</span>}
    </li>
  );
}

function ProbeDetail({ id }: { id: string }) {
  const [record, setRecord] = useState<ProbeRecord | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let live = true;
    getProbe(id).then((r) => live && setRecord(r.probe)).catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [id]);

  if (error) return <p className="fs-set__help" data-tone="warn">{error}</p>;
  if (!record) return <Skeleton label={t('Loading')} count={1} height="48px" />;
  return (
    <div className="fs-hp__detail" data-testid={`probe-detail-${id}`}>
      {record.task && (
        <p className="fs-set__help">
          {t('Task')}: <q>{record.task.text}</q>
          {record.task.followup ? <> · {t('The person then said')}: <q>{record.task.followup}</q></> : null}
        </p>
      )}
      <table className="fs-hp__runs">
        <thead>
          <tr><th>{t('Repetition')}</th><th>{t('Setup')}</th><th>{t('Delivered')}</th><th>{t('Steps')}</th><th>{t('Seconds')}</th></tr>
        </thead>
        <tbody>
          {record.rollouts.map((r) => (
            <tr key={r.id} data-testid={`probe-run-${r.id}`}>
              <td><code>{r.id}</code></td>
              <td>{r.arm === 'base' ? t('The setup unchanged') : `${t('With edit {id}', { id: r.candidate ?? '' })}${record.candidates.find((c) => c.id === r.candidate) ? ` (${record.candidates.find((c) => c.id === r.candidate)?.target})` : ''}`}</td>
              <td>{r.delivered ? t('Yes') : t('No')}{r.error ? ` (${r.error})` : ''}</td>
              <td>{r.steps.length}</td>
              <td>{r.seconds}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {record.compose && record.compose.verdicts.map((v) => (
        <div key={v.candidate} className="fs-hp__verdict" data-testid={`probe-verdict-${v.candidate}`}>
          <strong>{v.candidate}</strong>{' '}
          {v.accepted ? t('accepted by the comparison') : t('not accepted by the comparison')}
          {v.rejected_because ? ` · ${reasonLabel(v.rejected_because)}` : ''}
          {v.reason ? <span className="fs-set__help"> {v.reason}</span> : null}
          <Citations items={v.evidence} />
        </div>
      ))}
      {record.selection && (
        <p className="fs-set__help" data-testid="probe-selection">
          {t('Blind choice')}: {record.selection.winner === 'base' || !record.selection.winner ? t('the unmodified setup') : record.selection.winner}
          {record.selection.why ? ` · ${record.selection.why}` : ''}
          {record.selection.seed ? ` · ${t('seed')} ${record.selection.seed}` : ''}
        </p>
      )}
      {record.experience?.status && (
        <p className="fs-set__help" data-testid="probe-experience">
          {t('Lesson')}: {record.experience.status === 'written' ? t('written') : record.experience.status === 'no_entry' ? t('none (no real contrast)') : record.experience.status}
          {record.experience.reason && record.experience.status !== 'written' ? ` · ${record.experience.reason}` : ''}
        </p>
      )}
    </div>
  );
}

export function ProbesCard({ refreshKey }: { refreshKey: number }) {
  const [items, setItems] = useState<ProbeSummary[] | null>(null);
  const [open, setOpen] = useState('');
  const [error, setError] = useState('');

  const reload = useCallback(async () => {
    try {
      setItems(await listProbes());
      setError('');
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload, refreshKey]);

  const running = (items ?? []).some((p) => p.status === 'running');
  useEffect(() => {
    if (!running) return undefined;
    const timer = window.setInterval(() => void reload(), 3000);
    return () => window.clearInterval(timer);
  }, [running, reload]);

  return (
    <div className="fs-set__card" data-testid="harness-probes">
      <h3 className="fs-set__card-title">{t('Tried before kept')}</h3>
      <p className="fs-set__help">
        {t('Each round repeats the task read-only with the setup as it is and with each candidate edit. An edit is kept only if the repeats show a repeated failure it fixes and a blind comparison prefers its answer.')}
      </p>
      {error && <p className="fs-set__help" data-tone="warn">{error}</p>}
      {items === null ? (
        <Skeleton label={t('Loading')} count={2} height="48px" />
      ) : items.length === 0 ? (
        <p className="fs-set__help">{t('No rounds yet.')}</p>
      ) : (
        <ul className="fs-hp__list">
          {items.map((p) => (
            <li key={p.id} className="fs-hp__card" data-testid={`probe-${p.id}`} data-status={p.status}>
              <div className="fs-hp__head">
                <span className="fs-chip" data-on={p.status === 'running'}>{statusLabel(p.status)}</span>
                {p.status === 'running' && p.phase && <span className="fs-set__help">{phaseLabel(p.phase)}</span>}
                <span className="fs-hp__status" data-status={p.applied ? 'applied' : p.winner && p.winner !== 'base' ? 'pending' : ''}>
                  {p.status !== 'done' ? '' : p.winner && p.winner !== 'base'
                    ? (p.applied ? t('Applied by itself') : t('Waiting for approval'))
                    : t('Setup kept as it was')}
                </span>
              </div>
              {p.task && <p className="fs-hp__why">{p.task}</p>}
              {p.candidates.length > 0 && <ul className="fs-hp__cands">{p.candidates.map((c) => <CandidateRow key={c.id} c={c} />)}</ul>}
              {p.reason && p.status !== 'done' && <p className="fs-set__help">{p.reason}</p>}
              <div className="fs-hp__foot">
                <span className="fs-set__help">{when(p.created_at)}{p.k ? ` · ${tn(p.k, '{n} repeat of the unmodified setup', '{n} repeats of the unmodified setup')}` : ''}</span>
                <button type="button" className="fs-link" onClick={() => setOpen(open === p.id ? '' : p.id)} data-testid={`probe-open-${p.id}`}>
                  {open === p.id ? t('Hide the evidence') : t('Show the evidence')}
                </button>
              </div>
              {open === p.id && <ProbeDetail id={p.id} />}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function LessonsCard({ refreshKey }: { refreshKey: number }) {
  const [domains, setDomains] = useState<LessonDomain[] | null>(null);
  const [open, setOpen] = useState('');
  const [entries, setEntries] = useState<HarnessLesson[]>([]);

  useEffect(() => {
    listLessonDomains().then(setDomains).catch(() => setDomains([]));
  }, [refreshKey]);

  const toggle = async (key: string) => {
    if (open === key) {
      setOpen('');
      return;
    }
    setOpen(key);
    try {
      setEntries(await getLessons(key));
    } catch {
      setEntries([]);
    }
  };

  return (
    <div className="fs-set__card" data-testid="harness-lessons">
      <h3 className="fs-set__card-title">{t('Lessons')}</h3>
      <p className="fs-set__help">{t('What earlier rounds taught, per project. The proposer reads them before it proposes, as hypotheses to check.')}</p>
      {domains === null ? (
        <Skeleton label={t('Loading')} count={1} height="40px" />
      ) : domains.length === 0 ? (
        <p className="fs-set__help">{t('No lessons yet.')}</p>
      ) : (
        <ul className="fs-hp__list">
          {domains.map((d) => (
            <li key={d.key} className="fs-hp__card">
              <div className="fs-hp__head">
                <strong>{d.name}</strong>
                <span className="fs-set__help">{tn(d.entries, '{n} lesson', '{n} lessons')}</span>
                <button type="button" className="fs-link" onClick={() => void toggle(d.key)} data-testid={`lessons-open-${d.key}`}>
                  {open === d.key ? t('Hide') : t('Show')}
                </button>
              </div>
              {open === d.key && (
                <ul className="fs-hp__cites">
                  {entries.map((e, i) => (
                    <li key={i}>
                      <strong>{e.cue}</strong> — {e.lesson}
                      <span className="fs-set__help"> {e.evidence} · {e.confidence} · {e.verdict}</span>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
