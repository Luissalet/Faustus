import { useCallback, useEffect, useState } from 'react';
import { Button } from '../../components';
import { t } from '../../i18n';
import { telegramStatus, telegramTest, type TelegramBridgeStatus } from '../../adapters/chatBridges';

/**
 * One status line for the Telegram bridge, shown at the top of its group on
 * the agent settings page: whether the poller runs and as which bot, why it
 * stopped if it did, and the chats that wrote without being on the list (their
 * ids are what the allow-list field needs). The test button asks the server to
 * call the bot API with the saved token; the token itself never reaches the
 * browser.
 */
export function ChatBridgeStatus({ savedKey }: { savedKey: string }) {
  const [status, setStatus] = useState<TelegramBridgeStatus | null>(null);
  const [error, setError] = useState('');
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState<{ ok: boolean; text: string } | null>(null);

  const refresh = useCallback(async () => {
    try {
      setStatus(await telegramStatus());
      setError('');
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  // Read again when the settings were saved (savedKey changes) and while the page is open.
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 10000);
    return () => window.clearInterval(timer);
  }, [refresh, savedKey]);

  const runTest = async () => {
    setTesting(true);
    setTest(null);
    try {
      const out = await telegramTest();
      setTest(out.ok ? { ok: true, text: t('The token works: @{name}.', { name: out.username || out.name || '?' }) } : { ok: false, text: out.error || t('The bot API did not accept the token.') });
    } catch (e) {
      setTest({ ok: false, text: (e as Error).message });
    } finally {
      setTesting(false);
    }
  };

  let line = t('Loading the bridge status…');
  let tone: 'ok' | 'bad' | 'warn' | undefined;
  if (status) {
    if (status.disabled_reason) {
      line = status.disabled_reason;
      tone = 'bad';
    } else if (status.running && status.failures_in_a_row > 0) {
      line = t('Running, but the last request failed: {error}', { error: status.last_error });
      tone = 'warn';
    } else if (status.running) {
      line = status.bot_username ? t('Running as @{name}.', { name: status.bot_username }) : t('Starting…');
      tone = 'ok';
    } else if (status.enabled && !status.configured) {
      line = t('Enabled, but no bot token is saved.');
      tone = 'warn';
    } else if (status.enabled) {
      line = t('Enabled, not running yet.');
      tone = 'warn';
    } else {
      line = t('Off.');
    }
  }

  return (
    <div className="fs-set__card" data-testid="chat-bridge-status">
      <div className="fs-set__row-between">
        <h3 className="fs-set__card-title">{t('Telegram bridge')}</h3>
        <Button size="sm" variant="ghost" label={testing ? t('Testing…') : t('Test the token')} loading={testing} disabled={status ? !status.configured : false} onClick={() => void runTest()} />
      </div>
      {error ? <p className="fs-set__help" data-tone="bad" role="alert">{error}</p> : <p className="fs-set__help" data-tone={tone} role="status">{line}</p>}
      {test && <p className="fs-set__help" data-tone={test.ok ? 'ok' : 'bad'} role="status">{test.text}</p>}
      {status && status.last_error && !status.disabled_reason && !(status.running && status.failures_in_a_row > 0) && (
        <p className="fs-set__help">{t('Last error: {error}', { error: status.last_error })}</p>
      )}
      {status && (
        <p className="fs-set__help">
          {t('{allowed} allowed chats, {conversations} conversations.', { allowed: status.allowed_chats.length, conversations: status.mapped_sessions.length })}
        </p>
      )}
      {status && status.refused_chats.length > 0 && (
        <p className="fs-set__help" data-testid="chat-bridge-refused">
          {t('Chats that wrote and are not on the list: {chats}. Copy an id into the allowed list to let that chat in.', {
            chats: status.refused_chats.map((c) => (c.title ? `${c.chat_id} (${c.title})` : c.chat_id)).join(', '),
          })}
        </p>
      )}
    </div>
  );
}
