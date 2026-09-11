import { useEffect, useState } from 'react';
import { Button, Dialog } from '../../components';
import {
  merge,
  GitApiError,
  type GitBranchesResponse,
  type GitMergeConflictPayload,
  type GitRepo,
} from '../../adapters/git';
import { Field, Select, Toggle, Text as FieldText } from '../settings/fields';
import { t } from '../../i18n';

/**
 * OBJ-4 / Lote 89 — Luis: "prueba también a mergear la rama desde ahí, no
 * solo crearla. necesitamos mergearla." "Merge into <current branch>": pick
 * a source branch (local or remote), choose fast-forward-when-possible vs.
 * always a merge commit, an optional message, Merge.
 *
 * A conflict (409 `git.merge_conflict`) replaces the form with the
 * conflicting files and two ways forward: "Keep the conflicts to resolve
 * them" retries the SAME merge with `keepConflicts: true` — the server
 * leaves the tree paused mid-merge, and `ChangesPane`'s own "Merge
 * conflicts" banner (driven by `status.conflicts`) takes over from there —
 * or "Abort", which just closes: the first attempt (`keepConflicts: false`,
 * the default) already rolled the merge all the way back server-side, so
 * there is nothing left here to undo.
 */
export function MergeDialog({
  open,
  onOpenChange,
  repoId,
  currentBranch,
  branches,
  initialBranch,
  onMerged,
  onConflictKept,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  repoId: string;
  currentBranch: string | null;
  branches: GitBranchesResponse | null;
  /** Preselect a branch — the branch popover's per-row "Merge into
   *  current…" already knows which one. */
  initialBranch?: string;
  onMerged: (repo: GitRepo, sha: string, mergedBranch: string) => void;
  /** The "Keep the conflicts" path never resolves to a normal merge result
   *  — this tells the caller to refresh status/log so the paused merge's
   *  conflicts show up right away instead of waiting for the next poll. */
  onConflictKept: () => void;
}) {
  const [branch, setBranch] = useState('');
  const [noFf, setNoFf] = useState(false);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string[] | null>(null);

  useEffect(() => {
    if (!open) return;
    setBranch(initialBranch ?? '');
    setNoFf(false);
    setMessage('');
    setError(null);
    setConflict(null);
  }, [open, initialBranch]);

  const options = [
    ...(branches?.local ?? []).filter((b) => !b.is_current).map((b) => ({ value: b.name, label: b.name })),
    ...(branches?.remote ?? []).map((b) => ({ value: b.name, label: t('{name} (remote)', { name: b.name }) })),
  ];

  const submit = (keepConflicts: boolean) => {
    if (!branch) return;
    setBusy(true);
    setError(null);
    merge(repoId, { branch, ff: noFf ? 'no' : 'auto', message: message.trim() || undefined, keepConflicts })
      .then((res) => {
        onMerged(res.repo, res.sha, branch);
        onOpenChange(false);
      })
      .catch((e: unknown) => {
        if (e instanceof GitApiError && e.errorClass === 'git.merge_conflict') {
          const payload = e.payload as unknown as GitMergeConflictPayload;
          const files = Array.isArray(payload.conflicts) ? payload.conflicts : [];
          if (keepConflicts) {
            onConflictKept();
            onOpenChange(false);
            return;
          }
          setConflict(files);
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
      title={t('Merge into {branch}', { branch: currentBranch ?? t('current branch') })}
      testId="merge-dialog"
      footer={
        conflict ? (
          <>
            <Button variant="ghost" size="sm" label={t('Abort')} onClick={() => onOpenChange(false)} testId="merge-conflict-abort" />
            <Button
              variant="primary"
              size="sm"
              label={t('Keep the conflicts to resolve them')}
              loading={busy}
              onClick={() => submit(true)}
              testId="merge-keep-conflicts"
            />
          </>
        ) : (
          <>
            <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => onOpenChange(false)} />
            <Button
              variant="primary"
              size="sm"
              label={t('Merge')}
              loading={busy}
              disabled={!branch}
              onClick={() => submit(false)}
              testId="merge-submit"
            />
          </>
        )
      }
    >
      {conflict ? (
        <div className="fs-notice" data-tone="danger" role="alert" data-testid="merge-conflict-error">
          <p>{t('Merging {branch} produced conflicts.', { branch })}</p>
          <ul className="fs-sc__dirty-list">
            {conflict.map((path) => (
              <li key={path}>{path}</li>
            ))}
          </ul>
          <p className="fs-muted">
            {t('The merge was aborted automatically — nothing changed. "Keep the conflicts" pauses the merge instead so you can resolve them yourself.')}
          </p>
        </div>
      ) : (
        <>
          <Field label={t('Merge branch')} htmlFor="merge-branch">
            <Select id="merge-branch" value={branch} onChange={setBranch} options={options} allowEmpty={t('Choose a branch…')} />
          </Field>
          <Toggle id="merge-no-ff" checked={noFf} onChange={setNoFf} label={t('Always create a merge commit')} />
          <Field label={t('Message (optional)')} htmlFor="merge-message">
            <FieldText id="merge-message" value={message} onChange={setMessage} placeholder={t('Merge commit message')} />
          </Field>
          {error && (
            <p className="fs-notice" data-tone="danger" role="alert" data-testid="merge-error">
              {error}
            </p>
          )}
        </>
      )}
    </Dialog>
  );
}
