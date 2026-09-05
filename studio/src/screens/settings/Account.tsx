import { LogOut, ShieldCheck } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button, Skeleton } from '../../components';
import { authPolicy, authStatus, changePassword, logout, safeDataImage, tfaConfirm, tfaDisable, tfaSetup, tfaStatus, type AuthStatus } from '../../adapters/account';
import { t } from '../../i18n';
import { Field } from './fields';

/** Account: who is signed in, the password, two-factor. Same routes as the previous interface's Account tab. */
export function AccountSection({ say }: { say: (t: string) => void }) {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [minLen, setMinLen] = useState(8);
  useEffect(() => {
    authStatus().then(setStatus).catch(() => setStatus({}));
    authPolicy().then((p) => setMinLen(p.password_min_length || 8)).catch(() => {});
  }, []);

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-account">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-account" className="fs-set__title">{t('Account')}</h2>
          <p className="fs-prose">{t('Who is signed in on this browser, the password and the second factor.')}</p>
        </div>
      </header>

      {status === null ? (
        <Skeleton label={t('Loading')} count={2} height="48px" />
      ) : (
        <div className="fs-set__card fs-set__who">
          <span className="fs-set__avatar" aria-hidden="true">
            {(status.username ?? '?').charAt(0).toUpperCase()}
          </span>
          <span className="fs-set__who-text">
            <strong>{status.username ?? t('Unknown')}</strong>
            <span className="fs-set__help">{status.auth_enabled === false ? t('Sign-in is off: everyone is the owner.') : status.is_admin ? t('Administrator') : t('User')}</span>
          </span>
          <Button variant="danger" size="sm" icon={LogOut} label={t('Log out')} onClick={() => void logout()} testId="logout" />
        </div>
      )}

      <PasswordCard minLen={minLen} say={say} />
      <TwoFactorCard say={say} />
    </section>
  );
}

function PasswordCard({ minLen, say }: { minLen: number; say: (t: string) => void }) {
  const [cur, setCur] = useState('');
  const [next, setNext] = useState('');
  const [conf, setConf] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setErr(null);
    if (!cur || !next) return setErr(t('Fill in all the fields.'));
    if (next.length < minLen) return setErr(t('At least {n} characters.', { n: minLen }));
    if (next !== conf) return setErr(t('The passwords do not match.'));
    setBusy(true);
    try {
      await changePassword(cur, next);
      setCur('');
      setNext('');
      setConf('');
      say(t('Password updated.'));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="fs-set__card">
      <h3 className="fs-set__card-title">{t('Password')}</h3>
      <Field label={t('Current password')} htmlFor="pw-cur">
        <input id="pw-cur" type="password" className="fs-field" value={cur} onChange={(e) => setCur(e.target.value)} autoComplete="current-password" />
      </Field>
      <Field label={t('New password')} htmlFor="pw-new" help={t('At least {n} characters.', { n: minLen })}>
        <input id="pw-new" type="password" className="fs-field" value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" />
      </Field>
      <Field label={t('Repeat the new password')} htmlFor="pw-conf">
        <input id="pw-conf" type="password" className="fs-field" value={conf} onChange={(e) => setConf(e.target.value)} autoComplete="new-password" />
      </Field>
      <div className="fs-set__row-end">
        {err && <span className="fs-set__err">{err}</span>}
        <Button variant="primary" size="sm" label={t('Update the password')} loading={busy} onClick={() => void submit()} />
      </div>
    </div>
  );
}

type TfaView = { kind: 'loading' } | { kind: 'off' } | { kind: 'on' } | { kind: 'setup'; secret: string; qr: string } | { kind: 'done'; codes: string[] } | { kind: 'error' };

function TwoFactorCard({ say }: { say: (t: string) => void }) {
  const [view, setView] = useState<TfaView>({ kind: 'loading' });
  const [pw, setPw] = useState('');
  const [code, setCode] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const load = () => {
    setErr(null);
    tfaStatus()
      .then((s) => setView({ kind: s.enabled ? 'on' : 'off' }))
      .catch(() => setView({ kind: 'error' }));
  };
  useEffect(load, []);

  const run = async (fn: () => Promise<void>) => {
    setErr(null);
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fs-set__card">
      <h3 className="fs-set__card-title">
        <ShieldCheck size={14} aria-hidden="true" /> {t('Two-factor authentication')}
      </h3>
      {view.kind === 'loading' && <Skeleton label={t('Loading')} count={1} height="40px" />}
      {view.kind === 'error' && <p className="fs-set__help">{t('Could not read the two-factor status.')}</p>}
      {view.kind === 'off' && (
        <>
          <p className="fs-set__help">{t('An extra layer with an authenticator app (Aegis, Google Authenticator and the like).')}</p>
          <div className="fs-set__row-end">
            {err && <span className="fs-set__err">{err}</span>}
            <Button
              variant="primary"
              size="sm"
              label={t('Set up two-factor')}
              loading={busy}
              onClick={() =>
                void run(async () => {
                  const s = await tfaSetup();
                  setCode('');
                  setView({ kind: 'setup', secret: s.secret, qr: safeDataImage(s.qr_code) });
                })
              }
            />
          </div>
        </>
      )}
      {view.kind === 'setup' && (
        <>
          {view.qr && <img className="fs-set__qr" src={view.qr} alt={t('QR code for the authenticator app')} />}
          <p className="fs-set__help">{t('Scan it with the authenticator app, or type the secret by hand:')}</p>
          <code className="fs-set__secret">{view.secret}</code>
          <Field label={t('Six-digit code')} htmlFor="tfa-code">
            <input id="tfa-code" className="fs-field" value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" autoComplete="one-time-code" maxLength={8} />
          </Field>
          <div className="fs-set__row-end">
            {err && <span className="fs-set__err">{err}</span>}
            <Button variant="ghost" size="sm" label={t('Cancel')} onClick={load} />
            <Button
              variant="primary"
              size="sm"
              label={t('Verify and enable')}
              loading={busy}
              onClick={() =>
                void run(async () => {
                  if (!code.trim()) throw new Error(t('Enter the code.'));
                  const r = await tfaConfirm(code.trim());
                  setView({ kind: 'done', codes: r.backup_codes ?? [] });
                  say(t('Two-factor enabled.'));
                })
              }
            />
          </div>
        </>
      )}
      {view.kind === 'done' && (
        <>
          <p className="fs-set__help">{t('Two-factor is on. Keep these backup codes somewhere safe; each works once if you lose the authenticator:')}</p>
          <ul className="fs-set__codes">
            {view.codes.map((c) => (
              <li key={c}>
                <code>{c}</code>
              </li>
            ))}
          </ul>
          <div className="fs-set__row-end">
            <Button variant="primary" size="sm" label={t('Done')} onClick={load} />
          </div>
        </>
      )}
      {view.kind === 'on' && (
        <>
          <p className="fs-set__help">
            <strong>{t('Enabled.')}</strong> {t('The authenticator app is required on login.')}
          </p>
          <Field label={t('Password (to disable it)')} htmlFor="tfa-pw">
            <input id="tfa-pw" type="password" className="fs-field" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="current-password" />
          </Field>
          <div className="fs-set__row-end">
            {err && <span className="fs-set__err">{err}</span>}
            <Button
              variant="danger"
              size="sm"
              label={t('Disable two-factor')}
              loading={busy}
              onClick={() =>
                void run(async () => {
                  if (!pw) throw new Error(t('Enter your password.'));
                  await tfaDisable(pw);
                  setPw('');
                  say(t('Two-factor disabled.'));
                  load();
                })
              }
            />
          </div>
        </>
      )}
    </div>
  );
}
