import { useCallback, useEffect, useState } from 'react';
import {
  ChevronLeft, FlaskConical, GitMerge, Play, Plus, RefreshCw, ShieldAlert, Trash2, TriangleAlert,
} from 'lucide-react';
import { Button, Dialog, Skeleton, Toast } from '../../components';
import * as api from '../../adapters/alternatives';
import { t } from '../../i18n';

/**
 * CompareView — one experiment, in full: every alternative's diff against
 * `base_ref`, which files more than one alternative touches (`contested_files`
 * — the set `apply`/`combine` will have to three-way merge, or that will
 * collide if combined onto the same file), and the actions that change the
 * main copy: run a command inside an alternative's own isolation, apply ONE
 * alternative whole, or combine per contested file.
 *
 * A conflict from `apply`/`combine` is drawn as its own banner, never a
 * generic toast: it names every file that needs a human, and the main copy
 * is untouched (`src/alternatives.py`'s own guarantee — nothing is written
 * until every touched file's plan is conflict-free).
 */

function FileRow({ path, stat }: { path: string; stat: api.FileDiffStat }) {
  return (
    <li className="fs-alt__file" data-change={stat.change}>
      <span className="fs-alt__file-path fs-alt__mono">{path}</span>
      <span className="fs-alt__file-change">{changeWord(stat.change)}</span>
      {stat.additions !== null && <span className="fs-alt__file-add">+{stat.additions}</span>}
      {stat.deletions !== null && <span className="fs-alt__file-del">-{stat.deletions}</span>}
    </li>
  );
}

function changeWord(change: api.FileDiffStat['change']): string {
  switch (change) {
    case 'added': return t('added');
    case 'deleted': return t('deleted');
    case 'modified': return t('modified');
    default: return t('unchanged');
  }
}

function statusWord(status: api.AltStatus): string {
  switch (status) {
    case 'ready': return t('ready');
    case 'failed': return t('tests failed');
    case 'applied': return t('applied');
    default: return t('pending');
  }
}

function AlternativeCard({
  alt, exp, busy, onRunTests, onApply, onSaveDoc,
}: {
  alt: api.CompareAlternative;
  exp: api.Experiment;
  busy: string;
  onRunTests: (altId: string, command: string) => void;
  onApply: (altId: string) => void;
  onSaveDoc: (altId: string, content: string) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [command, setCommand] = useState('');
  const full = exp.alternatives.find((a) => a.id === alt.id);
  const [docDraft, setDocDraft] = useState(full?.content ?? '');
  const files = Object.entries(alt.files);

  return (
    <div className="fs-alt__card" data-status={alt.status} data-testid={`alt-${alt.id}`}>
      <header className="fs-alt__card-head">
        <b className="fs-alt__grow">{alt.label}</b>
        <span className="fs-alt__tag">{full?.isolation}</span>
        <span className="fs-alt__tag" data-tone={alt.status === 'failed' ? 'bad' : alt.status === 'applied' ? 'good' : 'quiet'}>
          {statusWord(alt.status)}
        </span>
      </header>

      {alt.diff_summary && (
        <p className="fs-alt__muted">
          {t('{n} file(s) changed, +{a}/-{d}', {
            n: alt.diff_summary.files_changed, a: alt.diff_summary.additions, d: alt.diff_summary.deletions,
          })}
        </p>
      )}

      {full?.isolation === 'doc_version' ? (
        <div className="fs-alt__doc">
          <textarea
            className="fs-field fs-alt__textarea"
            rows={6}
            value={docDraft}
            onChange={(event) => setDocDraft(event.target.value)}
            data-testid={`alt-doc-${alt.id}`}
          />
          <Button
            variant="secondary" size="sm" label={t('Save text')}
            onClick={() => onSaveDoc(alt.id, docDraft)}
          />
        </div>
      ) : (
        files.length > 0 && (
          <ul className="fs-alt__files">
            {files.map(([path, stat]) => <FileRow key={path} path={path} stat={stat} />)}
          </ul>
        )
      )}

      {full?.isolation !== 'doc_version' && (
        <div className="fs-alt__row">
          <input
            className="fs-field fs-alt__mono"
            type="text"
            placeholder={t('a command to run inside this alternative')}
            value={command}
            onChange={(event) => setCommand(event.target.value)}
            data-testid={`alt-cmd-${alt.id}`}
          />
          <Button
            variant="secondary" size="sm" icon={Play} label={t('Run')}
            loading={busy === `test-${alt.id}`} disabled={!command.trim()}
            onClick={() => onRunTests(alt.id, command)}
          />
        </div>
      )}

      {alt.tests_result && (
        <div className="fs-alt__result" data-ok={alt.tests_result.ok ? 'yes' : 'no'}>
          <p className="fs-alt__muted fs-alt__mono">
            {alt.tests_result.command} — {t('exit {code}', { code: String(alt.tests_result.exit_code) })}
          </p>
          <pre className="fs-alt__output">{alt.tests_result.output || t('(no output)')}</pre>
        </div>
      )}

      <div className="fs-alt__row">
        <span className="fs-alt__muted">{t('cost: {cost}', { cost: api.costLabel(full?.cost ?? { known_usd: 'unknown', unestimable: [] }) })}</span>
        <span className="fs-spacer" />
        {/* Applying writes into the main copy: two steps, like the board's
            destructive actions, so a stray click on a row never merges
            anything (seen live: one click applied straight away). */}
        {confirming ? (
          <>
            <span className="fs-alt__muted">{t('Merge "{label}" into the main copy?', { label: alt.label })}</span>
            <Button size="sm" label={t('Cancel')} onClick={() => setConfirming(false)} />
            <Button
              variant="primary" size="sm" icon={GitMerge} label={t('Confirm apply')}
              loading={busy === `apply-${alt.id}`}
              onClick={() => { setConfirming(false); onApply(alt.id); }}
              data-testid="alt-apply-confirm"
            />
          </>
        ) : (
          <Button
            variant="primary" size="sm" icon={GitMerge} label={t('Apply')}
            loading={busy === `apply-${alt.id}`}
            title={t('Merge this alternative into the main copy. A local edit made since the experiment started is respected, never overwritten.')}
            onClick={() => setConfirming(true)}
            data-testid="alt-apply"
          />
        )}
      </div>
    </div>
  );
}

export function CompareView({ projectId, expId, onBack, onDeleted }: {
  projectId: string;
  expId: string;
  onBack: () => void;
  onDeleted: () => void;
}) {
  const [data, setData] = useState<api.CompareResult | null>(null);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState<{ text: string; tone: 'ok' | 'warn' } | null>(null);
  const [conflicts, setConflicts] = useState<string[] | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [newLabel, setNewLabel] = useState('');

  const say = useCallback((text: string, tone: 'ok' | 'warn' = 'ok') => {
    setNotice({ text, tone });
    window.setTimeout(() => setNotice(null), 5000);
  }, []);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const result = await api.compareExperiment(projectId, expId, signal);
      setData(result);
    } catch (error) {
      if (signal?.aborted) return;
      say((error as Error).message, 'warn');
    }
  }, [projectId, expId, say]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const addAlternative = useCallback(async () => {
    setBusy('add');
    try {
      await api.addAlternative(projectId, expId, newLabel);
      setAddOpen(false);
      setNewLabel('');
      say(t('Alternative created.'));
      await load();
    } catch (error) {
      say((error as Error).message, 'warn');
    } finally {
      setBusy('');
    }
  }, [projectId, expId, newLabel, say, load]);

  const runTests = useCallback(async (altId: string, command: string) => {
    setBusy(`test-${altId}`);
    try {
      await api.runTests(projectId, expId, altId, command);
      say(t('Ran.'));
      await load();
    } catch (error) {
      say((error as Error).message, 'warn');
    } finally {
      setBusy('');
    }
  }, [projectId, expId, say, load]);

  const saveDoc = useCallback(async (altId: string, content: string) => {
    setBusy(`doc-${altId}`);
    try {
      await api.setDocContent(projectId, expId, altId, content);
      say(t('Saved.'));
      await load();
    } catch (error) {
      say((error as Error).message, 'warn');
    } finally {
      setBusy('');
    }
  }, [projectId, expId, say, load]);

  const apply = useCallback(async (altId: string) => {
    setBusy(`apply-${altId}`);
    setConflicts(null);
    try {
      const result = await api.applyAlternative(projectId, expId, altId);
      say(result.skipped_same
        ? t('Nothing to apply: the main copy already matches.')
        : t('Applied {n} file(s) into the main copy.', { n: result.applied_files.length }));
      await load();
    } catch (error) {
      if (error instanceof api.AlternativesApiError && error.errorClass === 'alternatives.apply_conflict') {
        setConflicts(error.conflicts);
      } else {
        say((error as Error).message, 'warn');
      }
    } finally {
      setBusy('');
    }
  }, [projectId, expId, say, load]);

  const remove = useCallback(async () => {
    setBusy('delete');
    try {
      await api.deleteExperiment(projectId, expId);
      onDeleted();
    } catch (error) {
      say((error as Error).message, 'warn');
    } finally {
      setBusy('');
    }
  }, [projectId, expId, say, onDeleted]);

  if (data === null) {
    return <Skeleton label={t('Reading the experiment')} count={3} height="72px" />;
  }

  const contested = Object.entries(data.contested_files);

  return (
    <div className="fs-alt__detail" data-testid="alt-compare">
      <header className="fs-alt__detail-head">
        <div className="fs-inline">
          <Button variant="ghost" size="sm" icon={ChevronLeft} label={t('All experiments')} onClick={onBack} />
          <span className="fs-spacer" />
          <Button variant="secondary" size="sm" icon={RefreshCw} label={t('Refresh')} onClick={() => void load()} />
          <Button
            variant="secondary" size="sm" icon={Plus} label={t('Add alternative')}
            onClick={() => setAddOpen(true)}
          />
          <Button
            variant="danger" size="sm" icon={Trash2} label={t('Delete experiment')}
            loading={busy === 'delete'}
            onClick={() => void remove()}
          />
        </div>
        <h2 className="fs-alt__goal">{data.experiment.goal}</h2>
        <p className="fs-alt__muted fs-alt__mono">
          {t('base: {ref}', { ref: data.base_ref.slice(0, 12) })}
        </p>
      </header>

      {conflicts && conflicts.length > 0 && (
        <section className="fs-alt__notice" data-tone="danger" role="alert">
          <ShieldAlert size={14} aria-hidden="true" />
          <div>
            <p>{t('This apply needs a human: these files changed on both sides since the experiment started, and nothing was written.')}</p>
            <ul className="fs-alt__gaps">
              {conflicts.map((path) => <li key={path} className="fs-alt__mono">{path}</li>)}
            </ul>
          </div>
        </section>
      )}

      {contested.length > 0 && (
        <section className="fs-alt__notice" data-tone="warning" role="status">
          <TriangleAlert size={14} aria-hidden="true" />
          <div>
            <p>{t('More than one alternative touches the same file(s) — applying one, then another, will need a merge:')}</p>
            <ul className="fs-alt__gaps">
              {contested.map(([path, ids]) => (
                <li key={path} className="fs-alt__mono">{path} — {ids.length} {t('alternative(s)')}</li>
              ))}
            </ul>
          </div>
        </section>
      )}

      {data.alternatives.length === 0 ? (
        <p className="fs-alt__muted">{t('No alternatives yet. Add one to start comparing approaches.')}</p>
      ) : (
        <div className="fs-alt__grid">
          {data.alternatives.map((alt) => (
            <AlternativeCard
              key={alt.id} alt={alt} exp={data.experiment} busy={busy}
              onRunTests={(id, command) => void runTests(id, command)}
              onApply={(id) => void apply(id)}
              onSaveDoc={(id, content) => void saveDoc(id, content)}
            />
          ))}
        </div>
      )}

      {addOpen && (
        <Dialog
          open
          onOpenChange={(isOpen) => !isOpen && setAddOpen(false)}
          title={t('Add alternative')}
          testId="alt-add"
          footer={(
            <Button
              variant="primary" size="sm" icon={FlaskConical} label={t('Create')}
              loading={busy === 'add'}
              onClick={() => void addAlternative()}
            />
          )}
        >
          <label className="fs-alt__row-field">
            <span>{t('Label')}</span>
            <input
              className="fs-field" type="text" autoFocus value={newLabel}
              onChange={(event) => setNewLabel(event.target.value)}
              placeholder={t('e.g. minimal fix')}
            />
          </label>
        </Dialog>
      )}

      {notice && (
        <Toast>
          {notice.text}
        </Toast>
      )}
    </div>
  );
}
