import { useEffect, useRef, useState } from 'react';
import { Button } from '../../components';
import { addEndpoint, testEndpoint } from '../../adapters/settings';
import { PROVIDER_PRESETS } from '../../lib/provider-presets';
import { t, tn } from '../../i18n';

/** A guided API connection, using the existing server-owned credential store. */
export function ProviderConnect({onDone, embedded = false}: {onDone: () => void; embedded?: boolean}) {
  const [provider, setProvider] = useState<(typeof PROVIDER_PRESETS)[number] | null>(null);
  const [key, setKey] = useState('');
  const [models, setModels] = useState<string[] | null>(null);
  const [chosen, setChosen] = useState<string[]>([]);
  const [busy, setBusy] = useState<'checking' | 'saving' | null>(null);
  const [message, setMessage] = useState('');
  const request = useRef<AbortController | null>(null);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => {alive.current = false; request.current?.abort();}; }, []);
  const choose = (next: typeof provider) => {
    request.current?.abort(); request.current = null;
    setProvider(next); setKey(''); setModels(null); setChosen([]); setBusy(null); setMessage('');
  };
  const act = async (save: boolean) => {
    if (!provider || !key.trim() || request.current || (save && !chosen.length)) return;
    const controller = new AbortController(); request.current = controller;
    const timer = setTimeout(() => controller.abort(), 45000);
    setBusy(save ? 'saving' : 'checking'); setMessage('');
    try {
      if (save) {
        await addEndpoint({name: provider.label, baseUrl: provider.baseUrl, apiKey: key.trim(), kind:'api', modelType:'llm', pinnedModels:chosen, shared:false, requireModels:true}, controller.signal);
        if (!alive.current || request.current !== controller) return;
        setKey(''); setModels(null); setChosen([]); setProvider(null);
        setMessage(t('Connected. Your selected models are now available in the chat picker.'));
        onDone();
      } else {
        const result = await testEndpoint(provider.baseUrl, key.trim(), controller.signal);
        if (!alive.current || request.current !== controller) return;
        if (!result.ok) {
          setMessage(t('Could not verify this key. Check the key, API access and provider status, then retry.'));
        } else if (!result.privateConnectionsSupported) {
          setMessage(t('Restart the updated Faustus server before adding a private connection.'));
        } else if (!result.models.length) {
          setModels([]);
          setMessage(t('The provider returned no models. Check API access before connecting.'));
        } else { setModels(result.models); setChosen([]); }
      }
    } catch {
      if (alive.current && request.current === controller) setMessage(save
        ? t('The save was not confirmed. Refresh the connections before retrying to check whether it completed.')
        : t('Could not verify this key. Check the key, API access and provider status, then retry.'));
    } finally {
      clearTimeout(timer);
      if (alive.current && request.current === controller) {request.current = null; setBusy(null);}
    }
  };
  return <div className="fs-set__quick-connect">
    {!embedded && <h3 className="fs-set__card-title">{t('Connect an AI provider')}</h3>}
    <p className="fs-set__help">{t('Use an API key. API usage is billed separately from chat subscriptions; provider limits and free tiers may apply.')}</p>
    <div className="fs-set__row-actions" role="group" aria-label={t('AI providers')}>
      {PROVIDER_PRESETS.map(p => <button type="button" className="fs-btn" key={p.id} aria-pressed={provider?.id === p.id} disabled={busy === 'saving'} onClick={() => choose(p)}>{p.label}</button>)}
    </div>
    {provider && <form className="fs-set__provider-form" onSubmit={event => {event.preventDefault(); void act(Boolean(models?.length));}}>
      <label className="fs-set__label" htmlFor="provider-key">{t('API key')} · {provider.label}</label>
      <input autoFocus id="provider-key" className="fs-field" type="password" autoComplete="new-password" value={key} disabled={Boolean(busy)} onChange={event => {setKey(event.target.value); setModels(null); setChosen([]); setMessage('');}} />
      <a href={provider.keyUrl} target="_blank" rel="noopener noreferrer">{t('Get a key from {provider}', {provider:provider.label})}</a>
      <p className="fs-set__help">{t('Only you can use this connection. Your key is sent to the Faustus server, not stored in this browser.')}</p>
      {Boolean(models?.length) && <fieldset className="fs-set__provider-models" disabled={Boolean(busy)}>
        <legend>{t('Choose models for the chat picker')}</legend>
        <p className="fs-set__help">{t('This checks the model list, not generation access or every model capability.')}</p>
        {models!.map(model => <label key={model}><input type="checkbox" checked={chosen.includes(model)} onChange={event => setChosen(list => event.target.checked ? [...list, model] : list.filter(id => id !== model))} /><span>{model}</span></label>)}
      </fieldset>}
      <div className="fs-set__row-actions">
        <Button label={t('Cancel')} disabled={busy === 'saving'} onClick={() => choose(null)} />
        <Button variant="primary" label={models?.length ? t('Connect selected models') : t('Check connection')} loading={Boolean(busy)} disabled={!key.trim() || (Boolean(models?.length) && !chosen.length)} onClick={() => void act(Boolean(models?.length))} />
        {busy && <span role="status">{busy === 'checking' ? t('Checking connection…') : t('Saving connection…')}</span>}
        {!busy && Boolean(models?.length) && <span>{tn(chosen.length, '{n} model selected', '{n} models selected')}</span>}
      </div>
    </form>}
    {message && <p role="status" className="fs-set__help">{message}</p>}
  </div>;
}
