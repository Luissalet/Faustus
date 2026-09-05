import { Copy, ExternalLink } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Button } from '../../components';
import {
  cancelDeviceFlow,
  deviceProvider,
  DEVICE_PROVIDERS,
  pollDeviceFlow,
  startDeviceFlow,
  type DeviceStart,
} from '../../adapters/deviceAuth';
import { t } from '../../i18n';
import { Field, Select } from './fields';

/**
 * Signing in with a subscription instead of pasting a key.
 *
 * Copilot and a ChatGPT plan have no API key: the server asks the provider
 * for a short code, you type it into a page, and it polls until you have.
 * The code is the whole interaction, so it is the biggest thing on screen —
 * copyable, because it is typed somewhere else, possibly on another device.
 *
 * The tab is opened by a click, never on its own: a popup that appears while
 * someone is reading is a popup that gets blocked or dismissed.
 *
 * An abandoned flow is cancelled on the way out, so it does not sit in the
 * server's memory until it expires.
 */
export function DeviceSignIn({ onDone, onClose, say }: { onDone: () => void; onClose: () => void; say: (m: string) => void }) {
  const [id, setId] = useState(DEVICE_PROVIDERS[0].id);
  const [enterprise, setEnterprise] = useState('');
  const [start, setStart] = useState<DeviceStart | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [waiting, setWaiting] = useState(false);
  const alive = useRef(true);
  const provider = deviceProvider(id) ?? DEVICE_PROVIDERS[0];

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // Abandoned flows are dropped rather than left to expire.
  useEffect(() => {
    const flow = start;
    return () => {
      if (flow && waiting) void cancelDeviceFlow(provider, flow.pollId);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [start]);

  const begin = async () => {
    setBusy(true);
    setError(null);
    try {
      const s = await startDeviceFlow(provider, enterprise.trim() ? { enterprise_url: enterprise.trim() } : {});
      if (!alive.current) return;
      setStart(s);
      setWaiting(true);
      void run(s);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const run = async (s: DeviceStart) => {
    const until = Date.now() + s.expiresInMs;
    let wait = s.intervalMs;
    while (alive.current && Date.now() < until) {
      await new Promise((r) => setTimeout(r, wait));
      if (!alive.current) return;
      try {
        const p = await pollDeviceFlow(provider, s.pollId);
        if (!alive.current) return;
        if (p.status === 'authorized') {
          setWaiting(false);
          say(t('Signed in to {provider}. The endpoint is ready.', { provider: t(provider.label) }));
          onDone();
          onClose();
          return;
        }
        if (p.status === 'failed') {
          setWaiting(false);
          setError(p.error === 'access_denied' ? t('The sign-in was refused.') : p.error === 'expired_token' ? t('The code expired. Start again.') : p.error);
          return;
        }
      } catch (err) {
        setWaiting(false);
        setError((err as Error).message);
        return;
      }
      // The provider may ask for a slower rhythm; the server tracks it, so a
      // steady beat here is enough.
      wait = s.intervalMs;
    }
    if (alive.current && waiting) {
      setWaiting(false);
      setError(t('The code expired. Start again.'));
    }
  };

  return (
    <div className="fs-set__card" data-testid="device-sign-in">
      <h3 className="fs-set__card-title">{t('Sign in with a subscription')}</h3>
      {!start ? (
        <>
          <Field label={t('Provider')} htmlFor="dev-provider" help={t(provider.note)}>
            <Select id="dev-provider" value={id} onChange={setId} options={DEVICE_PROVIDERS.map((p) => ({ value: p.id, label: t(p.label) }))} />
          </Field>
          {id === 'copilot' && (
            <Field label={t('GitHub Enterprise (optional)')} htmlFor="dev-ent" help={t('Only for a GitHub Enterprise Server of your own; leave it empty for github.com.')}>
              <input id="dev-ent" className="fs-field" value={enterprise} onChange={(e) => setEnterprise(e.target.value)} placeholder="github.example.com" />
            </Field>
          )}
          {error && <p className="fs-set__help" data-tone="danger">{error}</p>}
          <div className="fs-set__row-actions">
            <span className="fs-set__spacer" />
            <Button variant="ghost" size="sm" label={t('Cancel')} onClick={onClose} />
            <Button variant="primary" size="sm" label={t('Start')} loading={busy} onClick={() => void begin()} />
          </div>
        </>
      ) : (
        <>
          <p className="fs-prose">{t('Open the page and type this code:')}</p>
          <p className="fs-devcode" data-testid="device-code">
            <code>{start.userCode}</code>
            <Button
              variant="ghost"
              size="sm"
              icon={Copy}
              label={t('Copy the code')}
              onClick={() => void navigator.clipboard.writeText(start.userCode).then(() => say(t('Copied.'))).catch(() => say(t('Could not copy it; it is on screen.')))}
            />
          </p>
          <div className="fs-set__row-actions">
            <Button
              variant="primary"
              size="sm"
              icon={ExternalLink}
              label={t('Open the page')}
              onClick={() => window.open(start.verificationUriComplete, '_blank', 'noopener')}
            />
            <span className="fs-set__spacer" />
            <Button variant="ghost" size="sm" label={t('Cancel')} onClick={onClose} />
          </div>
          <p className="fs-set__help" aria-live="polite">
            {error ? error : waiting ? t('Waiting for you to authorise it…') : t('Not waiting any more.')}
          </p>
          {start.verificationUri && <p className="fs-set__help">{t('If the button does not work: {url}', { url: start.verificationUri })}</p>}
        </>
      )}
    </div>
  );
}
