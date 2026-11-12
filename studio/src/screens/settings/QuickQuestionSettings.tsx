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
  const [registered, setRegistered] = useState('');
  useEffect(() => {
    let live = true;
    void loadSettings().then(settings => {
      if (!live) return;
      setEnabled(settings.quick_question_enabled !== false);
      setHotkey(String(settings.quick_question_hotkey || 'Ctrl+Shift+Space'));
      setReady(true);
    }).catch(err => { if (live) setError((err as Error).message); });
    const readNative = () => {
      const bridge = window.faustusWindow as unknown as { command(action: string): Promise<{quick?: {registered: string; error: string}}> } | undefined;
      void bridge?.command('quick-state').then(result => {
        if (!live) return;
        setRegistered(result.quick?.registered || '');
        setError(result.quick?.error ? t('This shortcut is unavailable. Choose another combination; the previous shortcut still works.') : '');
      }).catch(() => undefined);
    };
    readNative();
    const timer = window.setInterval(readNative, 3000);
    return () => { live = false; clearInterval(timer); };
  }, []);
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
    {registered && <p className="fs-set__help">{t('Active shortcut: {key}', {key: registered})}</p>}
    {error && <p className="fs-set__help" role="alert" data-tone="bad">{error}</p>}
    <Button variant="secondary" size="sm" label={t('Save')} loading={busy} disabled={!ready || !hotkey.trim()} onClick={() => void save()} />
  </div>;
}
