import { useEffect, useId, useRef, useState } from 'react';
import { Button, Dialog } from '../../components';
import { ApiError } from '../../adapters/api';
import { parseSshTrust, sshTrustAction, type SshTrustState } from '../../adapters/ssh-trust';
import { t } from '../../i18n';

/** Host-key trust is separate from authorising the app's login key. */
export function SshTrust({host, port, disabled}: {host:string; port?:string; disabled:boolean}) {
  const id = useId();
  const [state, setState] = useState<SshTrustState | null>(null);
  const [fingerprint, setFingerprint] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [revoke, setRevoke] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => {request.current?.abort(); request.current = null;}, []);
  const run = async (action: 'fingerprint' | 'pair' | 'unpair') => {
    if (request.current || disabled) return;
    const controller = new AbortController(); request.current = controller;
    const timer = window.setTimeout(() => controller.abort(), 20000);
    setBusy(true); setMessage('');
    try {
      const raw = await sshTrustAction(action, host, port, controller.signal, action === 'pair' ? fingerprint.trim() : undefined);
      if (request.current !== controller) return;
      if (action === 'fingerprint') setState(parseSshTrust(raw));
      else {
        // A fresh scan is required after every mutation. Never present the
        // previously offered key as newly verified, or pair automatically.
        setState(null); setFingerprint(''); setRevoke(false);
        setMessage(t(action === 'pair' ? 'Host paired. You can now test SSH.' : 'Host trust removed. Inspect and verify its identity again before pairing.'));
      }
    } catch (error) {
      if (request.current !== controller) return;
      setState(null); setFingerprint(''); setRevoke(false);
      setMessage(error instanceof ApiError && error.status === 409
        ? t('The host key changed. Verify the server identity independently; never accept a replacement automatically.')
        : error instanceof ApiError && [401,403].includes(error.status)
          ? t('Only a signed-in administrator can change host trust.')
          : action === 'fingerprint' ? t('Could not inspect this host. Check its address, port and network, then retry.')
            : t('The trust change was not confirmed. Inspect the host again before retrying.'));
    } finally {
      window.clearTimeout(timer);
      if (request.current === controller) {request.current = null; setBusy(false);}
    }
  };
  const matches = state?.state === 'unpaired' && state.offered.some(k => k.fingerprint === fingerprint.trim());
  return <details className="fs-ck__keybox fs-ssh-trust">
    <summary>{t('Verify the remote server identity')}</summary>
    <p className="fs-muted">{t('Faustus only connects to trusted SSH hosts. This is separate from the login key below.')}</p>
    <p><code>{host}{port ? `:${port}` : ''}</code></p>
    {disabled && <p>{t('Save the server changes before checking trust.')}</p>}
    <Button label={t('Inspect host fingerprints')} loading={busy} disabled={disabled} onClick={() => void run('fingerprint')} />
    {state && <>
      <p role="status">{t(state.state === 'paired' ? 'Host identity matches the saved key.' : state.state === 'changed' ? 'The host key changed. Verify the server identity independently; never accept a replacement automatically.' : 'This host has not been paired yet.')}</p>
      {state.paired.length > 0 && <><h4>{t('Saved fingerprints')}</h4><ul>{state.paired.map(k => <li key={k}><code>{k}</code></li>)}</ul></>}
      <h4>{t('Offered fingerprints')}</h4>
      <ul>{state.offered.map(k => <li key={k.fingerprint}><span>{k.type}</span><code>{k.fingerprint}</code></li>)}</ul>
      {state.state === 'unpaired' && <div className="fs-ssh-trust__verify">
        <label htmlFor={id}>{t('Fingerprint verified through the server console')}</label>
        <p id={`${id}-help`}>{t('Get the SHA256 fingerprint from the server administrator or console through a trusted channel. Paste it here; do not simply copy the offered value.')}</p>
        <input id={id} className="fs-field" value={fingerprint} maxLength={64} spellCheck={false} autoComplete="off" aria-describedby={`${id}-help`} disabled={busy || disabled} onChange={e => setFingerprint(e.target.value)} />
        <Button label={t('Trust this verified host')} disabled={!matches || busy || disabled} onClick={() => void run('pair')} />
      </div>}
      {state.paired.length > 0 && <Button variant="danger" label={t('Remove saved host trust')} disabled={busy || disabled} onClick={() => setRevoke(true)} />}
    </>}
    {message && <p role="status">{message}</p>}
    {revoke && <Dialog open onOpenChange={v => {if (!busy) setRevoke(v);}} title={t('Remove saved host trust?')}
      description={t('Connections to this server will be refused until you verify and pair its identity again. This does not trust the replacement key.')}
      footer={<><Button label={t('Cancel')} disabled={busy} onClick={() => setRevoke(false)} /><Button variant="danger-solid" label={t('Remove saved host trust')} loading={busy} onClick={() => void run('unpair')} /></>} />}
  </details>;
}
