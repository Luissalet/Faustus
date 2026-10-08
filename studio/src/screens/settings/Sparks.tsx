import { ExternalLink, RefreshCw } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button } from '../../components';
import { loadSparks, saveSparksSettings, syncSparks, type SparksState } from '../../adapters/sparks';
import { t } from '../../i18n';
import { Field, Select, Text, Toggle } from './fields';

/**
 * Settings → Sparks: where Prometheus's Hoard answers, whether the DGX Sparks are the default chat backend while a
 * recipe serves, and which recipe is preferred. The local GPUs keep their models and become the default again when
 * nothing serves on the Sparks (src/sparks.py). Nothing about the cluster is fixed in code.
 */
export function SparksSection({ say }: { say: (text: string) => void }) {
  const [state, setState] = useState<SparksState | null>(null);
  const [url, setUrl] = useState('');
  const [saving, setSaving] = useState(false);
  const [syncing, setSyncing] = useState(false);

  const reload = async () => {
    const s = await loadSparks();
    setState(s);
    if (s) setUrl((u) => u || s.url);
  };
  useEffect(() => {
    void reload();
  }, []);

  const save = async (patch: { enabled?: boolean; url?: string; default_backend?: boolean; recipe?: string }) => {
    setSaving(true);
    const res = await saveSparksSettings(patch);
    setSaving(false);
    if (res.ok === false) say(String(res.error || t('Could not save.')));
    else say(t('Saved.'));
    await reload();
  };

  if (!state) return <p className="fs-prose">{t('Loading')}</p>;
  const recipes = state.recipes.filter((r) => !r.invalid).map((r) => ({ value: r.name, label: r.title }));
  const eff = state.effective;
  const online = state.nodes.filter((n) => n.online).length;

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-sparks" data-testid="settings-sparks">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-sparks" className="fs-set__title">{t('DGX Sparks')}</h2>
          <p className="fs-prose">
            {t("The cluster runs in Prometheus's Hoard. Faustus offers each model it serves as an endpoint and, if you want, makes it the default for new chats; this PC's GPUs keep their own models.")}
          </p>
        </div>
      </header>
      <Field label={t('Status')}>
        <p className="fs-prose" data-testid="sparks-status">
          {!state.enabled
            ? t('Off.')
            : !state.ok
              ? t('Prometheus does not answer at {url}.', { url: state.url })
              : t('{a} of {b} Sparks on · {n} model(s) serving', { a: online, b: state.nodes.length, n: state.deployments.filter((d) => d.state === 'running').length })}
        </p>
      </Field>
      <Field label={t('Use the Sparks')} htmlFor="sparks-on">
        <Toggle id="sparks-on" checked={state.enabled} onChange={(v) => void save({ enabled: v })} label={t('Show the Sparks pill and offer their models')} disabled={saving} />
      </Field>
      <Field label={t("Prometheus's Hoard address")} htmlFor="sparks-url" help={t('Where the app that runs the Sparks answers. Default: http://127.0.0.1:5205.')}>
        <div className="fs-set__inline">
          <Text id="sparks-url" value={url} onChange={setUrl} placeholder="http://127.0.0.1:5205" />
          <Button size="sm" label={t('Save')} disabled={saving || !url || url === state.url} onClick={() => void save({ url })} />
          {state.url && <Button size="sm" variant="ghost" icon={ExternalLink} label={t('Open')} onClick={() => window.open(state.url, '_blank', 'noopener,noreferrer')} />}
        </div>
      </Field>
      <Field label={t('Default backend')} htmlFor="sparks-default" help={t('While a recipe serves on the Sparks, new chats use it. When nothing serves, the default goes back to the model this PC had. Choosing another default by hand turns this off.')}>
        <Toggle id="sparks-default" checked={state.default_backend} onChange={(v) => void save({ default_backend: v })} label={t('The Sparks by default')} disabled={saving || !state.enabled} />
      </Field>
      <Field label={t('Preferred recipe')} htmlFor="sparks-recipe" help={t('When several recipes serve at once, the default goes to this one.')}>
        <Select id="sparks-recipe" value={state.recipe} options={recipes} allowEmpty={t('The one Prometheus marks as default, else the first serving')} onChange={(v) => void save({ recipe: v })} />
      </Field>
      <Field label={t('Default now')}>
        <p className="fs-prose" data-testid="sparks-default-now">
          {eff?.on_sparks ? t('Sparks · {model}', { model: eff.model }) : eff?.model ? t('This PC · {model}', { model: eff.model }) : '—'}
          {state.local_default?.model ? ` · ${t('this PC when the Sparks stop: {model}', { model: state.local_default.model })}` : ''}
        </p>
        <Button
          size="sm"
          variant="secondary"
          icon={RefreshCw}
          label={t('Sync now')}
          loading={syncing}
          onClick={async () => {
            setSyncing(true);
            const res = await syncSparks();
            setSyncing(false);
            say(res.ok === false ? String(res.error) : t('Synced.'));
            await reload();
          }}
        />
      </Field>
    </section>
  );
}
