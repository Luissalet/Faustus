import { PenLine, Settings2, Sparkles } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link } from 'react-router';
import { Button, Dialog, Skeleton } from '../../components';
import { DEFAULT_AWAY_SUBJECT, extractWritingStyle, getMailConfig, getWritingStyle, saveMailConfig, saveWritingStyle, type EmailAccount, type MailConfig } from '../../adapters/email';
import { t } from '../../i18n';

/**
 * The mail's own settings: the away reply, what runs on its own when
 * mail arrives, and the writing style AI drafts imitate. Accounts (IMAP,
 * SMTP, Google) live in Settings → Integrations and are linked from here.
 */
export function MailSettingsDialog({ accounts, accountId, onClose, onSaved, say }: { accounts: EmailAccount[]; accountId: string | null; onClose: () => void; onSaved: (cfg: MailConfig) => void; say: (msg: string, tone?: 'ok' | 'warn') => void }) {
  const [acc, setAcc] = useState(accountId);
  const [cfg, setCfg] = useState<MailConfig | null>(null);
  const [style, setStyle] = useState<string | null>(null);
  const [busy, setBusy] = useState<'save' | 'style' | 'extract' | null>(null);
  const account = accounts.find((a) => a.id === acc) ?? null;

  useEffect(() => {
    setCfg(null);
    setStyle(null);
    getMailConfig(acc)
      .then(setCfg)
      .catch((err: Error) => say(err.message || t('Could not read the mail settings.'), 'warn'));
    getWritingStyle(acc)
      .then(setStyle)
      .catch(() => setStyle(''));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [acc]);

  const set = (p: Partial<MailConfig>) => setCfg((c) => (c ? { ...c, ...p } : c));

  const save = async () => {
    if (!cfg) return;
    setBusy('save');
    try {
      await saveMailConfig(acc, cfg);
      say(t('Saved.'));
      onSaved(cfg);
    } catch (err) {
      say((err as Error).message || t('Could not save.'), 'warn');
    } finally {
      setBusy(null);
    }
  };

  const saveStyle = async () => {
    setBusy('style');
    try {
      await saveWritingStyle(acc, style ?? '');
      say(t('Style saved.'));
    } catch (err) {
      say((err as Error).message || t('Could not save the style.'), 'warn');
    } finally {
      setBusy(null);
    }
  };

  const extract = async () => {
    setBusy('extract');
    try {
      const s = await extractWritingStyle(acc);
      setStyle(s);
      say(t('Style extracted from your sent mail.'));
    } catch (err) {
      say((err as Error).message || t('Could not extract the style.'), 'warn');
    } finally {
      setBusy(null);
    }
  };

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={t('Mail settings')}
      testId="mail-settings"
      footer={
        <>
          <Link className="fs-mail__link" to="/settings?s=integrations">
            <Settings2 size={12} aria-hidden="true" /> {t('Accounts (IMAP, SMTP, Google) are in Settings → Integrations')}
          </Link>
          <span className="fs-spacer" />
          <Button variant="ghost" size="sm" label={t('Close')} onClick={onClose} />
          <Button variant="primary" size="sm" label={t('Save')} loading={busy === 'save'} disabled={!cfg} onClick={() => void save()} testId="mail-settings-save" />
        </>
      }
    >
      <div className="fs-mail-set">
        {accounts.length > 1 && (
          <label className="fs-mail__field">
            <span>{t('Account')}</span>
            <select className="fs-field" value={acc ?? ''} onChange={(e) => setAcc(e.target.value || null)}>
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                  {a.isDefault ? ` · ${t('default')}` : ''}
                </option>
              ))}
            </select>
          </label>
        )}

        {!cfg && <Skeleton label={t('Loading the settings')} count={5} height="32px" />}

        {cfg && (
          <>
            <section className="fs-mail-set__section" aria-labelledby="mail-set-away">
              <header className="fs-mail-set__head">
                <div>
                  <h3 id="mail-set-away">{t('Away reply')}</h3>
                  <p className="fs-muted">{t('A holiday or out-of-office answer for incoming mail{account}.', { account: account ? ` · ${account.name}` : '' })}</p>
                </div>
                <label className="fs-switch">
                  <input type="checkbox" checked={cfg.autoReply} onChange={(e) => set({ autoReply: e.target.checked })} data-testid="mail-away-on" />
                  <span>{cfg.autoReply ? t('On') : t('Off')}</span>
                </label>
              </header>
              <div className="fs-mail-set__grid">
                <label className="fs-mail__field">
                  <span>{t('From')}</span>
                  <input type="date" className="fs-field" value={cfg.autoReplyStart} onChange={(e) => set({ autoReplyStart: e.target.value })} />
                </label>
                <label className="fs-mail__field">
                  <span>{t('Until')}</span>
                  <input type="date" className="fs-field" value={cfg.autoReplyEnd} onChange={(e) => set({ autoReplyEnd: e.target.value })} />
                </label>
              </div>
              <label className="fs-mail__field">
                <span>{t('Subject')}</span>
                <input type="text" className="fs-field" value={cfg.autoReplySubject} placeholder={DEFAULT_AWAY_SUBJECT} onChange={(e) => set({ autoReplySubject: e.target.value })} />
              </label>
              <label className="fs-mail__field">
                <span>{t('Message')}</span>
                <textarea className="fs-field" rows={4} value={cfg.autoReplyMessage} onChange={(e) => set({ autoReplyMessage: e.target.value })} placeholder={t("I'm away until {date} and will answer when I'm back.", { date: cfg.autoReplyEnd || '…' })} />
              </label>
              <label className="fs-mail__field">
                <span>{t('Answer the same sender')}</span>
                <select className="fs-field" value={cfg.autoReplyCooldown} onChange={(e) => set({ autoReplyCooldown: e.target.value as MailConfig['autoReplyCooldown'] })}>
                  <option value="period">{t('Once while active')}</option>
                  <option value="1d">{t('Every day')}</option>
                  <option value="3d">{t('Every 3 days')}</option>
                  <option value="7d">{t('Every 7 days')}</option>
                </select>
              </label>
              <label className="fs-switch">
                <input type="checkbox" checked={cfg.autoReplyExcludeAutomated} onChange={(e) => set({ autoReplyExcludeAutomated: e.target.checked })} />
                <span>{t('Skip automated and no-reply senders')}</span>
              </label>
              <label className="fs-switch">
                <input type="checkbox" checked={cfg.autoReplyPauseNotifications} onChange={(e) => set({ autoReplyPauseNotifications: e.target.checked })} />
                <span>{t('Pause mail notifications while active')}</span>
              </label>
            </section>

            <section className="fs-mail-set__section" aria-labelledby="mail-set-auto">
              <header className="fs-mail-set__head">
                <div>
                  <h3 id="mail-set-auto">
                    <Sparkles size={13} aria-hidden="true" /> {t('When mail arrives')}
                  </h3>
                  <p className="fs-muted">{t('What Faustus does on its own with new mail. The triage task in Automations does the work; these switches allow it.')}</p>
                </div>
              </header>
              <label className="fs-switch">
                <input type="checkbox" checked={cfg.autoSummarize} onChange={(e) => set({ autoSummarize: e.target.checked })} />
                <span>{t('Summarise long mails')}</span>
              </label>
              <label className="fs-switch">
                <input type="checkbox" checked={cfg.autoTag} onChange={(e) => set({ autoTag: e.target.checked })} />
                <span>{t('Tag mails (urgent, bills, receipts, travel…)')}</span>
              </label>
              <label className="fs-switch">
                <input type="checkbox" checked={cfg.autoSpam} onChange={(e) => set({ autoSpam: e.target.checked })} />
                <span>{t('Flag likely spam')}</span>
              </label>
              <label className="fs-switch">
                <input type="checkbox" checked={cfg.autoCalendar} onChange={(e) => set({ autoCalendar: e.target.checked })} />
                <span>{t('Put appointments in the calendar')}</span>
              </label>
              <p className="fs-muted">{t('Translations default to {language}.', { language: cfg.translateLanguage })}</p>
            </section>
          </>
        )}

        <section className="fs-mail-set__section" aria-labelledby="mail-set-style">
          <header className="fs-mail-set__head">
            <div>
              <h3 id="mail-set-style">
                <PenLine size={13} aria-hidden="true" /> {t('Writing style')}
              </h3>
              <p className="fs-muted">{t('Used when Faustus drafts replies for you. Extract reads your sent mail and writes it down.')}</p>
            </div>
          </header>
          {style === null ? (
            <Skeleton label={t('Loading the style')} count={1} height="80px" />
          ) : (
            <textarea className="fs-field" rows={5} value={style} onChange={(e) => setStyle(e.target.value)} placeholder={t('Short sentences, warm, signs with a first name…')} data-testid="mail-style" />
          )}
          <div className="fs-inline">
            <Button variant="secondary" size="sm" icon={Sparkles} label={t('Extract from sent mail')} loading={busy === 'extract'} onClick={() => void extract()} />
            <Button variant="ghost" size="sm" label={t('Save the style')} loading={busy === 'style'} disabled={style === null} onClick={() => void saveStyle()} />
          </div>
        </section>
      </div>
    </Dialog>
  );
}
