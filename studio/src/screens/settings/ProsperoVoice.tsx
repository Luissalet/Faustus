import { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { Button } from '../../components';
import { loadProsperoEngines, type ProsperoEngines } from '../../adapters/prospero';
import { t } from '../../i18n';
import { Select } from './fields';

/** Engine picker for `tts_provider = 'prospero'`: reads aloud through the voice
 * engines installed in Prospero's Hoard (KittenTTS on the CPU, Piper, cloning engines). */
export function ProsperoVoice({ engine, onSelectEngine }: { engine: string; onSelectEngine: (id: string) => void }) {
  const [state, setState] = useState<ProsperoEngines | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setState(await loadProsperoEngines());
    } catch {
      setState({ reachable: false, engines: [] });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const options = (state?.engines ?? []).map((e) => ({ value: e.id, label: e.ready ? e.name : `${e.name} (${t('not installed')})` }));
  const current = engine || 'kittentts';
  const known = options.some((o) => o.value === current);
  const picked = state?.engines.find((e) => e.id === current);

  return (
    <div className="fs-set__field" data-testid="prospero-voice">
      <div className="fs-set__inline">
        <Select id="tts-prospero-engine" value={current} onChange={onSelectEngine}
          options={known ? options : [...options, { value: current, label: current }]} />
        <Button variant="ghost" size="sm" label={t('Refresh')} icon={RefreshCw} loading={loading} onClick={() => void refresh()} />
      </div>
      {loading ? <p className="fs-set__help">{t('Asking Prospero\'s Hoard for its voice engines…')}</p>
        : !state?.reachable ? <p className="fs-set__help">{t('Prospero\'s Hoard is not answering. Start it from the Hoard Hub.')}</p>
        : picked && !picked.ready ? <p className="fs-set__help">{picked.reason || t('This engine is not installed in Prospero\'s Hoard.')}</p>
        : <p className="fs-set__help">{t('Reading aloud uses the engine and voice installed in Prospero\'s Hoard.')}</p>}
    </div>
  );
}
