import { FolderOpen, Save, Sparkles, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button } from '../../components';
import { pickNative } from '../../adapters/composer';
import { AGENT_FLAGS, createProject, draftAgentsMd, flagOn, updateProject, type Project } from '../../adapters/projects';
import { getSummary, isValidBoardKey, setBoardKey as setBoardKeyApi } from '../../adapters/board';
import { getLang, t } from '../../i18n';

/**
 * The project's settings: name, folders, instructions and the agent's
 * manners in that folder. One form, everything visible; the same fields
 * projects.js had. `project === null` creates.
 */
export function ProjectSettings({ project, onSaved, onCancel, say }: { project: Project | null; onSaved: (p: Project) => void; onCancel: () => void; say: (msg: string) => void }) {
  const [name, setName] = useState(project?.name ?? '');
  const [workspace, setWorkspace] = useState(project?.workspace ?? '');
  const [instructions, setInstructions] = useState(project?.instructions ?? '');
  const [flags, setFlags] = useState<Record<string, boolean>>(() => Object.fromEntries(AGENT_FLAGS.map((f) => [f.key, project ? flagOn(project, f.key) : f.def])));
  const [testCommand, setTestCommand] = useState(project?.test_command ?? '');
  const [reviewModel, setReviewModel] = useState(project?.review_model ?? '');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState('');

  // Lote 94: the board key (CONTRATO_BOARD) — a per-project setting with its
  // OWN endpoint (`PUT .../board/key`, `adapters/board.ts`'s `setBoardKey`),
  // not one of `updateProject`'s fields, so it saves independently of the
  // form's own `save()`. Only meaningful once the project exists (an id is
  // required to call `/board/*` at all), so this whole block is a no-op for
  // the "create a project" flow (`project === null`).
  const [boardKey, setBoardKeyValue] = useState('');
  const [boardKeySaving, setBoardKeySaving] = useState(false);
  const [boardKeyError, setBoardKeyError] = useState<string | null>(null);
  useEffect(() => {
    if (!project?.id) return;
    let live = true;
    getSummary(project.id).then((s) => {
      if (live) setBoardKeyValue(s.key);
    }).catch(() => {
      /* the board key is a nice-to-have here; a failed load just leaves the
       * field blank rather than blocking the rest of the settings form. */
    });
    return () => {
      live = false;
    };
  }, [project?.id]);

  const saveBoardKey = async () => {
    if (!project?.id) return;
    const clean = boardKey.trim().toUpperCase();
    if (!isValidBoardKey(clean)) return;
    setBoardKeySaving(true);
    setBoardKeyError(null);
    try {
      const { key } = await setBoardKeyApi(project.id, clean);
      setBoardKeyValue(key);
      say(t('Key: {key}', { key }));
    } catch (e) {
      setBoardKeyError((e as Error).message);
    } finally {
      setBoardKeySaving(false);
    }
  };

  const cost = Math.max(1, Math.round(instructions.length / 4));

  const save = async () => {
    const clean = name.trim();
    if (!clean) return setError(t('The project needs a name'));
    setSaving(true);
    setError(null);
    const agent = { ...flags, test_command: testCommand.trim(), review_model: reviewModel.trim() };
    try {
      let saved: Project;
      if (project) {
        saved = await updateProject(project.id, { name: clean, workspace: workspace.trim(), instructions, ...agent });
      } else {
        const created = await createProject({ name: clean, folder: clean, workspace: workspace.trim(), instructions });
        saved = await updateProject(created.id, agent);
      }
      say(project ? t('Project saved') : t('Project “{name}” created', { name: saved.name }));
      onSaved(saved);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const browse = async () => {
    try {
      const pick = await pickNative('folder', workspace);
      if (pick.status === 'ok' && pick.path) setWorkspace(pick.path);
      else if (pick.status === 'unavailable') say(t('No system dialog here; type the path.'));
    } catch (e) {
      say((e as Error).message);
    }
  };

  const draft = async () => {
    if (!workspace.trim()) return setNote(t('Set the working folder first.'));
    setNote(t('Drafting…'));
    try {
      setNote(await draftAgentsMd(workspace.trim(), getLang() === 'es' ? 'es' : 'en'));
    } catch (e) {
      setNote(`${t('Could not draft')}: ${(e as Error).message}`);
    }
  };

  return (
    <form
      className="fs-pj__form"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
      data-testid="project-form"
    >
      <label className="fs-pj__field">
        <span>{t('Name')}</span>
        <input className="fs-field" value={name} onChange={(e) => setName(e.target.value)} maxLength={80} placeholder={t('My project')} required data-testid="project-name" />
      </label>
      <label className="fs-pj__field">
        <span>
          {t('Conversation group')}
          <small>{project ? t('Fixed once created: it is what files the chats.') : t('Created from the name.')}</small>
        </span>
        <input className="fs-field" value={project ? (project.folder ?? '') : name.trim()} readOnly />
      </label>
      <label className="fs-pj__field">
        <span>
          {t('Working folder')}
          <small>{t('Optional. The agent reads and changes files here.')}</small>
        </span>
        <span className="fs-pj__row">
          <input className="fs-field fs-pj__grow" value={workspace} onChange={(e) => setWorkspace(e.target.value)} placeholder="D:\\Projects\\my-project" spellCheck={false} data-testid="project-workspace" />
          <Button variant="secondary" size="sm" icon={FolderOpen} label={t('Browse…')} onClick={() => void browse()} />
        </span>
      </label>
      <label className="fs-pj__field">
        <span>
          {t('Instructions')}
          <small>{t('Sent with every message in this project — about {n} tokens.', { n: cost })}</small>
        </span>
        <textarea className="fs-field fs-pj__textarea" rows={8} maxLength={10000} value={instructions} onChange={(e) => setInstructions(e.target.value)} placeholder={t('How should Faustus work in this project?')} data-testid="project-instructions" />
      </label>

      <fieldset className="fs-pj__fieldset">
        <legend>
          {t('The agent in this folder')}
        </legend>
        {AGENT_FLAGS.map((f) => (
          <label key={f.key} className="fs-switch fs-pj__flag">
            <input type="checkbox" checked={flags[f.key]} onChange={(e) => setFlags((prev) => ({ ...prev, [f.key]: e.target.checked }))} data-testid={`project-flag-${f.key}`} />
            <span>
              <strong>{t(f.label)}</strong>
              <small>{t(f.help)}</small>
            </span>
          </label>
        ))}
        <label className="fs-pj__field">
          <span>
            {t('Test command')}
            <small>{t('Optional; empty detects pytest, npm test, cargo, go or make.')}</small>
          </span>
          <input className="fs-field" value={testCommand} onChange={(e) => setTestCommand(e.target.value)} placeholder="pytest -x -q tests/" spellCheck={false} maxLength={400} />
        </label>
        <label className="fs-pj__field">
          <span>
            {t('Reviewer model')}
            <small>{t('Optional; “same” is this chat’s model, or a model on the same endpoint.')}</small>
          </span>
          <input className="fs-field" value={reviewModel} onChange={(e) => setReviewModel(e.target.value)} placeholder="same" spellCheck={false} maxLength={200} />
        </label>
        <div className="fs-pj__row">
          <Button variant="ghost" size="sm" icon={Sparkles} label={t('Write an AGENTS.md from the folder')} title={t('From what the runtime detects: language, test runner, layout')} onClick={() => void draft()} />
          {note && <small className="fs-pj__note">{note}</small>}
        </div>
      </fieldset>

      {project && (
        <label className="fs-pj__field">
          <span>
            {t("This project's board key")}
            <small>{t('Ids for new issues start with this key. Changing it does not renumber existing ones.')}</small>
          </span>
          <span className="fs-pj__row">
            <input
              className="fs-field"
              value={boardKey}
              onChange={(e) => setBoardKeyValue(e.target.value.toUpperCase())}
              placeholder="FAU"
              maxLength={5}
              spellCheck={false}
              data-testid="project-board-key"
            />
            <Button
              variant="secondary"
              size="sm"
              icon={Save}
              label={t('Save changes')}
              loading={boardKeySaving}
              disabled={!isValidBoardKey(boardKey.trim().toUpperCase())}
              onClick={() => void saveBoardKey()}
            />
          </span>
          {boardKeyError && <p className="fs-pj__error" role="alert">{boardKeyError}</p>}
        </label>
      )}

      {error && <p className="fs-pj__error" role="alert">{error}</p>}

      <div className="fs-pj__row">
        <Button type="submit" variant="primary" size="sm" icon={Save} label={project ? t('Save changes') : t('Create project')} loading={saving} testId="project-save" />
        <Button variant="ghost" size="sm" icon={X} label={t('Cancel')} onClick={onCancel} />
      </div>
    </form>
  );
}
