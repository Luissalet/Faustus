import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  ClipboardList,
  FolderInput,
  Gauge,
  History,
  Link2,
  Plus,
  User,
  XCircle,
} from 'lucide-react';
import type { ReactNode } from 'react';
import { useCallback, useEffect, useState } from 'react';
import { Button, EmptyState, Skeleton } from '../../components';
import {
  acceptanceFromLines,
  acceptanceToLines,
  acceptRequirement,
  addLink,
  contextForTask,
  createRequirement,
  getProjectMatrix,
  getRequirement,
  getRequirementMatrix,
  getRevisions,
  getSidecar,
  linesToList,
  LINK_KINDS,
  LINK_STATE_TONE,
  listRequirements,
  rejectRequirement,
  removeLink,
  REQUIREMENT_SOURCES,
  REQUIREMENT_STATUSES,
  RequirementsApiError,
  STATUS_TONE,
  updateRequirement,
  type LinkKind,
  type MatrixRow,
  type Requirement,
  type RequirementRevision,
  type RequirementSource,
  type RequirementStatus,
  type SidecarItem,
  type SidecarResult,
  type TaskContext,
} from '../../adapters/requirements';
import { t, tn } from '../../i18n';

/**
 * W4-A — the Requirements tab: `docs/api/requirements.md`'s versioned
 * spec (proposed/accepted/rejected/superseded, human-only decisions,
 * immutable revisions), its evidence matrix (`docs/api/requirements.md
 * #matriz-de-cobertura`, four independent facts plus `stale`), the
 * budgeted "what does this task need" projection (`POST /context`), and a
 * read-only preview of `.faustus/requirements.yaml` a human can turn into
 * real requirements one at a time.
 */

const STATUS_LABEL: Record<RequirementStatus, string> = {
  proposed: 'Proposed',
  accepted: 'Accepted',
  rejected: 'Rejected',
  superseded: 'Superseded',
};

const SOURCE_LABEL: Record<RequirementSource, string> = {
  doc: 'Document',
  issue: 'Issue',
  url: 'URL',
  human: 'Human',
};

const LINK_KIND_LABEL: Record<LinkKind, string> = {
  implements: 'Implements',
  tests: 'Tests',
  evidences: 'Evidences',
  issue: 'Issue',
};

const LINK_STATE_LABEL: Record<string, string> = {
  linked: 'Linked',
  needs_review: 'Needs review',
  stale: 'Stale',
  unknown: 'Unknown',
};

/** A small pill, coloured by `Tone` — the one place this screen decides a
 *  colour, so every status/link-state badge reads the same way. */
function Badge({ tone, children }: { tone: 'ok' | 'bad' | 'warn' | 'neutral'; children: ReactNode }) {
  return (
    <span className="fs-req__badge" data-tone={tone}>
      {children}
    </span>
  );
}

/** The four independent coverage facts plus `stale` — never folded into one
 *  score (`docs/api/requirements.md#matriz-de-cobertura`). */
function MatrixDots({ row }: { row: Pick<MatrixRow, 'linked' | 'implemented' | 'tested' | 'verified' | 'stale'> }) {
  const cells: { key: string; label: string; on: boolean }[] = [
    { key: 'linked', label: t('Linked'), on: row.linked },
    { key: 'implemented', label: t('Implemented'), on: row.implemented },
    { key: 'tested', label: t('Tested'), on: row.tested },
    { key: 'verified', label: t('Verified'), on: row.verified },
  ];
  return (
    <span className="fs-req__matrix-dots">
      {cells.map((c) => (
        <span key={c.key} className="fs-req__matrix-dot" data-on={c.on || undefined} title={c.label}>
          {c.label[0]}
        </span>
      ))}
      {row.stale && (
        <span className="fs-req__matrix-dot" data-tone="warn" title={t('Stale — some evidence changed since it was recorded')}>
          <AlertTriangle size={11} aria-hidden="true" />
        </span>
      )}
    </span>
  );
}

function ProposedByBadge({ by }: { by: 'human' | 'model' }) {
  return (
    <span className="fs-req__by" data-by={by} title={by === 'model' ? t('Proposed by the model') : t('Proposed by a human')}>
      {by === 'model' ? <Bot size={12} aria-hidden="true" /> : <User size={12} aria-hidden="true" />}
      {by === 'model' ? t('Model') : t('Human')}
    </span>
  );
}

interface DetailProps {
  projectId: string;
  reqKey: string;
  say: (m: string) => void;
  onChanged: () => void;
}

/** The open requirement: full text, acceptance, human-only decision, an
 *  editable patch (creates a new immutable revision), its links (add
 *  only — this adapter's backend has no remove-link route, see the final
 *  report's "limits"), its own evidence row, and its revision history. */
function RequirementDetail({ projectId, reqKey, say, onChanged }: DetailProps) {
  const [item, setItem] = useState<Requirement | null>(null);
  const [matrix, setMatrix] = useState<MatrixRow | null>(null);
  const [revisions, setRevisions] = useState<RequirementRevision[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState('');
  const [editText, setEditText] = useState('');
  const [editAcceptance, setEditAcceptance] = useState('');
  const [editSource, setEditSource] = useState<RequirementSource>('human');
  const [editNote, setEditNote] = useState('');
  const [linkKind, setLinkKind] = useState<LinkKind>('implements');
  const [linkTarget, setLinkTarget] = useState('');
  const [linkRevision, setLinkRevision] = useState('');

  const load = useCallback(() => {
    setItem(null);
    setMatrix(null);
    setRevisions(null);
    getRequirement(projectId, reqKey)
      .then((d) => {
        setItem(d.requirement);
        setEditTitle(d.requirement.title);
        setEditText(d.requirement.text);
        setEditAcceptance(acceptanceToLines(d.requirement.acceptance));
        setEditSource(d.requirement.source);
      })
      .catch((e: Error) => say(e.message));
    getRequirementMatrix(projectId, reqKey)
      .then((d) => setMatrix(d.matrix))
      .catch(() => setMatrix(null));
  }, [projectId, reqKey, say]);

  useEffect(load, [load]);

  const loadRevisions = () => {
    getRevisions(projectId, reqKey)
      .then((d) => setRevisions(d.revisions))
      .catch((e: Error) => say(e.message));
  };

  const decide = async (status: 'accepted' | 'rejected') => {
    setBusy(status);
    try {
      const result = status === 'accepted' ? await acceptRequirement(projectId, reqKey) : await rejectRequirement(projectId, reqKey);
      setItem(result.requirement);
      onChanged();
      say(status === 'accepted' ? t('Accepted.') : t('Rejected.'));
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const saveEdit = async () => {
    if (!item) return;
    setBusy('save');
    try {
      const result = await updateRequirement(projectId, reqKey, {
        title: editTitle.trim() || item.title,
        text: editText,
        source: editSource,
        acceptance: acceptanceFromLines(editAcceptance),
        change_note: editNote.trim(),
      });
      setItem(result.requirement);
      setEditNote('');
      onChanged();
      say(t('Saved — a new revision was recorded.'));
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const link = async () => {
    if (!linkTarget.trim()) return;
    setBusy('link');
    try {
      await addLink(projectId, reqKey, { kind: linkKind, target: linkTarget.trim(), revision: linkRevision.trim() || undefined });
      setLinkTarget('');
      setLinkRevision('');
      load();
      say(t('Link added.'));
    } catch (e) {
      const err = e as Error;
      const detail = err instanceof RequirementsApiError && err.errorClass === 'requirements.path_outside_workspace'
        ? t('That path is outside the project workspace — it was not linked.')
        : err.message;
      say(detail);
    } finally {
      setBusy(null);
    }
  };

  const unlink = async (linkId: string) => {
    setBusy(`unlink:${linkId}`);
    try {
      await removeLink(projectId, reqKey, linkId);
      load();
      say(t('Link removed.'));
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  if (!item) {
    return <Skeleton label={t('Loading the requirement')} count={4} height="20px" />;
  }

  const canDecide = item.status === 'proposed';

  return (
    <div className="fs-req__detail" data-testid="requirement-detail">
      <div className="fs-req__detail-head">
        <div>
          <h3>
            <code>{item.key}</code> {item.title}
          </h3>
          <div className="fs-req__badges">
            <Badge tone={STATUS_TONE[item.status]}>{t(STATUS_LABEL[item.status])}</Badge>
            <ProposedByBadge by={item.proposed_by} />
            <span className="fs-req__muted">{t(SOURCE_LABEL[item.source])}</span>
            <span className="fs-req__muted">{t('revision {n}', { n: item.current_revision })}</span>
          </div>
        </div>
        {canDecide && (
          <div className="fs-req__decide" data-testid="requirement-decide">
            <Button
              variant="secondary"
              size="sm"
              icon={CheckCircle2}
              label={t('Accept')}
              loading={busy === 'accepted'}
              onClick={() => void decide('accepted')}
              testId="requirement-accept"
            />
            <Button
              variant="danger"
              size="sm"
              icon={XCircle}
              label={t('Reject')}
              loading={busy === 'rejected'}
              onClick={() => void decide('rejected')}
              testId="requirement-reject"
            />
          </div>
        )}
      </div>
      {!canDecide && item.status !== 'superseded' && (
        <p className="fs-req__note">
          {item.status === 'accepted' ? t('A human accepted this requirement.') : t('A human rejected this requirement.')}
        </p>
      )}

      <section className="fs-req__section">
        <p className="fs-panel__label">{t('Evidence for this requirement')}</p>
        {matrix ? <MatrixDots row={matrix} /> : <Skeleton label={t('Loading the evidence')} height="20px" />}
      </section>

      <section className="fs-req__section">
        <p className="fs-panel__label">{t('Edit')}</p>
        <label className="fs-field-label">
          {t('Title')}
          <input className="fs-field" value={editTitle} onChange={(e) => setEditTitle(e.target.value)} maxLength={300} />
        </label>
        <label className="fs-field-label">
          {t('Text')}
          <textarea className="fs-field fs-req__textarea" rows={4} value={editText} onChange={(e) => setEditText(e.target.value)} />
        </label>
        <label className="fs-field-label">
          {t('Acceptance criteria, one per line')}
          <textarea className="fs-field fs-req__textarea" rows={3} value={editAcceptance} onChange={(e) => setEditAcceptance(e.target.value)} placeholder={t('One criterion per line')} />
        </label>
        <label className="fs-field-label">
          {t('Source')}
          <select className="fs-field" value={editSource} onChange={(e) => setEditSource(e.target.value as RequirementSource)}>
            {REQUIREMENT_SOURCES.map((s) => (
              <option key={s} value={s}>
                {t(SOURCE_LABEL[s])}
              </option>
            ))}
          </select>
        </label>
        <label className="fs-field-label">
          {t('Change note (optional)')}
          <input className="fs-field" value={editNote} onChange={(e) => setEditNote(e.target.value)} placeholder={t('Why this change')} maxLength={1000} />
        </label>
        <Button variant="secondary" size="sm" label={t('Save changes')} loading={busy === 'save'} onClick={() => void saveEdit()} testId="requirement-save" />
      </section>

      <section className="fs-req__section">
        <p className="fs-panel__label">{t('Links')}</p>
        {item.links.length === 0 ? (
          <p className="fs-req__muted">{t('No links yet.')}</p>
        ) : (
          <ul className="fs-req__links">
            {item.links.map((l) => (
              <li key={l.id} data-testid="requirement-link">
                <Badge tone="neutral">{t(LINK_KIND_LABEL[l.kind])}</Badge>
                <code>{l.target}</code>
                <Badge tone={LINK_STATE_TONE[l.state]}>{t(LINK_STATE_LABEL[l.state] ?? l.state)}</Badge>
                <Button
                  variant="ghost"
                  size="sm"
                  label={t('Remove')}
                  loading={busy === `unlink:${l.id}`}
                  onClick={() => void unlink(l.id)}
                  testId="requirement-link-remove"
                />
              </li>
            ))}
          </ul>
        )}
        <form
          className="fs-req__link-form"
          onSubmit={(e) => {
            e.preventDefault();
            void link();
          }}
        >
          <select className="fs-field" value={linkKind} onChange={(e) => setLinkKind(e.target.value as LinkKind)} aria-label={t('Kind of link')}>
            {LINK_KINDS.map((k) => (
              <option key={k} value={k}>
                {t(LINK_KIND_LABEL[k])}
              </option>
            ))}
          </select>
          <input
            className="fs-field fs-req__grow"
            value={linkTarget}
            onChange={(e) => setLinkTarget(e.target.value)}
            placeholder={linkKind === 'implements' || linkKind === 'tests' ? t('path/to/file.py@symbol') : t('run id or issue key')}
            spellCheck={false}
            data-testid="requirement-link-target"
          />
          <input
            className="fs-field"
            value={linkRevision}
            onChange={(e) => setLinkRevision(e.target.value)}
            placeholder={t('Revision (optional)')}
          />
          <Button type="submit" variant="secondary" size="sm" icon={Link2} label={t('Add link')} loading={busy === 'link'} disabled={!linkTarget.trim()} testId="requirement-add-link" />
        </form>
      </section>

      <section className="fs-req__section">
        <div className="fs-req__section-head">
          <p className="fs-panel__label">{t('Revisions')}</p>
          {revisions === null && <Button variant="ghost" size="sm" icon={History} label={t('Show history')} onClick={loadRevisions} testId="requirement-show-revisions" />}
        </div>
        {revisions !== null && (
          revisions.length === 0 ? (
            <p className="fs-req__muted">{t('No revisions.')}</p>
          ) : (
            <ul className="fs-req__revisions" data-testid="requirement-revisions">
              {revisions
                .slice()
                .reverse()
                .map((r) => (
                  <li key={r.id}>
                    <span className="fs-req__rev-n">{t('rev {n}', { n: r.revision })}</span>
                    <Badge tone={STATUS_TONE[r.status]}>{t(STATUS_LABEL[r.status])}</Badge>
                    <span className="fs-req__muted">{r.changed_by || t('unknown')}</span>
                    {r.change_note && <em className="fs-req__note">{r.change_note}</em>}
                    <time className="fs-req__muted">{r.created_at.replace('T', ' ').slice(0, 16)}</time>
                  </li>
                ))}
            </ul>
          )
        )}
      </section>
    </div>
  );
}

/** `POST /context`: what a task actually gets, budgeted — every requested
 *  id that does not exist in `unknown`, every relevant one that did not fit
 *  the budget in `omitted`, never silently trimmed (`docs/api/
 *  requirements.md#contexto-para-una-tarea`). */
function ContextTool({ projectId, say }: { projectId: string; say: (m: string) => void }) {
  const [files, setFiles] = useState('');
  const [keys, setKeys] = useState('');
  const [budget, setBudget] = useState(4000);
  const [result, setResult] = useState<TaskContext | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    setBusy(true);
    try {
      const data = await contextForTask(projectId, { files: linesToList(files), keys: linesToList(keys), budget_chars: budget });
      setResult(data);
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="fs-panel" data-testid="requirement-context-tool">
      <h3>{t('Context for a task')}</h3>
      <p className="fs-prose">{t('What the agent would actually receive for a task touching these files or ids — budgeted, with every omission and unknown id named explicitly.')}</p>
      <form
        className="fs-req__context-form"
        onSubmit={(e) => {
          e.preventDefault();
          void run();
        }}
      >
        <label className="fs-field-label">
          {t('Files, one per line')}
          <textarea className="fs-field fs-req__textarea" rows={3} value={files} onChange={(e) => setFiles(e.target.value)} placeholder={t('src/example.py')} spellCheck={false} />
        </label>
        <label className="fs-field-label">
          {t('Requirement ids, one per line')}
          <textarea className="fs-field fs-req__textarea" rows={2} value={keys} onChange={(e) => setKeys(e.target.value)} placeholder="REQ-1" spellCheck={false} />
        </label>
        <label className="fs-field-label">
          {t('Character budget')}
          <input className="fs-field" type="number" min={0} value={budget} onChange={(e) => setBudget(Number(e.target.value) || 0)} />
        </label>
        <Button type="submit" variant="secondary" size="sm" label={t('Compute context')} loading={busy} testId="requirement-context-run" />
      </form>
      {result && (
        <div className="fs-req__context-result" data-testid="requirement-context-result">
          <p className="fs-panel__label">{tn(result.requirements.length, '{n} requirement included', '{n} requirements included')}</p>
          {result.requirements.length === 0 ? (
            <p className="fs-req__muted">{t('Nothing relevant fit the search.')}</p>
          ) : (
            <ul className="fs-req__ctx-list">
              {result.requirements.map((r) => (
                <li key={r.key}>
                  <code>{r.key}</code> {r.title}
                </li>
              ))}
            </ul>
          )}
          <p className="fs-panel__label">{t('Omitted (did not fit the budget)')}</p>
          {result.omitted.length === 0 ? (
            <p className="fs-req__muted">{t('None.')}</p>
          ) : (
            <ul className="fs-req__ctx-list">
              {result.omitted.map((o) => (
                <li key={o.key}>
                  <code>{o.key}</code> {o.title} <span className="fs-req__muted">({tn(o.chars, '{n} char', '{n} chars')})</span>
                </li>
              ))}
            </ul>
          )}
          <p className="fs-panel__label">{t('Unknown ids')}</p>
          {result.unknown.length === 0 ? <p className="fs-req__muted">{t('None.')}</p> : <p className="fs-req__ctx-list">{result.unknown.join(', ')}</p>}
          <p className="fs-req__muted">{t('{used} of {budget} characters used.', { used: result.used_chars, budget: result.budget_chars })}</p>
        </div>
      )}
    </section>
  );
}

/** A read-only preview of `.faustus/requirements.yaml`, shown only when the
 *  file actually exists (`docs/requirements-format.md`) — its items are
 *  never auto-imported; a person turns one into a real requirement here,
 *  one at a time, which is a human decision, not a database side effect. */
function SidecarPreview({ projectId, say, onImported }: { projectId: string; say: (m: string) => void; onImported: () => void }) {
  const [sidecar, setSidecar] = useState<SidecarResult | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    getSidecar(projectId)
      .then(setSidecar)
      .catch(() => setSidecar(null));
  }, [projectId]);

  if (!sidecar || !sidecar.path) return null;

  const importItem = async (item: SidecarItem, i: number) => {
    if (!item.title) return;
    const id = item.key || String(i);
    setBusy(id);
    try {
      const status = REQUIREMENT_STATUSES.includes(item.status as RequirementStatus) ? (item.status as RequirementStatus) : undefined;
      await createRequirement(projectId, {
        title: item.title,
        text: item.text ?? '',
        source: REQUIREMENT_SOURCES.includes(item.source as RequirementSource) ? (item.source as RequirementSource) : 'doc',
        acceptance: item.acceptance ?? [],
        proposed_by: 'human',
        status,
      });
      onImported();
      say(t('Created as a real requirement.'));
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="fs-panel" data-testid="requirement-sidecar">
      <h3>
        <FolderInput size={15} aria-hidden="true" /> {t('Sidecar: .faustus/requirements.yaml')}
      </h3>
      <p className="fs-prose">{t('A hand-written record in the project folder. Nothing here is imported automatically — pick what to turn into a real, tracked requirement.')}</p>
      {sidecar.errors.length > 0 && (
        <p className="fs-req__note" data-tone="warn">
          {tn(sidecar.errors.length, '{n} line could not be parsed and was skipped.', '{n} lines could not be parsed and were skipped.', { n: sidecar.errors.length })}
        </p>
      )}
      {sidecar.items.length === 0 ? (
        <p className="fs-req__muted">{t('The sidecar has no items.')}</p>
      ) : (
        <ul className="fs-req__sidecar-list">
          {sidecar.items.map((item, i) => (
            <li key={item.key || i}>
              <span>
                {item.key && <code>{item.key}</code>} {item.title || t('(no title)')}
              </span>
              <Button
                variant="ghost"
                size="sm"
                icon={Plus}
                label={t('Create as requirement')}
                loading={busy === (item.key || String(i))}
                disabled={!item.title}
                onClick={() => void importItem(item, i)}
              />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function ProjectRequirements({ projectId, say }: { projectId: string; say: (m: string) => void }) {
  const [list, setList] = useState<Requirement[] | null>(null);
  const [statusFilter, setStatusFilter] = useState('');
  const [q, setQ] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const [matrix, setMatrix] = useState<MatrixRow[] | null>(null);
  const [showMatrix, setShowMatrix] = useState(false);
  const [showContext, setShowContext] = useState(false);

  const [newTitle, setNewTitle] = useState('');
  const [newText, setNewText] = useState('');
  const [newAcceptance, setNewAcceptance] = useState('');
  const [newSource, setNewSource] = useState<RequirementSource>('human');
  const [creating, setCreating] = useState(false);

  const reload = useCallback(() => {
    listRequirements(projectId, { status: statusFilter, q })
      .then((d) => setList(d.requirements))
      .catch((e: Error) => say(e.message));
  }, [projectId, statusFilter, q, say]);

  useEffect(reload, [reload]);

  const reloadMatrix = useCallback(() => {
    getProjectMatrix(projectId)
      .then((d) => setMatrix(d.matrix))
      .catch(() => setMatrix([]));
  }, [projectId]);

  useEffect(() => {
    if (showMatrix) reloadMatrix();
  }, [showMatrix, reloadMatrix]);

  const onChanged = () => {
    reload();
    if (showMatrix) reloadMatrix();
  };

  const create = async () => {
    if (!newTitle.trim()) return;
    setCreating(true);
    try {
      const created = await createRequirement(projectId, {
        title: newTitle.trim(),
        text: newText,
        source: newSource,
        acceptance: acceptanceFromLines(newAcceptance),
        proposed_by: 'human',
      });
      setNewTitle('');
      setNewText('');
      setNewAcceptance('');
      onChanged();
      setSelected(created.requirement.key);
      say(t('Requirement created.'));
    } catch (e) {
      say((e as Error).message);
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="fs-req" data-testid="project-requirements">
      <p className="fs-prose">
        {t('A versioned spec for this project: every requirement carries who proposed it, who — a human, always — accepted or rejected it, an immutable history, and typed evidence linking it to code, tests, and runs.')}
      </p>

      <div className="fs-req__toolbar">
        <select className="fs-field" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} aria-label={t('Filter by status')}>
          <option value="">{t('All statuses')}</option>
          {REQUIREMENT_STATUSES.map((s) => (
            <option key={s} value={s}>
              {t(STATUS_LABEL[s])}
            </option>
          ))}
        </select>
        <input className="fs-field fs-req__grow" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t('Search title or text…')} data-testid="requirement-search" />
        <Button variant="ghost" size="sm" icon={Gauge} label={showMatrix ? t('Hide coverage matrix') : t('Show coverage matrix')} onClick={() => setShowMatrix((v) => !v)} testId="requirement-toggle-matrix" />
        <Button variant="ghost" size="sm" icon={ClipboardList} label={showContext ? t('Hide context tool') : t('Context for a task')} onClick={() => setShowContext((v) => !v)} testId="requirement-toggle-context" />
      </div>

      <section className="fs-panel">
        <p className="fs-panel__label">{t('New requirement')}</p>
        <form
          className="fs-req__create"
          onSubmit={(e) => {
            e.preventDefault();
            void create();
          }}
        >
          <input className="fs-field" value={newTitle} onChange={(e) => setNewTitle(e.target.value)} placeholder={t('Title')} maxLength={300} data-testid="requirement-new-title" />
          <textarea className="fs-field fs-req__textarea" rows={2} value={newText} onChange={(e) => setNewText(e.target.value)} placeholder={t('Description (optional)')} />
          <textarea
            className="fs-field fs-req__textarea"
            rows={2}
            value={newAcceptance}
            onChange={(e) => setNewAcceptance(e.target.value)}
            placeholder={t('Acceptance criteria, one per line')}
          />
          <div className="fs-req__form-row">
            <select className="fs-field" value={newSource} onChange={(e) => setNewSource(e.target.value as RequirementSource)} aria-label={t('Source')}>
              {REQUIREMENT_SOURCES.map((s) => (
                <option key={s} value={s}>
                  {t(SOURCE_LABEL[s])}
                </option>
              ))}
            </select>
            <Button type="submit" variant="secondary" size="sm" icon={Plus} label={t('Add requirement')} loading={creating} disabled={!newTitle.trim()} testId="requirement-create" />
          </div>
        </form>
      </section>

      {showContext && <ContextTool projectId={projectId} say={say} />}

      <SidecarPreview projectId={projectId} say={say} onImported={onChanged} />

      {showMatrix && (
        <section className="fs-panel" data-testid="requirement-matrix">
          <p className="fs-panel__label">{t('Coverage matrix — whole project')}</p>
          {matrix === null ? (
            <Skeleton label={t('Loading the matrix')} count={3} height="24px" />
          ) : matrix.length === 0 ? (
            <p className="fs-req__muted">{t('No requirements yet.')}</p>
          ) : (
            <ul className="fs-req__matrix-list">
              {matrix.map((row) => (
                <li key={row.key}>
                  <code>{row.key}</code>
                  <MatrixDots row={row} />
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      <div className="fs-req__body">
        <section className="fs-req__list-panel">
          <p className="fs-panel__label">{list ? tn(list.length, '{n} requirement', '{n} requirements') : t('Requirements')}</p>
          {list === null ? (
            <Skeleton label={t('Loading the requirements')} count={4} height="40px" />
          ) : list.length === 0 ? (
            <EmptyState
              icon={ClipboardList}
              title={t('No requirements yet')}
              body={t('Add the first one above, or import one from the sidecar file below if this project has one.')}
            />
          ) : (
            <ul className="fs-req__list" data-testid="requirement-list">
              {list.map((r) => (
                <li key={r.id}>
                  <button
                    type="button"
                    className="fs-req__row"
                    data-selected={selected === r.key || undefined}
                    onClick={() => setSelected(r.key)}
                    data-testid="requirement-row"
                  >
                    <code className="fs-req__row-key">{r.key}</code>
                    <span className="fs-req__row-title">{r.title}</span>
                    <Badge tone={STATUS_TONE[r.status]}>{t(STATUS_LABEL[r.status])}</Badge>
                    <ProposedByBadge by={r.proposed_by} />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="fs-req__detail-panel">
          {selected ? (
            <RequirementDetail projectId={projectId} reqKey={selected} say={say} onChanged={onChanged} />
          ) : (
            <EmptyState icon={ClipboardList} title={t('No requirement selected')} body={t('Choose one from the list to see its text, evidence, links and history.')} headingLevel={3} />
          )}
        </section>
      </div>
    </div>
  );
}
