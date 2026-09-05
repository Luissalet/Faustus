import { AlertTriangle, ArrowLeft, Check, Copy, Download, FlaskConical, GraduationCap, Pencil, Play, Plus, RotateCcw, Save, Trash2, X, Zap } from 'lucide-react';
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { Button } from '../../components';
import { relativeTime } from '../../adapters/home';
import {
  addSkill,
  decideTestApproval,
  draftMarkdown,
  importSkillFromUrl,
  necessityKind,
  saveSkillMarkdown,
  shortModel,
  skillMarkdown,
  startTest,
  testStatus,
  type DuplicateInfo,
  type Skill,
  type TestJob,
} from '../../adapters/skills';
import { t, tn } from '../../i18n';

/**
 * The right pane of Skills: one skill's overview, its SKILL.md (read and
 * edit in place) and its test run; or, in "new" mode, the form to write
 * one by hand and the field to import one from a URL.
 */

export type Tab = 'overview' | 'markdown' | 'test';

const VERDICT_LABEL: Record<string, string> = {
  pass: 'Pass',
  needs_work: 'Needs work',
  fail: 'Fail',
  inconclusive: 'Inconclusive',
  unknown: 'Unclear',
};

export function verdictTone(v: string): 'success' | 'warning' | 'danger' | 'muted' {
  if (v === 'pass') return 'success';
  if (v === 'needs_work') return 'warning';
  if (v === 'fail') return 'danger';
  return 'muted';
}

export function confidenceTone(pct: number): 'success' | 'warning' | 'danger' {
  if (pct >= 90) return 'success';
  if (pct >= 75) return 'warning';
  return 'danger';
}

function Meta({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="fs-sk__meta">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

/* ── SKILL.md: read, edit, save ── */

function MarkdownPane({ skill, say, onSaved }: { skill: Skill; say: (msg: string) => void; onSaved: () => void }) {
  const [md, setMd] = useState<string | null | undefined>(undefined);
  const [draft, setDraft] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setMd(undefined);
    setDraft(null);
    setError(null);
    skillMarkdown(skill.name)
      .then((text) => live && setMd(text))
      .catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [skill.name]);

  const save = async () => {
    if (draft === null) return;
    setSaving(true);
    try {
      await saveSkillMarkdown(skill.name, draft);
      setMd(draft);
      setDraft(null);
      say(t('SKILL.md saved'));
      onSaved();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  if (error) return <p className="fs-sk__error">{error}</p>;
  if (md === undefined) return <p className="fs-sk__hint">{t('Loading…')}</p>;

  // An entry from before SKILL.md files: nothing to read, but writing one
  // gives it a source (the server keeps the name and re-parses the rest).
  if (md === null && draft === null) {
    return (
      <div className="fs-sk__test-empty">
        <p className="fs-prose">{t('This skill has no SKILL.md yet — it predates the files. Its fields are in the overview; write the file to give it a source you can edit and test.')}</p>
        <Button variant="secondary" size="sm" icon={Pencil} label={t('Write SKILL.md from its fields')} onClick={() => setDraft(draftMarkdown(skill))} testId="skill-md-create" />
      </div>
    );
  }

  if (draft !== null) {
    return (
      <div className="fs-sk__editor">
        <textarea className="fs-sk__textarea" value={draft} onChange={(e) => setDraft(e.target.value)} spellCheck={false} aria-label="SKILL.md" data-testid="skill-md-editor" />
        <div className="fs-sk__row">
          <Button variant="primary" size="sm" icon={Save} label={t('Save')} loading={saving} onClick={() => void save()} testId="skill-md-save" />
          <Button variant="ghost" size="sm" icon={X} label={t('Cancel')} onClick={() => setDraft(null)} />
        </div>
      </div>
    );
  }

  const text = md ?? '';
  return (
    <div className="fs-sk__editor">
      <pre className="fs-sk__pre" data-testid="skill-md">
        {text || t('(empty)')}
      </pre>
      <div className="fs-sk__row">
        <Button variant="secondary" size="sm" icon={Pencil} label={t('Edit SKILL.md')} onClick={() => setDraft(text)} testId="skill-md-edit" />
        <Button
          variant="ghost"
          size="sm"
          icon={Download}
          label={t('Download')}
          onClick={() => {
            const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown' }));
            const a = document.createElement('a');
            a.href = url;
            a.download = `${skill.name}.md`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            window.setTimeout(() => URL.revokeObjectURL(url), 1000);
          }}
        />
      </div>
    </div>
  );
}

/* ── Test run: start, follow, decide approvals, read the verdict ── */

function lineOf(ev: TestJob['log'][number]): { text: string; kind: string } | null {
  switch (ev.type) {
    case 'skill_test_start':
      return { text: `${t('Task')}: ${ev.task ?? ''}\n${t('Model')}: ${ev.model ?? ''}`, kind: 'task' };
    case 'agent_step':
      return { text: t('round {n}', { n: ev.round ?? 0 }), kind: 'round' };
    case 'tool_start':
      return { text: `${ev.tool ?? ''}  ${(ev.command ?? '').slice(0, 200)}`, kind: 'tool' };
    case 'tool_output':
      return { text: (ev.output ?? '').slice(0, 500), kind: 'out' };
    case 'approval_granted':
    case 'approval_denied':
      return { text: ev.text ?? '', kind: 'meta' };
    case 'say':
      return { text: ev.text ?? '', kind: 'say' };
    case 'evaluating':
      return { text: t('Evaluating the run…'), kind: 'meta' };
    case 'error':
      return { text: `${t('Error')}: ${ev.error || t('run failed')}`, kind: 'err' };
    default:
      return null;
  }
}

function TestPane({ skill, published, onPublish, onEdit, onDelete, say }: { skill: Skill; published: boolean; onPublish: () => void; onEdit: () => void; onDelete: () => void; say: (msg: string) => void }) {
  const [job, setJob] = useState<TestJob | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const logRef = useRef<HTMLDivElement>(null);

  const refresh = useCallback(async () => {
    const next = await testStatus(skill.name);
    setJob(next);
    return next;
  }, [skill.name]);

  useEffect(() => {
    setJob(null);
    setError(null);
    void refresh();
  }, [refresh]);

  // The run lives on the server; we only look at it every so often.
  useEffect(() => {
    if (job?.status !== 'running') return;
    const id = window.setInterval(() => void refresh(), 1300);
    return () => window.clearInterval(id);
  }, [job?.status, refresh]);

  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [job?.log.length]);

  const run = async () => {
    setStarting(true);
    setError(null);
    try {
      await startTest(skill.name);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setStarting(false);
    }
  };

  const decide = async (decision: 'approve' | 'deny') => {
    if (!job?.approval) return;
    try {
      await decideTestApproval(skill.name, job.approval.approvalId, decision);
      await refresh();
    } catch (e) {
      setError(`${t('The approval failed')}: ${(e as Error).message}`);
    }
  };

  const copyAll = async () => {
    const v = job?.verdict;
    const log = (job?.log ?? []).map(lineOf).filter(Boolean).map((l) => l!.text).join('\n');
    const conf = v?.confidence != null ? ` (${Math.round(v.confidence * 100)}%)` : '';
    const issues = v?.issues.length ? `\n${t('Issues')}:\n- ${v.issues.join('\n- ')}` : '';
    const text = `${log}\n\n=== ${t('Verdict')}: ${v ? t(VERDICT_LABEL[v.verdict]) : ''}${conf} ===\n${v?.summary ?? ''}${issues}`;
    try {
      await navigator.clipboard.writeText(text);
      say(t('Copied'));
    } catch {
      say(t('The browser refused the clipboard — select the result and copy it by hand.'));
    }
  };

  if (job === null) return <p className="fs-sk__hint">{t('Loading…')}</p>;

  if (job.status === 'none') {
    return (
      <div className="fs-sk__test-empty">
        <FlaskConical size={22} aria-hidden="true" />
        <p className="fs-prose">{t('Runs the skill on a task of its own, with the agent, and grades the result. Actions that touch the world stop and ask you first.')}</p>
        {error && <p className="fs-sk__error">{error}</p>}
        <Button variant="primary" size="sm" icon={Play} label={t('Run a test')} loading={starting} onClick={() => void run()} testId="skill-test-run" />
      </div>
    );
  }

  const v = job.verdict;
  const lines = job.log.map(lineOf).filter((l): l is { text: string; kind: string } => l !== null);

  return (
    <div className="fs-sk__test" data-status={job.status}>
      <div className="fs-sk__log" ref={logRef} data-testid="skill-test-log">
        {lines.map((l, i) => (
          <div key={i} className="fs-sk__line" data-kind={l.kind}>
            {l.text}
          </div>
        ))}
        {job.status === 'running' && <div className="fs-sk__line" data-kind="running">{t('Running — you can leave; it keeps going.')}</div>}
      </div>

      {job.status === 'awaiting_approval' && job.approval && (
        <div className="fs-sk__approval" role="group" aria-label={t('Approval')}>
          <p className="fs-sk__approval-q">{job.approval.question || t('Allow this exact action once?')}</p>
          {job.approval.action && (
            <pre className="fs-sk__pre fs-sk__pre--sm">
              {[
                job.approval.action.tool || 'tool',
                job.approval.action.content,
                job.approval.action.effects.length ? `${t('Effects')}: ${job.approval.action.effects.join(', ')}` : '',
                job.approval.action.workspace ? `${t('Workspace')}: ${job.approval.action.workspace}` : '',
                job.approval.action.digest ? `${t('Fingerprint')}: ${job.approval.action.digest}` : '',
              ]
                .filter(Boolean)
                .join('\n')}
            </pre>
          )}
          <div className="fs-sk__row">
            <Button variant="primary" size="sm" icon={Check} label={t('Allow once')} onClick={() => void decide('approve')} testId="skill-test-allow" />
            <Button variant="ghost" size="sm" icon={X} label={t('Deny')} onClick={() => void decide('deny')} />
          </div>
        </div>
      )}

      {error && <p className="fs-sk__error">{error}</p>}

      {job.status === 'done' && v && (
        <div className="fs-sk__verdict" data-tone={verdictTone(v.verdict)} data-testid="skill-verdict">
          <div className="fs-sk__verdict-head">
            <span className="fs-sk__verdict-badge">
              {t(VERDICT_LABEL[v.verdict])}
              {v.confidence != null && <span className="fs-sk__verdict-conf">{Math.round(v.confidence * 100)}%</span>}
            </span>
            {v.summary && <span className="fs-sk__verdict-summary">{v.summary}</span>}
          </div>
          {v.issues.length > 0 && (
            <ul className="fs-sk__issues">
              {v.issues.map((issue, i) => (
                <li key={i}>{issue}</li>
              ))}
            </ul>
          )}
          <div className="fs-sk__row fs-sk__verdict-actions">
            <Button
              variant={published ? 'secondary' : v.verdict === 'pass' ? 'primary' : 'secondary'}
              size="sm"
              icon={Check}
              label={published ? t('Approved — unpublish') : t('Approve')}
              title={published ? t('Back to draft') : t('Publish: it joins the index the agent reads')}
              onClick={onPublish}
              testId="skill-verdict-approve"
            />
            <Button variant="ghost" size="sm" icon={RotateCcw} label={t('Retry')} loading={starting} onClick={() => void run()} />
            <Button variant="ghost" size="sm" icon={Copy} label={t('Copy')} onClick={() => void copyAll()} />
            <Button variant="ghost" size="sm" icon={Pencil} label={t('Edit')} onClick={onEdit} />
            <Button variant="danger" size="sm" icon={Trash2} label={t('Delete')} onClick={onDelete} />
          </div>
        </div>
      )}
    </div>
  );
}

/* ── The detail pane ── */

export interface DetailProps {
  skill: Skill;
  dup: DuplicateInfo | undefined;
  tab: Tab;
  onTab: (tab: Tab) => void;
  onPublish: () => void;
  onAudit: () => void;
  onDelete: () => void;
  /** The list re-reads the index (a saved SKILL.md changes the fields shown). */
  onChanged: () => void;
  /** Narrow screens show the list or the pane, not both; this goes back. */
  onBack: () => void;
  say: (msg: string) => void;
  busy: boolean;
}

export function SkillDetail({ skill, dup, tab, onTab, onPublish, onAudit, onDelete, onChanged, onBack, say, busy }: DetailProps) {
  const published = skill.status === 'published';
  const kind = necessityKind(skill, dup);
  const tabs: { key: Tab; label: string }[] = [
    { key: 'overview', label: t('Overview') },
    { key: 'markdown', label: 'SKILL.md' },
    { key: 'test', label: t('Test') },
  ];

  return (
    <section className="fs-sk__detail" aria-labelledby="fs-sk-detail-title" data-testid="skill-detail">
      <div className="fs-sk__back">
        <Button variant="ghost" size="sm" icon={ArrowLeft} label={t('All skills')} onClick={onBack} />
      </div>
      <header className="fs-sk__detail-head">
        <div className="fs-sk__detail-title">
          <h2 id="fs-sk-detail-title">
            <code>{skill.name}</code>
          </h2>
          {skill.description && <p className="fs-sk__desc">{skill.description}</p>}
        </div>
        <div className="fs-sk__row">
          <Button
            variant={published ? 'secondary' : 'primary'}
            size="sm"
            icon={Check}
            label={published ? t('Unpublish') : t('Publish')}
            title={published ? t('Back to draft') : t('Publish: it joins the index the agent reads')}
            loading={busy}
            onClick={onPublish}
            testId="skill-publish"
          />
          <Button variant="ghost" size="sm" icon={FlaskConical} label={t('Test')} onClick={() => onTab('test')} />
          <Button variant="ghost" size="sm" icon={Zap} label={t('Audit')} title={t('Test, fix with the teacher if it fails, retry')} onClick={onAudit} />
          <Button variant="danger" size="sm" icon={Trash2} label={t('Delete')} onClick={onDelete} testId="skill-delete" />
        </div>
      </header>

      {(kind || skill.auditVerdict === 'fail' || skill.auditVerdict === 'needs_work') && (
        <p className="fs-sk__attention" role="note">
          <AlertTriangle size={13} aria-hidden="true" />
          {kind === 'duplicate' && dup
            ? t('Duplicate group #{n}: {names}. Worth keeping: {keep}.', { n: dup.group, names: dup.names.join(', '), keep: dup.keepName })
            : kind === 'duplicate'
              ? `${t('Overlaps with another skill')}${skill.necessity?.redundantWith.length ? `: ${skill.necessity.redundantWith.join(', ')}` : ''}`
              : kind === 'trivial'
                ? `${t('Too generic to be worth a saved skill')}${skill.necessity?.reason ? ` — ${skill.necessity.reason}` : ''}`
                : kind === 'irrelevant'
                  ? `${t('Possibly not worth keeping')}${skill.necessity?.reason ? ` — ${skill.necessity.reason}` : ''}`
                  : `${t('The last audit did not pass')}: ${t(VERDICT_LABEL[skill.auditVerdict])}`}
        </p>
      )}

      <div className="fs-tabs" role="tablist" aria-label={t('Skill')}>
        {tabs.map((x) => (
          <button key={x.key} type="button" role="tab" className="fs-tab" aria-selected={tab === x.key} onClick={() => onTab(x.key)} data-testid={`skill-tab-${x.key}`}>
            {x.label}
          </button>
        ))}
      </div>

      {tab === 'overview' && (
        <div className="fs-sk__overview">
          {skill.whenToUse && (
            <div className="fs-sk__block">
              <h3>{t('When to use')}</h3>
              <p>{skill.whenToUse}</p>
            </div>
          )}
          {skill.procedure.length > 0 && (
            <div className="fs-sk__block">
              <h3>{t('Procedure')}</h3>
              <ol className="fs-sk__steps">
                {skill.procedure.map((step, i) => (
                  <li key={i}>{step}</li>
                ))}
              </ol>
            </div>
          )}
          {skill.pitfalls && (
            <div className="fs-sk__block">
              <h3>{t('Pitfalls')}</h3>
              <p>{skill.pitfalls}</p>
            </div>
          )}
          {skill.verification && (
            <div className="fs-sk__block">
              <h3>{t('Verification')}</h3>
              <p>{skill.verification}</p>
            </div>
          )}
          <dl className="fs-sk__metas">
            <Meta label={t('Status')}>{skill.status === 'published' ? t('Published') : skill.status === 'draft' ? t('Draft') : skill.status}</Meta>
            <Meta label={t('Confidence')}>
              <span data-tone={confidenceTone(skill.confidence)} className="fs-sk__conf">
                {skill.confidence}%
              </span>
            </Meta>
            <Meta label={t('Uses')}>{skill.lastUsed ? `${skill.uses} · ${t('last {when}', { when: relativeTime(skill.lastUsed) })}` : String(skill.uses)}</Meta>
            <Meta label={t('Origin')}>
              {skill.source === 'teacher-escalation' ? (
                <span className="fs-sk__inline">
                  <GraduationCap size={13} aria-hidden="true" /> {t('Written by the teacher')} {skill.teacherModel && `(${shortModel(skill.teacherModel)})`}
                </span>
              ) : skill.source === 'learned' ? (
                t('Learned from a conversation')
              ) : (
                t('Written by hand')
              )}
            </Meta>
            {(skill.auditVerdict || skill.auditWorkerModel) && (
              <Meta label={t('Last audit')}>
                <span className="fs-sk__inline" data-tone={verdictTone(skill.auditVerdict)}>
                  {skill.auditVerdict === 'pass' && <Check size={13} aria-hidden="true" />}
                  {t(VERDICT_LABEL[skill.auditVerdict] ?? 'Unclear')}
                  {skill.auditWorkerModel && ` · ${shortModel(skill.auditWorkerModel)}`}
                  {skill.auditedAt ? ` · ${relativeTime(skill.auditedAt)}` : ''}
                </span>
                {skill.auditByTeacher && (
                  <span className="fs-sk__inline fs-sk__teacher">
                    <GraduationCap size={13} aria-hidden="true" /> {t('rewritten by the teacher so it would pass')}
                    {skill.auditTeacherModel && ` (${shortModel(skill.auditTeacherModel)})`}
                  </span>
                )}
              </Meta>
            )}
            {skill.category && <Meta label={t('Category')}>{skill.category}</Meta>}
            {skill.tags.length > 0 && (
              <Meta label={t('Tags')}>
                <span className="fs-sk__tags">
                  {skill.tags.map((tag) => (
                    <span key={tag} className="fs-sk__tag">
                      {tag}
                    </span>
                  ))}
                </span>
              </Meta>
            )}
            {skill.created ? <Meta label={t('Created')}>{relativeTime(skill.created)}{skill.owner ? ` · ${skill.owner}` : ''}</Meta> : null}
            {skill.path && (
              <Meta label={t('File')}>
                <code className="fs-sk__path">{skill.path}</code>
              </Meta>
            )}
          </dl>
        </div>
      )}

      {tab === 'markdown' && <MarkdownPane skill={skill} say={say} onSaved={onChanged} />}

      {tab === 'test' && <TestPane skill={skill} published={published} onPublish={onPublish} onEdit={() => onTab('markdown')} onDelete={onDelete} say={say} />}
    </section>
  );
}

/* ── New skill: by hand, or from a URL ── */

export function NewSkillPane({ onAdded, onClose, say }: { onAdded: (name: string) => void; onClose: () => void; say: (msg: string) => void }) {
  const [title, setTitle] = useState('');
  const [when, setWhen] = useState('');
  const [how, setHow] = useState('');
  const [tags, setTags] = useState('');
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState<'add' | 'import' | null>(null);
  const [error, setError] = useState<string | null>(null);

  const add = async () => {
    if (!title.trim()) return;
    setBusy('add');
    setError(null);
    try {
      await addSkill({ name: title, description: title, whenToUse: when, procedure: how, tags });
      say(t('Skill added as a draft'));
      onAdded(title.trim());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const importUrl = async () => {
    if (!url.trim()) return;
    setBusy('import');
    setError(null);
    try {
      const r = await importSkillFromUrl(url.trim());
      say(tn(r.files, 'Imported {name} ({n} file)', 'Imported {name} ({n} files)', { name: r.name || 'skill' }));
      setUrl('');
      onAdded(r.name);
    } catch (e) {
      setError(`${t('Import failed')}: ${(e as Error).message}`);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="fs-sk__detail fs-sk__new" aria-labelledby="fs-sk-new-title" data-testid="skill-new">
      <header className="fs-sk__detail-head">
        <div className="fs-sk__detail-title">
          <h2 id="fs-sk-new-title">{t('New skill')}</h2>
          <p className="fs-sk__desc">{t('A procedure the agent can follow the next time the same problem shows up. It starts as a draft: test it, then publish.')}</p>
        </div>
        <Button variant="ghost" size="sm" icon={X} label={t('Close')} onClick={onClose} />
      </header>

      <form
        className="fs-sk__form"
        onSubmit={(e) => {
          e.preventDefault();
          void add();
        }}
      >
        <label className="fs-sk__field">
          <span>{t('Title')}</span>
          <input className="fs-field" value={title} onChange={(e) => setTitle(e.target.value)} placeholder={t('short name, e.g. build-vllm-wheel')} required data-testid="skill-new-title" />
        </label>
        <label className="fs-sk__field">
          <span>{t('When to use')}</span>
          <input className="fs-field" value={when} onChange={(e) => setWhen(e.target.value)} placeholder={t('what problem does it solve?')} data-testid="skill-new-when" />
        </label>
        <label className="fs-sk__field">
          <span>{t('How')}</span>
          <textarea className="fs-field fs-sk__textarea fs-sk__textarea--form" value={how} onChange={(e) => setHow(e.target.value)} placeholder={t('the steps, commands or rules to follow — one per line')} rows={5} data-testid="skill-new-how" />
        </label>
        <label className="fs-sk__field">
          <span>{t('Tags')}</span>
          <input className="fs-field" value={tags} onChange={(e) => setTags(e.target.value)} placeholder={t('comma-separated, e.g. python, build, vllm')} />
        </label>
        {error && <p className="fs-sk__error">{error}</p>}
        <div className="fs-sk__row">
          <Button type="submit" variant="primary" size="sm" icon={Plus} label={t('Add as a draft')} loading={busy === 'add'} disabled={!title.trim()} testId="skill-new-add" />
        </div>
      </form>

      <form
        className="fs-sk__import"
        onSubmit={(e) => {
          e.preventDefault();
          void importUrl();
        }}
      >
        <h3>{t('Or import one')}</h3>
        <p className="fs-sk__hint">{t('A GitHub link to a skill folder, or a skills.sh URL.')}</p>
        <div className="fs-sk__row">
          <input className="fs-field fs-sk__grow" type="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://github.com/…/tree/main/skills/…" aria-label={t('Skill URL')} data-testid="skill-import-url" />
          <Button type="submit" variant="secondary" size="sm" icon={Download} label={t('Import')} loading={busy === 'import'} disabled={!url.trim()} testId="skill-import" />
        </div>
      </form>
    </section>
  );
}
