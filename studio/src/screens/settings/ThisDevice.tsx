import { useEffect, useState } from 'react';
import { Bell, BellOff, Download, Share, Trash2 } from 'lucide-react';
import { Button, IconButton, Skeleton } from '../../components';
import { t } from '../../i18n';
import {
  currentSubscription,
  isSupported,
  listSubscriptions,
  removeSubscription,
  sendTest,
  subscribeThisDevice,
  unsubscribeThisDevice,
  type PushSubscriptionRow,
} from '../../adapters/push';
import { canInstall, isInstalled, needsManualInstallHint, promptInstall, subscribeInstallPrompt } from '../../lib/installPrompt';

/**
 * "This device" (lot P-B): the phone-install half of Settings — whether
 * push works here, the devices already subscribed, and the "Install
 * Faustus" button once the browser has offered one.
 */
export function ThisDeviceSection({ say }: { say: (t: string) => void }) {
  const supported = isSupported();
  const [permission, setPermission] = useState<NotificationPermission | 'unsupported'>(
    supported && typeof Notification !== 'undefined' ? Notification.permission : 'unsupported',
  );
  const [subscribedHere, setSubscribedHere] = useState<boolean | null>(null);
  const [devices, setDevices] = useState<PushSubscriptionRow[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [label, setLabel] = useState('');
  // The last failure of enable(), kept on screen: a toast is gone before
  // anyone reads why the push service refused (seen live: a privacy-minded
  // browser with its push service switched off says "push service error"
  // and nothing else).
  const [enableError, setEnableError] = useState<string | null>(null);
  const [installable, setInstallable] = useState(canInstall());
  const [installed, setInstalled] = useState(isInstalled());

  const refresh = async () => {
    try {
      const [sub, rows] = await Promise.all([currentSubscription(), listSubscriptions()]);
      setSubscribedHere(!!sub);
      setDevices(rows);
    } catch (err) {
      say((err as Error).message || t('Could not load the subscribed devices.'));
      setDevices([]);
    }
  };

  useEffect(() => {
    void refresh();
    return subscribeInstallPrompt(() => {
      setInstallable(canInstall());
      setInstalled(isInstalled());
    });
  }, []);

  const enable = async () => {
    setBusy('enable');
    setEnableError(null);
    try {
      await subscribeThisDevice(label.trim() || defaultDeviceLabel());
      setPermission('granted');
      setLabel('');
      say(t('Notifications enabled on this device.'));
      await refresh();
    } catch (err) {
      const message = (err as Error).message || t('Could not enable notifications.');
      const pushServiceDown = /push service/i.test(message);
      setEnableError(
        pushServiceDown
          ? t('The browser could not reach its push service. Some privacy-focused browsers keep it switched off by default — enable push messaging in the browser\'s privacy settings, or install Faustus from another browser.')
          : message,
      );
      say(message);
    } finally {
      // The permission may have been granted even when the subscription
      // failed; show what the browser actually says now.
      if (typeof Notification !== 'undefined') setPermission(Notification.permission);
      setBusy(null);
    }
  };

  const disable = async () => {
    setBusy('disable');
    try {
      await unsubscribeThisDevice();
      say(t('Notifications disabled on this device.'));
      await refresh();
    } catch (err) {
      say((err as Error).message || t('Could not disable notifications.'));
    } finally {
      setBusy(null);
    }
  };

  const test = async () => {
    setBusy('test');
    try {
      await sendTest();
      say(t('Test notification sent.'));
    } catch (err) {
      say((err as Error).message || t('Could not send the test notification.'));
    } finally {
      setBusy(null);
    }
  };

  const remove = async (row: PushSubscriptionRow) => {
    setBusy(row.id);
    try {
      await removeSubscription(row.id);
      say(t('Device removed.'));
      await refresh();
    } catch (err) {
      say((err as Error).message || t('Could not remove the device.'));
    } finally {
      setBusy(null);
    }
  };

  const install = async () => {
    setBusy('install');
    try {
      const accepted = await promptInstall();
      say(accepted ? t('Faustus installed.') : t('Installation dismissed.'));
    } finally {
      setBusy(null);
      setInstallable(canInstall());
      setInstalled(isInstalled());
    }
  };

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-device">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-device" className="fs-set__title">{t('This device')}</h2>
          <p className="fs-prose">{t('Install Faustus as an app and get notifications here when a turn finishes, an approval is waiting, or a reminder is due.')}</p>
        </div>
      </header>

      <div className="fs-set__card">
        <h3 className="fs-set__card-title">{t('Install')}</h3>
        {installed && <p className="fs-set__help">{t('Faustus is already installed on this device.')}</p>}
        {!installed && installable && (
          <Button variant="primary" size="sm" icon={Download} label={t('Install Faustus')} loading={busy === 'install'} onClick={() => void install()} />
        )}
        {!installed && !installable && needsManualInstallHint() && (
          <p className="fs-set__help">
            <Share size={12} aria-hidden="true" style={{ verticalAlign: 'text-bottom' }} />{' '}
            {t('On iPhone or iPad: open the Share menu and choose "Add to Home Screen".')}
          </p>
        )}
        {!installed && !installable && !needsManualInstallHint() && (
          <p className="fs-set__help">{t('This browser has not offered to install Faustus yet. Reload the page, or use its own menu for "Install app" / "Add to Home Screen".')}</p>
        )}
      </div>

      <div className="fs-set__card">
        <h3 className="fs-set__card-title">{t('Notifications')}</h3>
        {!supported && <p className="fs-set__help" data-tone="bad">{t('This browser does not support push notifications.')}</p>}
        {supported && (
          <>
            <p className="fs-set__help">
              {t('Permission: {state}', { state: permissionLabel(permission) })}
              {' · '}
              {subscribedHere === null ? t('checking…') : subscribedHere ? t('subscribed on this device') : t('not subscribed on this device')}
            </p>
            {!subscribedHere && (
              <div className="fs-set__inline">
                <input
                  className="fs-field"
                  value={label}
                  maxLength={80}
                  placeholder={t('Device name (optional)')}
                  onChange={(e) => setLabel(e.target.value)}
                  aria-label={t('Device name')}
                />
                <Button variant="primary" size="sm" icon={Bell} label={t('Enable notifications on this device')} loading={busy === 'enable'} onClick={() => void enable()} />
              </div>
            )}
            {enableError && <p className="fs-set__help" data-tone="bad" role="alert">{enableError}</p>}
            {subscribedHere && (
              <div className="fs-set__row-actions">
                <Button variant="ghost" size="sm" icon={BellOff} label={t('Disable')} loading={busy === 'disable'} onClick={() => void disable()} />
                <Button variant="secondary" size="sm" label={t('Send a test notification')} loading={busy === 'test'} onClick={() => void test()} />
              </div>
            )}
          </>
        )}
      </div>

      <div className="fs-set__card">
        <h3 className="fs-set__card-title">{t('Subscribed devices')}</h3>
        {!devices && <Skeleton label={t('Loading devices')} count={2} height="48px" />}
        {devices && devices.length === 0 && <p className="fs-set__help">{t('No device is subscribed yet.')}</p>}
        {devices && devices.length > 0 && (
          <ul className="fs-set__health-list">
            {devices.map((row) => (
              <li key={row.id}>
                <strong>{row.device_name || t('Unnamed device')}</strong>
                {' — '}
                {t('added {date}', { date: new Date(row.created_at * 1000).toLocaleDateString() })}
                {row.failures > 0 && <span data-tone="bad"> · {t('{n} failed sends', { n: row.failures })}</span>}
                <IconButton icon={Trash2} label={t('Remove {name}', { name: row.device_name || t('Unnamed device') })} size="sm" disabled={busy === row.id} onClick={() => void remove(row)} />
              </li>
            ))}
          </ul>
        )}
      </div>

      <p className="fs-set__help">
        {t('Reaching Faustus away from this network needs the server exposed over HTTPS — a VPN or tunnel to it, set up once by whoever runs the server.')}
      </p>
    </section>
  );
}

function permissionLabel(permission: NotificationPermission | 'unsupported'): string {
  if (permission === 'granted') return t('granted');
  if (permission === 'denied') return t('denied');
  if (permission === 'unsupported') return t('unsupported');
  return t('not asked yet');
}

function defaultDeviceLabel(): string {
  const ua = navigator.userAgent;
  if (/iphone/i.test(ua)) return 'iPhone';
  if (/ipad/i.test(ua)) return 'iPad';
  if (/android/i.test(ua)) return 'Android';
  if (/mac/i.test(ua)) return 'Mac';
  if (/win/i.test(ua)) return 'Windows';
  return t('This device');
}
