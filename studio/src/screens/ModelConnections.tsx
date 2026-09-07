import { useEffect, useRef, useState } from 'react';
import { Button, Dialog } from '../components';
import { ProviderConnect } from './settings/ProviderConnect';
import { t } from '../i18n';
import './settings.css';

/** A protected, temporary form: closing it returns to the same conversation. */
export default function ModelConnections({open, onOpenChange, onDone}: {
  open: boolean; onOpenChange: (open: boolean) => void; onDone: () => void;
}) {
  const [kind, setKind] = useState<'api' | 'claude'>('api');
  const [mode, setMode] = useState<'subscription' | 'api'>('subscription');
  const [model, setModel] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => {request.current?.abort(); request.current = null;}, []);
  const connect = async () => {
    if (request.current) return;
    const controller = new AbortController(); request.current = controller;
    const timer = window.setTimeout(() => controller.abort(), 20000);
    setBusy(true); setMessage('');
    try {
      const response = await fetch('/api/agent-runners/claude/model', {
        method: 'POST', credentials: 'same-origin', signal: controller.signal,
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({billing_mode: mode, model: model.trim() || 'client-default'}),
      });
      if (request.current !== controller) return;
      if (!response.ok) {
        setMessage(response.status === 409 ? t('Enable external agent runners in Settings before connecting a client.')
          : [401, 403].includes(response.status) ? t('Sign in as an administrator to add this private connection.')
          : response.status === 400 ? t('The client did not confirm the selected access method. Check its sign-in status, then retry.')
          : t('The connection was not confirmed. Refresh the model list before retrying.'));
        return;
      }
      setMessage(t('Connected. Refresh the model picker and select the new connection. Your current model has not changed.'));
      onDone();
    } catch {
      if (request.current === controller) setMessage(t('The connection was not confirmed. Refresh the model list before retrying.'));
    } finally {
      window.clearTimeout(timer);
      if (request.current === controller) {request.current = null; setBusy(false);}
    }
  };
  return <Dialog open={open} onOpenChange={next => {if (!busy) onOpenChange(next);}}
    title={t('Connect an AI provider')} description={t('Add a private connection without leaving this conversation. Existing chats keep their current model.')} testId="chat-model-connections">
    <div className="fs-set__row-actions" role="group" aria-label={t('Connection type')}>
      <Button label={t('Provider API')} aria-pressed={kind === 'api'} disabled={busy} variant={kind === 'api' ? 'primary' : 'secondary'} onClick={() => {setKind('api'); setMessage('');}} />
      <Button label="Claude Code" aria-pressed={kind === 'claude'} disabled={busy} variant={kind === 'claude' ? 'primary' : 'secondary'} onClick={() => {setKind('claude'); setMessage('');}} />
    </div>
    {kind === 'api' ? <ProviderConnect embedded onDone={onDone} /> : <form className="fs-set__provider-form" onSubmit={e => {e.preventDefault(); void connect();}}>
      <p className="fs-set__help">{t('Use the official Claude Code sign-in on the Faustus server. Text only; Faustus runs the tools. Subscription limits apply. No automatic switch to paid API.')}</p>
      <label className="fs-set__label" htmlFor="client-billing">{t('Access method')}</label>
      <select id="client-billing" className="fs-field" value={mode} disabled={busy} onChange={e => setMode(e.target.value as typeof mode)}>
        <option value="subscription">{t('Subscription')}</option><option value="api">{t('Provider API')}</option>
      </select>
      <label className="fs-set__label" htmlFor="client-model">{t('Client model (optional)')}</label>
      <input id="client-model" className="fs-field" maxLength={200} value={model} disabled={busy} onChange={e => setModel(e.target.value)} aria-describedby="client-model-help" />
      <p id="client-model-help" className="fs-set__help">{t('Leave blank to use the client default. Availability depends on your account; no model catalog is assumed.')}</p>
      <a href="https://code.claude.com/docs/en/authentication" target="_blank" rel="noopener noreferrer">{t('Setup and sign-in help')}</a>
      <Button label={busy ? t('Checking connection…') : t('Connect Claude Code')} variant="primary" loading={busy} onClick={() => void connect()} />
    </form>}
    {message && <p role="status" className="fs-set__help">{message}</p>}
  </Dialog>;
}
