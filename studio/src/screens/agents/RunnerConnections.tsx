import { useEffect, useRef, useState } from 'react';
import { Button } from '../../components';
import { ApiError } from '../../adapters/api';
import { checkRunnerConnection, type RunnerConnection } from '../../adapters/runner-connections';
import { t } from '../../i18n';

const CLIENTS = [
  {key: 'codex', name: 'Codex', docs: 'https://learn.chatgpt.com/docs/auth'},
  {key: 'claude', name: 'Claude Code', docs: 'https://code.claude.com/docs/en/authentication'},
] as const;

function statusText(result: RunnerConnection): string {
  switch (result.state) {
    case 'connected': return t(result.authMethod === 'subscription' ? 'Subscription session detected' : 'API session detected');
    case 'login_required': return t('Sign in to the official client, then check again.');
    case 'not_installed': return t('Client not found on the Faustus server. Follow the setup instructions, then check again.');
    case 'configuration_conflict': return t('A session was detected, but environment settings may change the access method. Review the client configuration.');
    case 'timeout': return t('The client took too long to answer. Check that it opens normally, then retry.');
    default: return t('The client did not confirm a recognized access method. Check its sign-in status, then retry.');
  }
}

function ClientConnection({client}: {client: typeof CLIENTS[number]}) {
  const [result, setResult] = useState<RunnerConnection | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const request = useRef<AbortController | null>(null);
  const alive = useRef(true);
  useEffect(() => {alive.current = true; return () => {alive.current = false; request.current?.abort();};}, []);
  const check = async () => {
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
    const timer = window.setTimeout(() => controller.abort(), 12000);
    setBusy(true); setResult(null); setMessage('');
    try {
      const next = await checkRunnerConnection(client.key, controller.signal);
      if (alive.current && request.current === controller) setResult(next);
    } catch (error) {
      if (!alive.current || request.current !== controller) return;
      setMessage(error instanceof ApiError && [401, 403].includes(error.status)
        ? t('Sign in as an administrator to check this client.')
        : error instanceof ApiError && error.status === 404
          ? t('Restart the updated Faustus server to enable client connection checks.')
          : controller.signal.aborted
            ? t('The connection check timed out. Try again.')
            : t('Could not check the client. Check your connection to Faustus and retry.'));
    } finally {
      window.clearTimeout(timer);
      if (alive.current && request.current === controller) {request.current = null; setBusy(false);}
    }
  };
  const cancel = () => {request.current?.abort(); request.current = null; setBusy(false); setMessage(t('Connection check cancelled.'));};
  return <div className="fs-run-connection" aria-label={client.name}>
    <div className="fs-run-connection__content">
      <h4>{client.name}</h4>
      <p role="status" aria-live="polite" data-confirmed={result?.state === 'connected' || undefined}>
        {busy ? t('Checking the official client…') : message || (result ? statusText(result) : t('Not checked. Your existing sign-in stays with the official client.'))}
      </p>
      {result && !result.enabled && <p className="fs-run-connection__note">{t('External jobs are still off. This check does not enable them.')}</p>}
      {result?.state === 'configuration_conflict' && result.overrides.length > 0 && <p className="fs-run-connection__note">{t('Settings to review')}: <code>{result.overrides.join(', ')}</code></p>}
    </div>
    <div className="fs-run-connection__actions">
      <Button variant="secondary" label={t('Check {name} connection', {name: client.name})} loading={busy} onClick={() => void check()} />
      {busy ? <Button variant="ghost" label={t('Cancel')} onClick={cancel} />
        : <a href={client.docs} target="_blank" rel="noopener noreferrer">{t('Setup and sign-in help')}</a>}
    </div>
  </div>;
}

/** An explicit status check, not a second credential store or login launcher. */
export function RunnerConnections() {
  return <section className="fs-run-connections" aria-labelledby="runner-connections-heading">
    <h3 id="runner-connections-heading">{t('Your official clients')}</h3>
    <p className="fs-run-connections__intro">{t('Check an existing Codex or Claude Code sign-in. Subscription and API access are shown separately; no prompt is sent.')}</p>
    {CLIENTS.map(client => <ClientConnection client={client} key={client.key} />)}
    <p className="fs-run-connections__footnote">{t('This is a snapshot, not a quota or billing guarantee. Nothing is installed and your account settings are not changed by Faustus.')}</p>
  </section>;
}
