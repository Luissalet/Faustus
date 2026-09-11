import { useEffect, useState } from 'react';
import { Button, Dialog } from '../../components';
import { createBranch, GitApiError, type GitBranchesResponse, type GitRepo } from '../../adapters/git';
import { Field, Select, Toggle, Text as FieldText } from '../settings/fields';
import { t } from '../../i18n';

/**
 * OBJ-4 / Lote 83 — "que se puedan crear... ramas fácil sin movidas, con un
 * botón y eligiendo nombre y de dónde vienen y ya": one dialog (name +
 * "from" + "switch to it") replaces `BranchPopover`'s inline text field —
 * same `POST /branches` call, just with a "from" a person can actually see
 * and pick from instead of only ever branching off whatever HEAD happened
 * to be.
 */
export function CreateBranchDialog({
  open,
  onOpenChange,
  repoId,
  branches,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  repoId: string;
  branches: GitBranchesResponse | null;
  onCreated: (repo: GitRepo) => void;
}) {
  const [name, setName] = useState('');
  const [from, setFrom] = useState('HEAD');
  const [switchTo, setSwitchTo] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; dirty: string[] } | null>(null);

  useEffect(() => {
    if (!open) return;
    setName('');
    setFrom('HEAD');
    setSwitchTo(true);
    setError(null);
  }, [open]);

  const submit = () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    setBusy(true);
    setError(null);
    createBranch(repoId, { name: trimmed, startPoint: from === 'HEAD' ? undefined : from, checkout: switchTo })
      .then((res) => {
        onCreated(res.repo);
        onOpenChange(false);
      })
      .catch((e: unknown) => {
        if (e instanceof GitApiError && e.errorClass === 'git.dirty') {
          const dirty = Array.isArray(e.payload.dirty) ? (e.payload.dirty as unknown[]).filter((p): p is string => typeof p === 'string') : [];
          setError({ message: e.message, dirty });
        } else {
          setError({ message: (e as Error).message, dirty: [] });
        }
      })
      .finally(() => setBusy(false));
  };

  const fromOptions = [
    { value: 'HEAD', label: t('Current commit') },
    ...(branches?.local ?? []).map((b) => ({ value: b.name, label: b.name })),
    ...(branches?.remote ?? []).map((b) => ({ value: b.name, label: t('{name} (remote)', { name: b.name }) })),
  ];

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={t('New branch')}
      testId="create-branch-dialog"
      footer={
        <>
          <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => onOpenChange(false)} />
          <Button variant="primary" size="sm" label={t('Create')} loading={busy} disabled={!name.trim()} onClick={submit} testId="create-branch-submit" />
        </>
      }
    >
      <Field label={t('Name')} htmlFor="create-branch-name">
        <FieldText id="create-branch-name" value={name} onChange={setName} placeholder={t('New branch name')} />
      </Field>
      <Field label={t('From')} htmlFor="create-branch-from">
        <Select id="create-branch-from" value={from} onChange={setFrom} options={fromOptions} />
      </Field>
      <Toggle id="create-branch-switch" checked={switchTo} onChange={setSwitchTo} label={t('Switch to it')} />
      {error && (
        <div className="fs-notice" data-tone="danger" role="alert" data-testid="create-branch-error">
          <p>{error.message}</p>
          {error.dirty.length > 0 && (
            <ul className="fs-sc__dirty-list">
              {error.dirty.map((path) => (
                <li key={path}>{path}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </Dialog>
  );
}
