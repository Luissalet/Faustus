import { useEffect, useState } from 'react';
import { Button, Dialog, Skeleton } from '../../components';
import { getRepoPolicy, setRepoPolicy, type AgentGitPolicy, type GitRepo, type RepoPolicyResponse } from '../../adapters/git';
import { Toggle } from '../settings/fields';
import { AgentPolicyFields } from './AgentPolicyFields';
import { t } from '../../i18n';

/**
 * OBJ-4 / Lote 83 — "Agent & this repository": the per-repo override of
 * the global agent git policy (Settings → Repositories has the global
 * twin). `{"inherit": true}` clears the override server-side
 * (CONTRATO_GIT_2.md) rather than this screen having to know what the
 * global values currently are to "reset" to them.
 */
export function RepoPolicyDialog({
  open,
  onOpenChange,
  repo,
  onPolicyChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  repo: GitRepo;
  onPolicyChange: (policy: RepoPolicyResponse) => void;
}) {
  const [loading, setLoading] = useState(false);
  const [useGlobal, setUseGlobal] = useState(true);
  const [effective, setEffective] = useState<AgentGitPolicy | null>(null);
  const [draft, setDraft] = useState<AgentGitPolicy | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setError(null);
    getRepoPolicy(repo.id)
      .then((p) => {
        setUseGlobal(!p.overridden);
        setEffective(p.effective);
        setDraft(p.effective);
      })
      .catch((e: unknown) => setError((e as Error).message))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, repo.id]);

  const save = () => {
    setSaving(true);
    setError(null);
    const body: AgentGitPolicy | { inherit: true } = useGlobal ? { inherit: true } : (draft as AgentGitPolicy);
    setRepoPolicy(repo.id, body)
      .then((p) => {
        onPolicyChange(p);
        onOpenChange(false);
      })
      .catch((e: unknown) => setError((e as Error).message))
      .finally(() => setSaving(false));
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={t('Agent & this repository')}
      description={t('What the agent may do on its own in this repository, on top of the global default.')}
      testId="repo-policy-dialog"
      footer={
        <>
          <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => onOpenChange(false)} />
          <Button variant="primary" size="sm" label={t('Save')} loading={saving} disabled={loading} onClick={save} testId="repo-policy-save" />
        </>
      }
    >
      {loading && <Skeleton label={t('Loading')} count={3} height="48px" />}
      {!loading && (
        <>
          <Toggle id="repo-policy-use-global" checked={useGlobal} onChange={setUseGlobal} label={t('Use the global policy')} />
          {(draft || effective) && (
            <AgentPolicyFields idPrefix="repo-policy" value={useGlobal ? (effective as AgentGitPolicy) : (draft as AgentGitPolicy)} onChange={setDraft} disabled={useGlobal} />
          )}
        </>
      )}
      {error && (
        <p className="fs-notice" data-tone="danger" role="alert" data-testid="repo-policy-error">
          {error}
        </p>
      )}
    </Dialog>
  );
}
