import { useEffect, useState } from 'react';
import { Button, Dialog } from '../../components';
import { deleteBranch, GitApiError, type GitRepo } from '../../adapters/git';
import { Toggle } from '../settings/fields';
import { t } from '../../i18n';

/**
 * OBJ-4 / Lote 89 — the branch popover's per-row "Delete branch": confirm
 * first (destructive), and on 409 `git.branch_unmerged` swap the confirm
 * button for "Delete anyway" (retries with `force: true`) instead of just
 * failing — the same "ask once, offer the override inline" shape
 * `ChangesPane`'s discard-confirm already uses.
 */
export function DeleteBranchDialog({
  open,
  onOpenChange,
  repoId,
  branchName,
  onDeleted,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  repoId: string;
  branchName: string | null;
  onDeleted: (repo: GitRepo, name: string) => void;
}) {
  const [remote, setRemote] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unmerged, setUnmerged] = useState(false);

  useEffect(() => {
    if (!open) return;
    setRemote(false);
    setError(null);
    setUnmerged(false);
  }, [open, branchName]);

  const submit = (force: boolean) => {
    if (!branchName) return;
    setBusy(true);
    setError(null);
    deleteBranch(repoId, branchName, { force, remote })
      .then((res) => {
        onDeleted(res.repo, branchName);
        onOpenChange(false);
      })
      .catch((e: unknown) => {
        if (e instanceof GitApiError && e.errorClass === 'git.branch_unmerged') {
          setUnmerged(true);
        } else {
          setError((e as Error).message);
        }
      })
      .finally(() => setBusy(false));
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={t('Delete branch {branch}?', { branch: branchName ?? '' })}
      testId="delete-branch-dialog"
      footer={
        <>
          <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => onOpenChange(false)} />
          <Button
            variant="danger-solid"
            size="sm"
            label={unmerged ? t('Delete anyway') : t('Delete')}
            loading={busy}
            onClick={() => submit(unmerged)}
            testId="delete-branch-confirm"
          />
        </>
      }
    >
      <p className="fs-prose">{t('This removes the local branch. It cannot be undone.')}</p>
      <Toggle id="delete-branch-remote" checked={remote} onChange={setRemote} label={t('Also delete on origin')} />
      {unmerged && (
        <p className="fs-notice" data-tone="warning" role="alert" data-testid="delete-branch-unmerged">
          {t('This branch is not fully merged into the current one — deleting it may lose commits.')}
        </p>
      )}
      {error && (
        <p className="fs-notice" data-tone="danger" role="alert" data-testid="delete-branch-error">
          {error}
        </p>
      )}
    </Dialog>
  );
}
