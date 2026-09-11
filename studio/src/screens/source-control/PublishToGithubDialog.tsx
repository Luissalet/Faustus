import { ExternalLink } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button, Dialog } from '../../components';
import {
  getGithubAccounts,
  GitApiError,
  listIdentities,
  publishToGithub,
  type CreateRepoResult,
  type GitIdentity,
  type GithubAccountsResponse,
  type GitRepo,
} from '../../adapters/git';
import { Field, Select, str, Text, Toggle } from '../settings/fields';
import { t } from '../../i18n';

/**
 * OBJ-4 / Lote 85 (CONTRATO_GIT_3.md) — "Publish to GitHub": the repo
 * header's action for a repo that has no `origin` yet. Same `gh`-backed
 * create-and-wire-remote flow `NewRepositoryDialog`'s GitHub section
 * offers when starting a repo, just for one that already exists locally —
 * `POST /repos/{id}/github/publish` instead of the `github` block on
 * `POST /repos`.
 */
export function PublishToGithubDialog({
  open,
  onOpenChange,
  repo,
  onPublished,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  repo: GitRepo;
  onPublished: (repo: GitRepo) => void;
}) {
  const [accounts, setAccounts] = useState<GithubAccountsResponse | null>(null);
  const [identities, setIdentities] = useState<GitIdentity[] | null>(null);
  const [login, setLogin] = useState('');
  const [name, setName] = useState(repo.name);
  const [visibility, setVisibility] = useState<'private' | 'public'>('private');
  const [identityId, setIdentityId] = useState('');
  const [push, setPush] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; detail?: string } | null>(null);
  const [result, setResult] = useState<CreateRepoResult | null>(null);

  useEffect(() => {
    if (!open) return;
    setError(null);
    setResult(null);
    setName(repo.name);
    getGithubAccounts()
      .then((res) => {
        setAccounts(res);
        setLogin((cur) => cur || res.accounts.find((a) => a.active)?.login || res.accounts[0]?.login || '');
      })
      .catch(() => setAccounts({ available: false, version: null, accounts: [] }));
    listIdentities()
      .then((r) => setIdentities(r.identities))
      .catch(() => setIdentities([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, repo.id]);

  const reset = () => {
    setLogin('');
    setIdentityId('');
    setVisibility('private');
    setPush(true);
    setError(null);
    setResult(null);
  };

  const canSubmit = accounts?.available === true && login.trim().length > 0 && name.trim().length > 0 && !busy;

  const submit = () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    publishToGithub(repo.id, {
      login: login.trim(),
      private: visibility === 'private',
      name: name.trim() !== repo.name ? name.trim() : undefined,
      identityId: identityId || undefined,
      push,
    })
      .then((res) => {
        onPublished(res.repo);
        setResult(res);
      })
      .catch((e: unknown) => {
        if (e instanceof GitApiError) {
          const payloadRepo = e.payload.repo as GitRepo | undefined;
          if (payloadRepo && typeof payloadRepo === 'object') onPublished(payloadRepo);
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
      title={t('Publish to GitHub')}
      description={t('Create a GitHub repository for {name} and set it as origin.', { name: repo.name })}
      testId="publish-github-dialog"
      footer={
        result ? (
          <Button variant="primary" size="sm" label={t('Done')} onClick={() => onOpenChange(false)} />
        ) : (
          <>
            <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => onOpenChange(false)} />
            <Button variant="primary" size="sm" label={t('Publish')} loading={busy} disabled={!canSubmit} onClick={submit} testId="publish-github-submit" />
          </>
        )
      }
    >
      {result ? (
        <p className="fs-prose" data-testid="publish-github-success">
          {t('Published as {full_name}.', { full_name: result.github?.full_name ?? '' })}{' '}
          {result.github?.html_url && (
            <a href={result.github.html_url} target="_blank" rel="noreferrer" className="fs-inline">
              {result.github.html_url} <ExternalLink size={12} aria-hidden="true" />
            </a>
          )}
        </p>
      ) : (
        <>
          {accounts && !accounts.available && (
            <p className="fs-notice" data-tone="warning">
              {t('GitHub CLI (gh) not found — install it and run `gh auth login` to create repositories from here.')}
            </p>
          )}
          <Field label={t('GitHub account')} htmlFor="publish-github-login">
            <Select
              id="publish-github-login"
              value={login}
              onChange={setLogin}
              options={(accounts?.accounts ?? []).map((a) => ({ value: a.login, label: a.active ? t('{login} (active)', { login: a.login }) : a.login }))}
            />
          </Field>
          <Field label={t('Repository name')} htmlFor="publish-github-name">
            <Text id="publish-github-name" value={name} onChange={setName} disabled={!accounts?.available} />
          </Field>
          <div className="fs-seg" role="radiogroup" aria-label={t('Visibility')}>
            <button type="button" role="radio" aria-checked={visibility === 'private'} onClick={() => setVisibility('private')} data-testid="publish-github-private">
              {t('Private')}
            </button>
            <button type="button" role="radio" aria-checked={visibility === 'public'} onClick={() => setVisibility('public')} data-testid="publish-github-public">
              {t('Public')}
            </button>
          </div>
          <Field label={t('SSH identity (optional)')} htmlFor="publish-github-identity">
            <Select
              id="publish-github-identity"
              value={identityId}
              onChange={setIdentityId}
              allowEmpty={t('None')}
              options={(identities ?? []).map((i) => ({ value: i.id, label: i.label }))}
            />
          </Field>
          <Toggle id="publish-github-push" checked={push} onChange={setPush} label={t('Push after creating')} disabled={!accounts?.available} />
        </>
      )}
      {error && (
        <p className="fs-notice" data-tone="danger" role="alert" data-testid="publish-github-error">
          {error.message}
          {error.detail ? ` — ${error.detail}` : ''}
        </p>
      )}
    </Dialog>
  );
}
