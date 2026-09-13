import { AlertTriangle, Check, Copy, ExternalLink, Trash2, Upload } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Button, IconButton, Skeleton } from '../../components';
import {
  checkGoogleOAuthClient,
  deleteGoogleOAuthClient,
  getGoogleOAuthClient,
  saveGoogleOAuthClient,
  type GoogleOAuthClientStatus,
  type GoogleOAuthStep,
} from '../../adapters/google';
import { t } from '../../i18n';
import { Field } from './fields';
import { FormFoot, useMsg } from './IntegrationForms';

/**
 * G3.3: Settings › Integrations › Google — the wizard that replaces editing
 * `.env` by hand. Backend: `routes/google_oauth_routes.py` (G3.2), storage
 * and resolution in `src/google_oauth_client.py` (G3.1).
 *
 * Mounted from two places: `IntegrationsMore.tsx`'s `GoogleCalendarForm`
 * when `configured` is false, and as its own list entry in
 * `Integrations.tsx` once a client exists (edit/replace/remove). Both pass
 * `onSaved` so the caller can refresh its own `configured` flag without a
 * page reload.
 */

const STEP_TEXT: Record<GoogleOAuthStep, string> = {
  project: 'Open Google Cloud Console and create (or pick) a project.',
  enable_calendar_api: 'APIs & Services → Library → enable the Google Calendar API (and the Gmail API too, if you also want to connect email).',
  consent_screen: 'APIs & Services → OAuth consent screen → User Type: External.',
  test_user: 'Publishing status: Testing, then add your own Google account under Test users. Testing refresh tokens expire after 7 days — publishing without verification is fine for personal use.',
  credentials: 'Credentials → Create Credentials → OAuth client ID → Application type: Web application. Paste the redirect URIs below into Authorized redirect URIs.',
  paste: 'Copy the Client ID and Client secret back here — or drop the client_secret_*.json file Google offers to download.',
};

function UriRow({ label, url, say }: { label: string; url: string; say: (t: string) => void }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      say(t('Could not copy — select it and copy by hand.'));
    }
  };
  return (
    <div className="fs-set__field">
      <span className="fs-set__label">{label}</span>
      <div className="fs-set__inline">
        <code className="fs-set__secret" style={{ flex: '1 1 auto', textAlign: 'left', margin: 0 }}>{url}</code>
        <IconButton icon={copied ? Check : Copy} label={t('Copy')} size="sm" onClick={() => void copy()} />
      </div>
    </div>
  );
}

export function GoogleOAuthSetup({ onSaved, onClose, say }: { onSaved?: () => void; onClose?: () => void; say: (t: string) => void }) {
  const [status, setStatus] = useState<GoogleOAuthClientStatus | null>(null);
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [jsonText, setJsonText] = useState('');
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const m = useMsg();

  const load = () => getGoogleOAuthClient().then(setStatus).catch((e: Error) => m.bad(e.message));
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const readFile = async (f: File) => {
    setJsonText(await f.text());
  };

  const save = async () => {
    m.clear();
    const pastedJson = jsonText.trim();
    if (!pastedJson && !clientId.trim()) return m.bad(t('Paste the Client ID and secret, or the client_secret JSON file.'));
    if (!pastedJson && !clientSecret.trim()) return m.bad(t('A client secret is required.'));
    setBusy(true);
    try {
      const result = pastedJson
        ? await saveGoogleOAuthClient({ client_secret_json: pastedJson })
        : await saveGoogleOAuthClient({ client_id: clientId.trim(), client_secret: clientSecret.trim() });
      setStatus(result);
      setClientId('');
      setClientSecret('');
      setJsonText('');
      say(t('Google OAuth client saved.'));
      onSaved?.();
      (result.warnings ?? []).forEach((w) => m.bad(w));
    } catch (e) {
      m.bad((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const check = async () => {
    m.clear();
    setChecking(true);
    try {
      const r = await checkGoogleOAuthClient();
      (r.ok ? m.good : m.bad)(r.detail);
    } catch (e) {
      m.bad((e as Error).message);
    } finally {
      setChecking(false);
    }
  };

  const remove = async () => {
    setBusy(true);
    try {
      await deleteGoogleOAuthClient();
      say(t('Google OAuth client removed.'));
      setConfirmRemove(false);
      await load();
      onSaved?.();
    } catch (e) {
      m.bad((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!status) return <Skeleton label={t('Loading')} count={3} height="32px" />;

  const hostname = status.origin.replace(/^https?:\/\//, '').split(':')[0];
  const ambiguousHost = hostname === '127.0.0.1' || hostname === 'localhost';

  return (
    <>
      <h3 className="fs-set__card-title">{t('Configure Google')}</h3>
      <p className="fs-set__help" data-tone={status.configured ? 'ok' : undefined}>
        {status.configured
          ? status.source === 'stored'
            ? t('Configured (saved here).')
            : t('Configured (from .env).')
          : t('Not configured yet.')}
      </p>

      <ol className="fs-set__help" data-testid="google-oauth-steps">
        {status.steps.map((step) => (
          <li key={step}>{t(STEP_TEXT[step])}</li>
        ))}
      </ol>
      <div className="fs-set__row-end" style={{ justifyContent: 'flex-start' }}>
        <a href={status.console_url} target="_blank" rel="noopener noreferrer" className="fs-link">
          {t('Open Google Cloud Console')} <ExternalLink size={12} aria-hidden="true" style={{ verticalAlign: '-1px' }} />
        </a>
      </div>

      {ambiguousHost && (
        <div className="fs-notice" data-tone="warning" role="note">
          <AlertTriangle size={14} aria-hidden="true" />
          {t('Google matches the redirect URI letter by letter — register the origin you actually use ({host}).', { host: hostname })}
        </div>
      )}
      <UriRow label={t('Calendar redirect URI')} url={status.redirect_uris.calendar} say={say} />
      <UriRow label={t('Email redirect URI')} url={status.redirect_uris.email} say={say} />

      <h4 className="fs-users__h">{t('Client ID and secret')}</h4>
      <div className="fs-set__grid2">
        <Field label={t('Client ID')} htmlFor="goauth-id">
          <input id="goauth-id" className="fs-field" value={clientId} onChange={(e) => setClientId(e.target.value)} placeholder="123456789-abc.apps.googleusercontent.com" autoComplete="off" />
        </Field>
        <Field label={t('Client secret')} htmlFor="goauth-secret">
          <input id="goauth-secret" type="password" className="fs-field" value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} autoComplete="new-password" />
        </Field>
      </div>

      <Field label={t('Or drop / paste the client_secret_*.json file Google gave you')} htmlFor="goauth-json">
        <textarea
          id="goauth-json"
          className="fs-field"
          rows={3}
          value={jsonText}
          onChange={(e) => setJsonText(e.target.value)}
          placeholder='{"web": {"client_id": "…", "client_secret": "…", ...}}'
          onDrop={(e) => {
            if (e.dataTransfer.files.length) {
              e.preventDefault();
              void readFile(e.dataTransfer.files[0]);
            }
          }}
          onDragOver={(e) => {
            if (e.dataTransfer.types.includes('Files')) e.preventDefault();
          }}
        />
      </Field>
      <div className="fs-set__row-end" style={{ justifyContent: 'flex-start' }}>
        <Button size="sm" variant="ghost" icon={Upload} label={t('Choose a file…')} onClick={() => fileInput.current?.click()} />
        <input
          ref={fileInput}
          type="file"
          accept="application/json,.json"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void readFile(f);
            e.target.value = '';
          }}
        />
      </div>

      <FormFoot msg={m.msg} tone={m.tone}>
        {onClose && <Button size="sm" variant="ghost" label={t('Close')} onClick={onClose} />}
        {!confirmRemove ? (
          <Button size="sm" variant="ghost" icon={Trash2} label={t('Remove')} disabled={!status.configured || busy} onClick={() => setConfirmRemove(true)} testId="goauth-remove" />
        ) : (
          <span className="fs-modes__confirm">
            <span className="fs-set__help" data-tone="bad">{t('Remove for good?')}</span>
            <Button size="sm" variant="danger-solid" label={t('Remove')} loading={busy} onClick={() => void remove()} testId="goauth-remove-confirm" />
            <Button size="sm" variant="ghost" label={t('Cancel')} disabled={busy} onClick={() => setConfirmRemove(false)} />
          </span>
        )}
        <Button size="sm" variant="secondary" label={checking ? t('Checking…') : t('Check')} loading={checking} disabled={!status.configured} onClick={() => void check()} testId="goauth-check" />
        <Button size="sm" variant="primary" label={t('Save')} loading={busy} onClick={() => void save()} testId="goauth-save" />
      </FormFoot>
    </>
  );
}
