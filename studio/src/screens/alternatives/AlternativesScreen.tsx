import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router';
import { FlaskConical, GitFork, Plus, RefreshCw } from 'lucide-react';
import { Button, Dialog, EmptyState, Skeleton, Toast } from '../../components';
import * as api from '../../adapters/alternatives';
import { listProjects, type Project } from '../../adapters/projects';
import { t } from '../../i18n';
import { CompareView } from './CompareView';
import './alternatives.css';

/**
 * Alternatives (W2-G / CMP-13, INFORME §3.12).
 *
 * "Faustus already lets you branch" is not the same claim as this screen:
 * a git branch is one working tree, so trying two approaches to the same
 * task still means stashing one to try the other, and the agent has to be
 * told in prose which lines came from which idea. Here, an "experiment"
 * holds N ISOLATED alternatives at once — each in its own worktree, snapshot
 * directory, or (for a single document) its own text — so two of them, and
 * the user's own main copy, can each hold different uncommitted content on
 * the same files at the same time. Comparing is a real diff against one
 * shared base, not a memory; applying is a three-way merge that never
 * destroys a manual edit made to the main copy since the experiment started
 * (`CompareView`'s own conflict banner is where that guarantee is visible).
 */

function baseKindWord(kind: api.BaseKind): string {
  switch (kind) {
    case 'git_sha': return t('git repository');
    case 'snapshot': return t('folder snapshot');
    default: return t('document');
  }
}

function ExperimentCard({ exp, onOpen }: { exp: api.Experiment; onOpen: (id: string) => void }) {
  return (
    <button
      type="button"
      className="fs-alt__exp-card"
      onClick={() => onOpen(exp.id)}
      data-testid={`exp-${exp.id}`}
    >
      <span className="fs-alt__card-top">
        <b className="fs-alt__grow">{exp.goal}</b>
        <span className="fs-alt__tag">{baseKindWord(exp.base_kind)}</span>
      </span>
      <span className="fs-alt__muted">
        {t('{n} alternative(s)', { n: exp.alternatives.length })}
        {exp.applied ? ` — ${t('applied')}` : ''}
      </span>
    </button>
  );
}

function NewExperiment({ projects, defaultProjectId, busy, onClose, onSubmit }: {
  projects: Project[];
  defaultProjectId: string;
  busy: boolean;
  onClose: () => void;
  onSubmit: (goal: string, workspace: string) => void;
}) {
  const [goal, setGoal] = useState('');
  const [workspace, setWorkspace] = useState('');
  const project = projects.find((p) => p.id === defaultProjectId);
  return (
    <Dialog
      open
      onOpenChange={(isOpen) => !isOpen && onClose()}
      title={t('New experiment')}
      testId="alt-new"
      footer={(
        <Button
          variant="primary" size="sm" icon={FlaskConical} label={t('Start')}
          loading={busy} disabled={!goal.trim()}
          onClick={() => onSubmit(goal.trim(), workspace.trim())}
        />
      )}
    >
      <div className="fs-alt__form">
        <label className="fs-alt__row-field">
          <span>{t('Goal')}</span>
          <textarea
            className="fs-field fs-alt__textarea" rows={2} autoFocus value={goal}
            onChange={(event) => setGoal(event.target.value)}
            placeholder={t('what these alternatives are trying to answer')}
          />
        </label>
        <label className="fs-alt__row-field">
          <span>{t('Workspace')}</span>
          <input
            className="fs-field fs-alt__mono" type="text" value={workspace}
            onChange={(event) => setWorkspace(event.target.value)}
            placeholder={project?.workspace || t('an absolute path — defaults to the project\'s own workspace')}
          />
        </label>
      </div>
    </Dialog>
  );
}

export function AlternativesScreen() {
  const [params, setParams] = useSearchParams();
  const projectId = params.get('project') ?? '';
  const openId = params.get('exp') ?? '';

  const [projects, setProjects] = useState<Project[]>([]);
  const [experiments, setExperiments] = useState<api.Experiment[] | null>(null);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState<{ text: string; tone: 'ok' | 'warn' } | null>(null);
  const [formOpen, setFormOpen] = useState(false);

  const say = useCallback((text: string, tone: 'ok' | 'warn' = 'ok') => {
    setNotice({ text, tone });
    window.setTimeout(() => setNotice(null), 5000);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    listProjects(controller.signal).then((rows) => {
      setProjects(rows);
      if (!projectId && rows.length > 0) {
        const next = new URLSearchParams(params);
        next.set('project', rows[0].id);
        setParams(next, { replace: true });
      }
    }).catch(() => setProjects([]));
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const load = useCallback(async (signal?: AbortSignal) => {
    if (!projectId) {
      setExperiments([]);
      return;
    }
    try {
      const answer = await api.listExperiments(projectId, signal);
      setExperiments(answer.experiments);
    } catch (error) {
      if (signal?.aborted) return;
      setExperiments((current) => current ?? []);
      say((error as Error).message, 'warn');
    }
  }, [projectId, say]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const selectProject = useCallback((id: string) => {
    const next = new URLSearchParams(params);
    if (id) next.set('project', id); else next.delete('project');
    next.delete('exp');
    setParams(next, { replace: true });
  }, [params, setParams]);

  const open = useCallback((id: string) => {
    const next = new URLSearchParams(params);
    next.set('exp', id);
    setParams(next, { replace: false });
  }, [params, setParams]);

  const back = useCallback(() => {
    const next = new URLSearchParams(params);
    next.delete('exp');
    setParams(next, { replace: false });
  }, [params, setParams]);

  const create = useCallback(async (goal: string, workspace: string) => {
    setBusy('create');
    try {
      const exp = await api.createExperiment(projectId, goal, workspace || undefined);
      setFormOpen(false);
      say(t('Experiment started.'));
      await load();
      open(exp.id);
    } catch (error) {
      say((error as Error).message, 'warn');
    } finally {
      setBusy('');
    }
  }, [projectId, say, load, open]);

  return (
    <div className="fs-screen fs-alt" data-testid="alternatives">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Alternatives')}</h1>
          <p className="fs-prose fs-alt__lede">
            {t('Try more than one approach to the same task, each isolated from the other and from your own edits, then compare and apply the one you want — never a silent overwrite.')}
          </p>
        </div>
        <div className="fs-inline">
          <label className="fs-alt__project-picker">
            <span className="fs-alt__sr">{t('Project')}</span>
            <select className="fs-field" value={projectId} onChange={(event) => selectProject(event.target.value)}>
              <option value="">{t('choose a project')}</option>
              {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
            </select>
          </label>
          <Button variant="secondary" size="sm" icon={RefreshCw} label={t('Reload')} onClick={() => void load()} />
          <Button
            variant="primary" size="sm" icon={Plus} label={t('New experiment')}
            disabled={!projectId}
            onClick={() => setFormOpen(true)}
          />
        </div>
      </header>

      {openId ? (
        <CompareView
          projectId={projectId}
          expId={openId}
          onBack={back}
          onDeleted={() => { back(); void load(); }}
        />
      ) : experiments === null ? (
        <Skeleton label={t('Reading the experiments')} count={3} height="64px" />
      ) : experiments.length === 0 ? (
        <EmptyState
          icon={GitFork}
          title={t('No experiments yet')}
          body={projectId
            ? t('Start one to try more than one approach to the same task, side by side.')
            : t('Choose a project first.')}
          primaryAction={projectId ? { label: t('New experiment'), icon: Plus, onClick: () => setFormOpen(true) } : undefined}
        />
      ) : (
        <div className="fs-alt__list" aria-label={t('Experiments')}>
          {experiments.map((exp) => <ExperimentCard key={exp.id} exp={exp} onOpen={open} />)}
        </div>
      )}

      {formOpen && (
        <NewExperiment
          projects={projects}
          defaultProjectId={projectId}
          busy={busy === 'create'}
          onClose={() => setFormOpen(false)}
          onSubmit={(goal, workspace) => void create(goal, workspace)}
        />
      )}

      {notice && <Toast>{notice.text}</Toast>}
    </div>
  );
}
