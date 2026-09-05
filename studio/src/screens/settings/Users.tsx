import { ChevronDown, Plus, UserCog } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button, EmptyState, Skeleton } from '../../components';
import { authStatus, createUser, deleteUser, listUsers, PRIV_LABELS, renameUser, setOpenSignup, setPrivileges, setUserAdmin, type Privileges, type User } from '../../adapters/account';
import { listEndpoints, loadSettings, saveSettings, type ModelEndpoint } from '../../adapters/settings';
import { t } from '../../i18n';
import { Field, Toggle } from './fields';

/** Users: registration, the shared defaults, each account with its privileges, and a new one. Admin only. */
export function UsersSection({ say }: { say: (t: string) => void }) {
  const [users, setUsers] = useState<User[] | null>(null);
  const [denied, setDenied] = useState(false);
  const [signup, setSignup] = useState(false);
  const [share, setShare] = useState(false);
  const [endpoints, setEndpoints] = useState<ModelEndpoint[]>([]);

  const reload = () =>
    listUsers()
      .then(setUsers)
      .catch(() => {
        setDenied(true);
        setUsers([]);
      });
  useEffect(() => {
    void reload();
    authStatus().then((s) => setSignup(!!s.signup_enabled)).catch(() => {});
    loadSettings().then((s) => setShare(s.share_defaults_with_users === true)).catch(() => {});
    listEndpoints().then(setEndpoints).catch(() => {});
  }, []);

  if (denied) return <EmptyState icon={UserCog} title={t('Administrators only')} body={t('This account cannot manage users.')} />;

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-users">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-users" className="fs-set__title">{t('Users')}</h2>
          <p className="fs-prose">{t('The accounts of this installation, what each may use, and whether anyone can sign up.')}</p>
        </div>
      </header>
      <div className="fs-set__card">
        <Field label={t('Open signup')} htmlFor="u-signup" help={t('Anyone can create an account from the login page.')}>
          <Toggle
            id="u-signup"
            checked={signup}
            onChange={(v) => {
              setSignup(v);
              setOpenSignup(v)
                .then(setSignup)
                .catch(() => setSignup(!v));
            }}
          />
        </Field>
        <Field label={t('Share the defaults with the other users')} htmlFor="u-share" help={t('Users without a default of their own inherit the global default model, when it is allowed for them.')}>
          <Toggle
            id="u-share"
            checked={share}
            onChange={(v) => {
              setShare(v);
              saveSettings({ share_defaults_with_users: v }).catch(() => setShare(!v));
            }}
          />
        </Field>
      </div>

      <div className="fs-set__card">
        <h3 className="fs-set__card-title">{t('Accounts')}</h3>
        {users === null ? (
          <Skeleton label={t('Loading')} count={3} height="44px" />
        ) : users.length === 0 ? (
          <p className="fs-set__help">{t('No users.')}</p>
        ) : (
          <ul className="fs-users">
            {users.map((u) => (
              <UserRow key={u.username} user={u} endpoints={endpoints} onChanged={reload} say={say} />
            ))}
          </ul>
        )}
      </div>

      <NewUser onCreated={reload} say={say} />
    </section>
  );
}

function UserRow({ user, endpoints, onChanged, say }: { user: User; endpoints: ModelEndpoint[]; onChanged: () => void; say: (t: string) => void }) {
  const [open, setOpen] = useState(false);
  const [priv, setPriv] = useState<Privileges>(user.privileges ?? {});
  useEffect(() => setPriv(user.privileges ?? {}), [user]);

  const patch = (p: Partial<Privileges>) => {
    setPriv((cur) => ({ ...cur, ...p }));
    setPrivileges(user.username, p).then(setPriv).catch(() => say(t('Could not update the privilege.')));
  };

  const allModels = endpoints.flatMap((ep) => (ep.models ?? []).map((mid) => ({ mid, ep: ep.name })));
  const allowed = new Set(priv.allowed_models ?? []);
  const restricted = !!priv.allowed_models_restricted;
  const blockAll = !!priv.block_all_models;
  const isOn = (mid: string) => (blockAll ? false : !restricted ? true : allowed.has(mid));
  const setModels = (checked: string[]) => {
    // all → no restriction; none → block everything; some → allowlist (as the previous interface did)
    const all = checked.length === allModels.length;
    const none = checked.length === 0;
    patch({ allowed_models: all || none ? [] : checked, allowed_models_restricted: !all && !none, block_all_models: none });
  };
  const modelsHint = blockAll ? t('No model allowed') : !restricted ? t('Every model allowed (no restriction)') : t('{n} model(s) allowed', { n: allowed.size });

  return (
    <li className="fs-users__row" data-open={open || undefined}>
      <div className="fs-users__head">
        <span className="fs-set__avatar" aria-hidden="true">
          {user.username.charAt(0).toUpperCase()}
        </span>
        <span className="fs-users__name">
          <strong>{user.username}</strong>
          {user.is_admin && <span className="fs-users__badge">{t('admin')}</span>}
        </span>
        <span className="fs-users__actions">
          <Button
            size="sm"
            variant="ghost"
            label={user.is_admin ? t('Revoke admin') : t('Make admin')}
            onClick={() => {
              const q = user.is_admin ? t('Revoke admin from "{name}"?', { name: user.username }) : t('Make "{name}" an administrator? They will see and change everything.', { name: user.username });
              if (!window.confirm(q)) return;
              setUserAdmin(user.username, !user.is_admin).then(onChanged).catch((e: Error) => say(e.message));
            }}
          />
          <Button
            size="sm"
            variant="ghost"
            label={t('Rename')}
            onClick={() => {
              const next = window.prompt(t('New username for "{name}":', { name: user.username }), user.username)?.trim();
              if (!next || next === user.username) return;
              renameUser(user.username, next).then(onChanged).catch((e: Error) => say(e.message));
            }}
          />
          {!user.is_admin && (
            <Button
              size="sm"
              variant="danger"
              label={t('Remove')}
              onClick={() => {
                if (!window.confirm(t('Remove the user "{name}"?', { name: user.username }))) return;
                deleteUser(user.username).then(onChanged).catch((e: Error) => say(e.message));
              }}
            />
          )}
          {!user.is_admin && (
            <button type="button" className="fs-users__toggle" aria-expanded={open} aria-label={t('Privileges of {name}', { name: user.username })} onClick={() => setOpen((o) => !o)}>
              <ChevronDown size={14} aria-hidden="true" />
            </button>
          )}
        </span>
      </div>
      {open && !user.is_admin && (
        <div className="fs-users__priv">
          <h4 className="fs-users__h">{t('Features')}</h4>
          {PRIV_LABELS.map((p) => (
            <Field key={p.key} label={t(p.label)} htmlFor={`priv-${user.username}-${p.key}`}>
              <Toggle id={`priv-${user.username}-${p.key}`} checked={priv[p.key] === true} onChange={(v) => patch({ [p.key]: v })} />
            </Field>
          ))}
          <h4 className="fs-users__h">{t('Limits')}</h4>
          <Field label={t('Daily message limit')} htmlFor={`priv-${user.username}-max`} help={t('0 = no limit')}>
            <input
              id={`priv-${user.username}-max`}
              type="number"
              min={0}
              className="fs-field fs-field--short"
              value={priv.max_messages_per_day ?? 0}
              onChange={(e) => setPriv((c) => ({ ...c, max_messages_per_day: Number(e.target.value) || 0 }))}
              onBlur={(e) => patch({ max_messages_per_day: Number(e.target.value) || 0 })}
            />
          </Field>
          <h4 className="fs-users__h">
            {t('Allowed models')}
            <span className="fs-users__h-actions">
              <button type="button" className="fs-link" onClick={() => setModels(allModels.map((m) => m.mid))}>
                {t('All')}
              </button>
              <button type="button" className="fs-link" onClick={() => setModels([])}>
                {t('None')}
              </button>
            </span>
          </h4>
          <p className="fs-set__help">{modelsHint}</p>
          {allModels.length === 0 ? (
            <p className="fs-set__help">{t('No models available.')}</p>
          ) : (
            <ul className="fs-users__models">
              {allModels.map((m) => (
                <li key={m.mid}>
                  <label className="fs-check">
                    <input
                      type="checkbox"
                      checked={isOn(m.mid)}
                      onChange={(e) => {
                        const next = allModels.filter((x) => (x.mid === m.mid ? e.target.checked : isOn(x.mid))).map((x) => x.mid);
                        setModels(next);
                      }}
                    />
                    <span>
                      {m.mid.split('/').pop()} <span className="fs-set__help">{m.ep}</span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </li>
  );
}

function NewUser({ onCreated, say }: { onCreated: () => void; say: (t: string) => void }) {
  const [name, setName] = useState('');
  const [pw, setPw] = useState('');
  const [admin, setAdmin] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <div className="fs-set__card">
      <h3 className="fs-set__card-title">{t('Add a user')}</h3>
      <div className="fs-set__grid2">
        <Field label={t('Username')} htmlFor="nu-name">
          <input id="nu-name" className="fs-field" value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" />
        </Field>
        <Field label={t('Password')} htmlFor="nu-pw">
          <input id="nu-pw" type="password" className="fs-field" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="new-password" />
        </Field>
      </div>
      <Field label={t('Administrator')} htmlFor="nu-admin" help={t('Full access to everything.')}>
        <Toggle id="nu-admin" checked={admin} onChange={setAdmin} />
      </Field>
      <div className="fs-set__row-end">
        {err && <span className="fs-set__err">{err}</span>}
        <Button
          variant="primary"
          size="sm"
          icon={Plus}
          label={t('Add the user')}
          loading={busy}
          disabled={!name.trim() || !pw}
          onClick={() => {
            setBusy(true);
            setErr(null);
            createUser(name.trim(), pw, admin)
              .then(() => {
                setName('');
                setPw('');
                setAdmin(false);
                say(t('User created.'));
                onCreated();
              })
              .catch((e: Error) => setErr(e.message))
              .finally(() => setBusy(false));
          }}
        />
      </div>
    </div>
  );
}
