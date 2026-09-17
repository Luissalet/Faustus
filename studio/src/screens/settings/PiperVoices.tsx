import { useCallback, useEffect, useState } from 'react';
import { Download, RefreshCw } from 'lucide-react';
import { Button } from '../../components';
import { downloadPiperVoice, installPiperEngine, loadPiperStatus, type PiperStatus } from '../../adapters/piper';
import { t } from '../../i18n';
import { Select } from './fields';

/** Voice picker + engine/voice installer for `tts_provider = 'piper'`.
 * Fully local, MIT-licensed TTS — no built-in runtime needed on first run,
 * this is where an operator installs the engine and downloads a voice. */
export function PiperVoices({ voice, onSelectVoice, say }: { voice: string; onSelectVoice: (name: string) => void; say: (t: string) => void }) {
  const [status, setStatus] = useState<PiperStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null); // voice name being downloaded, or 'engine'

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setStatus(await loadPiperStatus());
    } catch (e) {
      say((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [say]);

  useEffect(() => { void refresh(); }, [refresh]);

  const handleDownload = async (name: string) => {
    setBusy(name);
    try {
      await downloadPiperVoice(name);
      say(t('Voice "{name}" installed.', { name }));
      await refresh();
      if (!voice) onSelectVoice(name);
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const handleInstallEngine = async () => {
    setBusy('engine');
    try {
      await installPiperEngine();
      say(t('Piper engine installed.'));
      await refresh();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  if (loading) return <p className="fs-prose">{t('Loading Piper voices…')}</p>;
  if (!status) return null;

  const installed = status.installed_voices;
  const notInstalled = status.catalogue.filter((v) => !v.installed);

  return (
    <div className="fs-set__field">
      {!status.dependency_installed && (
        <div className="fs-set__inline">
          <p className="fs-prose">{t('No Piper runtime found (neither the piper-tts Python package nor the engine binary).')}</p>
          <Button variant="secondary" size="sm" label={t('Install engine')} loading={busy === 'engine'} disabled={busy !== null} onClick={() => void handleInstallEngine()} />
        </div>
      )}
      {status.dependency_installed && <p className="fs-set__help">{t('Runtime: {runtime}', { runtime: status.runtime || '' })}</p>}
      {installed.length > 0 && (
        <Select
          id="piper-voice"
          value={voice}
          onChange={onSelectVoice}
          options={installed.map((v) => ({ value: v.name, label: `${v.name} (${v.language}, ${v.quality})` }))}
        />
      )}
      {installed.length === 0 && <p className="fs-prose">{t('No Piper voices installed yet. Download one below.')}</p>}
      {notInstalled.length > 0 && (
        <ul className="fs-set__piper-catalogue">
          {notInstalled.map((v) => (
            <li key={v.name} className="fs-set__inline">
              <span>{v.name} ({v.language}, {v.quality})</span>
              <Button
                variant="ghost" size="sm" icon={Download} label={t('Download')}
                loading={busy === v.name} disabled={busy !== null}
                onClick={() => void handleDownload(v.name)}
              />
            </li>
          ))}
        </ul>
      )}
      <Button variant="ghost" size="sm" icon={RefreshCw} label={t('Refresh')} onClick={() => void refresh()} disabled={busy !== null} />
    </div>
  );
}
