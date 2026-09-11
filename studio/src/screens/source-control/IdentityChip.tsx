import { Github, KeyRound, Plus, RefreshCw } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button, IconButton, Popover } from '../../components';
import {
  createIdentity,
  getRepoIdentity,
  GitApiError,
  identityChipLabel,
  listIdentities,
  probeIdentity,
  rewriteRemoteToAlias,
  setRepoIdentity,
  type GitIdentitiesResponse,
  type GitIdentity,
  type GitRepo,
  type RepoIdentity,
} from '../../adapters/git';
import { Field, str, Text, Toggle } from '../settings/fields';
import { t } from '../../i18n';

/**
 * OBJ-4 / Lote 83 — the repo header's identity chip: "who am I pushing as,
 * right now" (CONTRATO_GIT_2.md: "que salga explícitamente cuál es la
 * cuenta logeada... y una forma de cambiarla fácil con un selector"). Loads
 * lazily on open, same reason `BranchPopover` does — up to 60 repos on
 * screen at once must never mean 60 eager `/identity` calls.
 */
export function IdentityChip({ repo, onRepoUpdate }: { repo: GitRepo; onRepoUpdate: (repo: GitRepo) => void }) {
  const [open, setOpen] = useState(false);
  const [identities, setIdentities] = useState<GitIdentitiesResponse | null>(null);
  const [repoIdentity, setRepoIdentityState] = useState<RepoIdentity | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [probing, setProbing] = useState<string | null>(null);

  const [adding, setAdding] = useState(false);
  const [addForm, setAddForm] = useState({ label: '', sshHost: '', identityFile: '', gitUserName: '', gitUserEmail: '', alsoUser: false });
  const [addBusy, setAddBusy] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [confirmSetUser, setConfirmSetUser] = useState(true);
  const [applying, setApplying] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);

  const load = () => {
    setLoadError(null);
    Promise.all([listIdentities(), getRepoIdentity(repo.id)])
      .then(([ids, ri]) => {
        setIdentities(ids);
        setRepoIdentityState(ri);
      })
      .catch((e: unknown) => setLoadError((e as Error).message));
  };

  useEffect(() => {
    if (!open) return;
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, repo.id]);

  const reset = () => {
    setOpen(false);
    setAdding(false);
    setAddForm({ label: '', sshHost: '', identityFile: '', gitUserName: '', gitUserEmail: '', alsoUser: false });
    setAddError(null);
    setConfirmId(null);
    setApplyError(null);
  };

  const probe = (id: string) => {
    setProbing(id);
    probeIdentity(id)
      .then(load)
      .catch(() => {})
      .finally(() => setProbing(null));
  };

  const submitAdd = () => {
    if (!addForm.label.trim() || !addForm.identityFile.trim()) return;
    setAddBusy(true);
    setAddError(null);
    createIdentity({
      label: addForm.label.trim(),
      sshHost: addForm.sshHost.trim() || undefined,
      identityFile: addForm.identityFile.trim(),
      gitUserName: addForm.alsoUser ? addForm.gitUserName.trim() || undefined : undefined,
      gitUserEmail: addForm.alsoUser ? addForm.gitUserEmail.trim() || undefined : undefined,
    })
      .then(() => {
        setAdding(false);
        setAddForm({ label: '', sshHost: '', identityFile: '', gitUserName: '', gitUserEmail: '', alsoUser: false });
        load();
      })
      .catch((e: unknown) => setAddError((e as Error).message))
      .finally(() => setAddBusy(false));
  };

  const confirmedIdentity = identities?.identities.find((i) => i.id === confirmId) ?? null;
  const beforeUrl = repoIdentity?.remote_url ?? '';
  // A `gh` identity has no ssh host alias of its own (CONTRATO_GIT_3.md:
  // "ssh_host: null" — it rewrites straight onto `github.com`, never onto
  // its login, which is what falling back to `.label` would wrongly do.
  const previewAlias = confirmedIdentity
    ? confirmedIdentity.source === 'gh'
      ? 'github.com'
      : confirmedIdentity.ssh_host ?? confirmedIdentity.label
    : null;
  const afterUrl = previewAlias ? rewriteRemoteToAlias(beforeUrl, previewAlias) : null;

  const apply = () => {
    if (!confirmId) return;
    setApplying(true);
    setApplyError(null);
    setRepoIdentity(repo.id, { identityId: confirmId, setGitUser: confirmSetUser })
      .then((res) => {
        onRepoUpdate(res.repo);
        setConfirmId(null);
        load();
      })
      .catch((e: unknown) => {
        if (e instanceof GitApiError) setApplyError(`${e.message}${str(e.payload.detail) ? ` — ${str(e.payload.detail)}` : ''}`);
        else setApplyError((e as Error).message);
      })
      .finally(() => setApplying(false));
  };

  const chip = identityChipLabel(repo.identity);
  // Suggested keys: every discovered identity's key file, deduplicated —
  // "con lista de claves encontradas en ~/.ssh como sugerencias".
  const suggestedKeys = [...new Set((identities?.identities ?? []).map((i) => i.identity_file).filter(Boolean))];

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) reset();
      }}
      className="fs-sc__identity-pop"
      testId="identity-popover"
      trigger={
        <button type="button" className="fs-chip" data-on={open || undefined} data-testid="identity-chip">
          <KeyRound size={13} aria-hidden="true" />
          {t(chip.key, chip.values)}
        </button>
      }
    >
      {loadError && (
        <p className="fs-notice" data-tone="danger" role="alert">
          {loadError}
        </p>
      )}

      {!identities && !loadError && <p className="fs-muted">{t('Loading identities…')}</p>}

      {identities && (
        <div className="fs-sc__identity-list" role="radiogroup" aria-label={t('SSH identities')}>
          {identities.identities.length === 0 && <p className="fs-muted">{t('No SSH identities found yet.')}</p>}
          {identities.identities.map((id) => (
            <div key={id.id} className="fs-sc__identity-row" data-active={repoIdentity?.active?.id === id.id || undefined}>
              <label className="fs-inline">
                <input
                  type="radio"
                  name="identity-pick"
                  checked={confirmId === id.id || (confirmId === null && repoIdentity?.active?.id === id.id)}
                  onChange={() => setConfirmId(id.id)}
                  data-testid="identity-radio"
                />
                <span className="fs-sc__identity-label">
                  {id.source === 'gh' ? <Github size={13} aria-hidden="true" /> : <KeyRound size={13} aria-hidden="true" />}
                  {id.label}
                  {id.github_login ? ` ${t('(login: {login})', { login: id.github_login })}` : ''}
                  <span className="fs-sc__branch-tag">
                    {id.source === 'gh' ? t('GitHub CLI') : id.source === 'ssh_config' ? t('ssh config') : t('manual')}
                  </span>
                </span>
              </label>
              <IconButton icon={RefreshCw} label={t('Refresh login for {label}', { label: id.label })} size="sm" disabled={probing === id.id} onClick={() => probe(id.id)} />
            </div>
          ))}
        </div>
      )}

      {confirmId && confirmedIdentity && (
        <div className="fs-sc__identity-confirm" data-testid="identity-apply-confirm">
          <p className="fs-set__help">{t('Remote URL before')}: <code>{beforeUrl || t('(none)')}</code></p>
          <p className="fs-set__help">{t('Remote URL after')}: <code>{afterUrl ?? t('(cannot preview — unrecognized remote)')}</code></p>
          {(confirmedIdentity.git_user_name || confirmedIdentity.git_user_email) && (
            <Toggle
              id="identity-also-user"
              checked={confirmSetUser}
              onChange={setConfirmSetUser}
              label={t('Also set user.name/user.email for this repository')}
            />
          )}
          {applyError && (
            <p className="fs-notice" data-tone="danger" role="alert">
              {applyError}
            </p>
          )}
          <div className="fs-inline">
            <Button size="sm" variant="ghost" label={t('Cancel')} onClick={() => setConfirmId(null)} />
            <Button size="sm" variant="primary" label={t('Apply to this repo')} loading={applying} onClick={apply} testId="identity-apply" />
          </div>
        </div>
      )}

      <div className="fs-sc__identity-add">
        {adding ? (
          <div className="fs-sc__identity-add-form">
            <Field label={t('Label')} htmlFor="identity-add-label">
              <Text id="identity-add-label" value={addForm.label} onChange={(v) => setAddForm((f) => ({ ...f, label: v }))} placeholder={t('e.g. Work GitHub')} />
            </Field>
            <Field label={t('Host alias')} htmlFor="identity-add-host" help={t('The Host entry in ~/.ssh/config; leave empty for github.com directly.')}>
              <Text id="identity-add-host" value={addForm.sshHost} onChange={(v) => setAddForm((f) => ({ ...f, sshHost: v }))} placeholder="github-work" />
            </Field>
            <Field label={t('Key file')} htmlFor="identity-add-key">
              <input
                id="identity-add-key"
                className="fs-field"
                list="identity-key-suggestions"
                value={addForm.identityFile}
                onChange={(e) => setAddForm((f) => ({ ...f, identityFile: e.target.value }))}
                placeholder="~/.ssh/id_ed25519"
              />
              <datalist id="identity-key-suggestions">
                {suggestedKeys.map((k) => (
                  <option key={k} value={k} />
                ))}
              </datalist>
            </Field>
            <Toggle id="identity-add-also-user" checked={addForm.alsoUser} onChange={(v) => setAddForm((f) => ({ ...f, alsoUser: v }))} label={t('Also set user.name/user.email')} />
            {addForm.alsoUser && (
              <>
                <Field label={t('user.name')} htmlFor="identity-add-name">
                  <Text id="identity-add-name" value={addForm.gitUserName} onChange={(v) => setAddForm((f) => ({ ...f, gitUserName: v }))} />
                </Field>
                <Field label={t('user.email')} htmlFor="identity-add-email">
                  <Text id="identity-add-email" value={addForm.gitUserEmail} onChange={(v) => setAddForm((f) => ({ ...f, gitUserEmail: v }))} />
                </Field>
              </>
            )}
            {addError && (
              <p className="fs-notice" data-tone="danger" role="alert">
                {addError}
              </p>
            )}
            <div className="fs-inline">
              <Button size="sm" variant="ghost" label={t('Cancel')} onClick={() => setAdding(false)} />
              <Button
                size="sm"
                variant="primary"
                label={t('Save identity')}
                loading={addBusy}
                disabled={!addForm.label.trim() || !addForm.identityFile.trim()}
                onClick={submitAdd}
              />
            </div>
          </div>
        ) : (
          <Button size="sm" variant="ghost" icon={Plus} label={t('Add identity…')} onClick={() => setAdding(true)} testId="identity-add-open" />
        )}
      </div>
    </Popover>
  );
}
