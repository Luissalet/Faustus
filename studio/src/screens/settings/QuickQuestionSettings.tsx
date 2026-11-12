import { useEffect, useState } from 'react';
import { Button } from '../../components';
import { loadSettings, saveSettings } from '../../adapters/settings';
import { t } from '../../i18n';
import { Field, Text, Toggle } from './fields';

export function QuickQuestionSettings({ say }: { say: (text: string) => void }) {
  const [enabled, setEnabled] = useState(true);
  const [hotkey, setHotkey] = useState('Ctrl+Shift+Space');
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [native, setNative] = useState<{registered: string; shortcutUnavailable: boolean}>();
  const bridge = window.faustusWindow as unknown as {
    command(action: 'quick-state' | 'quick-open'): Promise<{quick?: typeof native}>;
  } | undefined;
  useEffect(() => {
    let live = true;
    void loadSettings().then(settings => {
      if (!live) return;
      setEnabled(settings.quick_question_enabled !== false);
      setHotkey(String(settings.quick_question_hotkey || 'Ctrl+Shift+Space'));
      setReady(true);
    }).catch(err => { if (live) setError((err as Error).message); });
    const readNative = () => {
      void bridge?.command('quick-state').then(result => {
        if (live) setNative(result.quick);
      }).catch(() => undefined);
    };
    readNative();
    const timer = bridge ? window.setInterval(readNative, 3000) : undefined;
    return () => { live = false; clearInterval(timer); };
  }, [bridge]);
  const save = async () => {
    setBusy(true); setError('');
    try {
      await saveSettings({quick_question_enabled: enabled, quick_question_hotkey: hotkey.trim()});
      say(t('Quick question settings saved.'));
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  };
  return <div className="fs-set__card" data-testid="quick-question-settings">
    <h3 className="fs-set__card-title">{t('Quick question')}</h3>
    <p className="fs-set__help">{t('In the Windows desktop app, open a small window from any app. It uses the same Faustus and conversation as Studio, including on a Spark.')}</p>
    <Toggle id="quick-question-enabled" label={t('On')} checked={enabled} disabled={!ready || busy} onChange={setEnabled} />
    <Field label={t('Global shortcut')} htmlFor="quick-question-hotkey" help={t('For example Ctrl+Shift+Space. Esc hides the window without stopping the answer.')}>
      <Text id="quick-question-hotkey" value={hotkey} onChange={setHotkey} disabled={!ready || busy} />
    </Field>
    {native?.registered && <p className="fs-set__help">{t('Active shortcut: {key}', {key: native.registered})}</p>}
    {native?.shortcutUnavailable && <p className="fs-set__help" role="alert" data-tone="bad">{t('This shortcut is unavailable. Choose another combination.')}</p>}
    {error && <p className="fs-set__help" role="alert" data-tone="bad">{error}</p>}
    <Button variant="secondary" size="sm" label={t('Save')} loading={busy} disabled={!ready || !hotkey.trim()} onClick={() => void save()} />
    {native && <Button variant="ghost" size="sm" label={t('Open quick question')} onClick={() => {
      void bridge?.command('quick-open').catch(err => setError((err as Error).message));
    }} />}
  </div>;
}
