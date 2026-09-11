import type { AgentGitPolicy } from '../../adapters/git';
import { pushToggleDisabled } from '../../adapters/git';
import { Field, Text, Toggle } from '../settings/fields';
import '../settings.css';
import './agent-policy.css';
import { t } from '../../i18n';

/**
 * OBJ-4 / Lote 83 — the three-checkmark form CONTRATO_GIT_2.md asks for,
 * shared by the global card (Settings → Repositories) and the per-repo
 * card: "checklist: el agente usa ramas aparte? commits? push? y
 * dependiendo de lo que haga crea ramas aparte, hace commits o no y hace
 * push o no, a preferencia del usuario." Push is disabled — not merely
 * unchecked — the instant commit is off (`pushToggleDisabled`, tested by
 * `l83-git-panel.check.mjs`), since a push with nothing committed by the
 * agent has no meaning to act on.
 */
export function AgentPolicyFields({
  value,
  onChange,
  disabled,
  idPrefix,
}: {
  value: AgentGitPolicy;
  onChange: (next: AgentGitPolicy) => void;
  disabled?: boolean;
  idPrefix: string;
}) {
  const set = <K extends keyof AgentGitPolicy>(key: K, v: AgentGitPolicy[K]) => {
    const next: AgentGitPolicy = { ...value, [key]: v };
    if (key === 'commit' && v === false) next.push = false;
    onChange(next);
  };
  const pushDisabled = disabled || pushToggleDisabled(value.commit);

  return (
    <div className="fs-gitpolicy" data-disabled={disabled || undefined}>
      <div className="fs-gitpolicy__check">
        <Toggle
          id={`${idPrefix}-branch`}
          checked={value.use_branch}
          onChange={(v) => set('use_branch', v)}
          disabled={disabled}
          label={t('Work on a separate branch (faustus/…)')}
        />
        <p className="fs-set__help">{t('Before writing anything, the agent switches to its own branch instead of the one you had checked out.')}</p>
      </div>
      <div className="fs-gitpolicy__check">
        <Toggle
          id={`${idPrefix}-commit`}
          checked={value.commit}
          onChange={(v) => set('commit', v)}
          disabled={disabled}
          label={t('Commit what the agent changes at the end of each turn')}
        />
        <p className="fs-set__help">{t('Only the files it actually wrote, edited or deleted that turn — never everything in the tree. Does not require a separate branch.')}</p>
      </div>
      <div className="fs-gitpolicy__check">
        <Toggle
          id={`${idPrefix}-push`}
          checked={value.push}
          onChange={(v) => set('push', v)}
          disabled={pushDisabled}
          label={t('Push those commits')}
        />
        <p className="fs-set__help">
          {value.commit ? t('Sends each commit to the remote right after it is made.') : t('Needs "Commit" turned on above.')}
        </p>
      </div>
      <details className="fs-set__group">
        <summary className="fs-set__group-head">{t('Advanced')}</summary>
        <div className="fs-set__group-body">
          <Field label={t('Branch name prefix')} htmlFor={`${idPrefix}-branch-prefix`}>
            <Text id={`${idPrefix}-branch-prefix`} value={value.branch_prefix} onChange={(v) => set('branch_prefix', v)} disabled={disabled} placeholder="faustus/" />
          </Field>
          <Field label={t('Commit message prefix')} htmlFor={`${idPrefix}-commit-prefix`}>
            <Text id={`${idPrefix}-commit-prefix`} value={value.commit_message_prefix} onChange={(v) => set('commit_message_prefix', v)} disabled={disabled} placeholder="faustus: " />
          </Field>
          <Toggle
            id={`${idPrefix}-upstream`}
            checked={value.push_set_upstream}
            onChange={(v) => set('push_set_upstream', v)}
            disabled={pushDisabled}
            label={t('Set the upstream on the first push (-u)')}
          />
        </div>
      </details>
    </div>
  );
}
