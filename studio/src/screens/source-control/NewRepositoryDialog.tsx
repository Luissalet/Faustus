import { useEffect, useState } from 'react';
import { Button, Dialog } from '../../components';
import {
  createRepo,
  GitApiError,
  isValidRepoName,
  listFolders,
  listIdentities,
  type GitFolder,
  type GitIdentity,
  type GitRepo,
} from '../../adapters/git';
import { Field, Select, str, Text, Toggle } from '../settings/fields';
import { t } from '../../i18n';

/**
 * OBJ-4 / Lote 83 — "New repository": one dialog, two modes ("Create here" /
 * "Clone") instead of two separate flows, since every field but the clone
 * URL and the initial-commit checkbox is shared between them
 * (CONTRATO_GIT_2.md: "que se puedan crear repos y ramas fácil sin movidas,
 * con un botón y eligiendo nombre y de dónde vienen y ya"). `POST
 * /api/git/repos` does the rest; on success the new repo is handed back to
 * the caller so it can be selected immediately.
 */
export function NewRepositoryDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (repo: GitRepo) => void;
}) {
  const [mode, setMode] = useState<'init' | 'clone'>('init');
  const [folders, setFolders] = useState<GitFolder[] | null>(null);
  const [parentFolder, setParentFolder] = useState('');
  const [subpath, setSubpath] = useState('');
  const [name, setName] = useState('');
  const [url, setUrl] = useState('');
  const [identities, setIdentities] = useState<GitIdentity[] | null>(null);
  const [identityId, setIdentityId] = useState('');
  const [initialCommit, setInitialCommit] = useState(true);
  const [defaultBranch, setDefaultBranch] = useState('main');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; detail?: string } | null>(null);

  useEffect(() => {
    if (!open) return;
    setError(null);
    listFolders()
      .then((r) => {
        setFolders(r.folders);
        setParentFolder((cur) => cur || r.folders[0]?.path || '');
      })
      .catch(() => setFolders([]));
    listIdentities()
      .then((r) => setIdentities(r.identities))
      .catch(() => setIdentities([]));
  }, [open]);

  const reset = () => {
    setMode('init');
    setSubpath('');
    setName('');
    setUrl('');
    setIdentityId('');
    setInitialCommit(true);
    setDefaultBranch('main');
    setError(null);
  };

  const nameValid = isValidRepoName(name);
  const canSubmit = Boolean(parentFolder) && nameValid && (mode === 'init' || url.trim().length > 0) && !busy;

  const submit = () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    const parent = subpath.trim() ? `${parentFolder.replace(/\/+$/, '')}/${subpath.trim().replace(/^\/+/, '')}` : parentFolder;
    createRepo({
      mode,
      parentFolder: parent,
      name: name.trim(),
      url: mode === 'clone' ? url.trim() : undefined,
      identityId: identityId || undefined,
      initialCommit: mode === 'init' ? initialCommit : undefined,
      defaultBranch: defaultBranch.trim() || 'main',
    })
      .then((res) => {
        onCreated(res.repo);
        onOpenChange(false);
        reset();
      })
      .catch((e: unknown) => {
        if (e instanceof GitApiError) {
          setError({ message: e.message, detail: str(e.payload.stderr) || str(e.payload.detail) || undefined });
        } else {
          setError({ message: (e as Error).message });
        }
      })
      .finally(() => setBusy(false));
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next);
        if (!next) reset();
      }}
      title={t('New repository')}
      description={t('Start a repository in one of your linked folders, or clone one from a URL.')}
      testId="new-repo-dialog"
      footer={
        <>
          <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => onOpenChange(false)} />
          <Button
            variant="primary"
            size="sm"
            label={mode === 'clone' ? t('Clone') : t('Create')}
            loading={busy}
            disabled={!canSubmit}
            onClick={submit}
            testId="new-repo-submit"
          />
        </>
      }
    >
      <div className="fs-seg" role="radiogroup" aria-label={t('Where this repository comes from')}>
        <button type="button" role="radio" aria-checked={mode === 'init'} onClick={() => setMode('init')} data-testid="new-repo-mode-init">
          {t('Create here')}
        </button>
        <button type="button" role="radio" aria-checked={mode === 'clone'} onClick={() => setMode('clone')} data-testid="new-repo-mode-clone">
          {t('Clone')}
        </button>
      </div>

      <Field label={t('Name')} htmlFor="new-repo-name">
        <Text id="new-repo-name" value={name} onChange={setName} placeholder={t('my-project')} />
      </Field>
      {name.length > 0 && !nameValid && (
        <p className="fs-notice" data-tone="danger" role="alert">
          {t('Only letters, numbers, dots, dashes and underscores.')}
        </p>
      )}

      <Field label={t('Parent folder')} htmlFor="new-repo-parent" help={t('One of your linked project folders — a new folder is created inside it for this repository.')}>
        <Select
          id="new-repo-parent"
          value={parentFolder}
          onChange={setParentFolder}
          options={(folders ?? []).map((f) => ({ value: f.path, label: f.project_name ? `${f.project_name} — ${f.path}` : f.path }))}
        />
      </Field>
      <Field label={t('Subfolder (optional)')} htmlFor="new-repo-subpath">
        <Text id="new-repo-subpath" value={subpath} onChange={setSubpath} placeholder={t('e.g. work/')} />
      </Field>

      {mode === 'clone' && (
        <Field label={t('URL')} htmlFor="new-repo-url" help={t('An https:// or git@ URL. Pick an SSH identity below to clone over ssh.')}>
          <Text id="new-repo-url" value={url} onChange={setUrl} placeholder="git@github.com:owner/repo.git" />
        </Field>
      )}

      <Field label={t('SSH identity')} htmlFor="new-repo-identity" help={mode === 'init' ? t('Sets this repository\'s local user.name/user.email.') : t('Used to clone over ssh with this identity\'s key.')}>
        <Select
          id="new-repo-identity"
          value={identityId}
          onChange={setIdentityId}
          allowEmpty={t('None')}
          options={(identities ?? []).map((i) => ({ value: i.id, label: i.label }))}
        />
      </Field>

      {mode === 'init' && (
        <Toggle id="new-repo-initial-commit" checked={initialCommit} onChange={setInitialCommit} label={t('Create an initial commit')} />
      )}

      <Field label={t('Default branch')} htmlFor="new-repo-branch">
        <Text id="new-repo-branch" value={defaultBranch} onChange={setDefaultBranch} placeholder="main" />
      </Field>

      {error && (
        <p className="fs-notice" data-tone="danger" role="alert" data-testid="new-repo-error">
          {error.message}
          {error.detail ? ` — ${error.detail}` : ''}
        </p>
      )}
    </Dialog>
  );
}
