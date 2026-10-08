import { ExternalLink, RefreshCw } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button } from '../../components';
import { loadSparks, saveSparksSettings, syncSparks, type SparksState } from '../../adapters/sparks';
import { t } from '../../i18n';
import { Field, Select, Text, Toggle } from './fields';

const FIRST_TOKEN_MIN = 5;
const FIRST_TOKEN_MAX = 120;
const FIRST_TOKEN_DEFAULT = 30;

/**
 * Settings → Sparks: where Prometheus's Hoard answers, whether the DGX Sparks are the default chat backend while a
 * recipe serves, and which recipe is preferred. The local GPUs keep their models and become the default again when
 * nothing serves on the Sparks (src/sparks.py). Nothing about the cluster is fixed in code.
 *
 * `first_token_timeout_s` comes from the typed Sparks adapter (#106 on master).
 */
export function SparksSection({ say }: { say: (text: string) => void }) {
  const [state, setState] = useState<SparksState | null>(null);
  const [url, setUrl] = useState('');
  const [saving, setSaving] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [firstTokenDraft, setFirstTokenDraft] = useState(String(FIRST_TOKEN_DEFAULT));

  const reload = async () => {
    const s = await loadSparks();
    setState(s);
    if (s) {
      setUrl((u) => u || s.url);
      const n = typeof s.first_token_timeout_s === 'number' && Number.isFinite(s.first_token_timeout_s)
        ? Math.min(FIRST_TOKEN_MAX, Math.max(FIRST_TOKEN_MIN, Math.round(s.first_token_timeout_s)))
        : FIRST_TOKEN_DEFAULT;
      setFirstTokenDraft(String(n));
    }
  };
  useEffect(() => {
    void reload();
  }, []);

  const save = async (patch: { enabled?: boolean; url?: string; default_backend?: boolean; recipe?: string; first_token_timeout_s?: number }) => {
    setSaving(true);
    const res = await saveSparksSettings(patch);
    setSaving(false);
    if (res.ok === false) say(String(res.error || t('Could not save.')));
    else say(t('Saved.'));
    await reload();
  };

  const saveFirstToken = async () => {
    const n = Math.round(Number(String(firstTokenDraft).replace(',', '.')));
    if (!Number.isFinite(n) || n < FIRST_TOKEN_MIN || n > FIRST_TOKEN_MAX) {
      say(t('Could not save.'));
      return;
    }
    await save({ first_token_timeout_s: n });
  };

  if (!state) return <p className="fs-prose">{t('Loading')}</p>;
  const recipes = state.recipes.filter((r) => !r.invalid).map((r) => ({ value: r.name, label: r.title }));
  const eff = state.effective;
  const online = state.nodes.filter((n) => n.online).length;
  const shownTimeout = typeof state.first_token_timeout_s === 'number' ? state.first_token_timeout_s : FIRST_TOKEN_DEFAULT;

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
      <Field
        label={t('Initial wait (seconds)#sparks')}
        htmlFor="sparks-first-token"
        help={t('How long a Sparks turn may wait in the queue and while the model prepares before the first token. Default {n} s ({min}–{max}). When the limit expires you get a notice; you can shrink the context or pick another model. There is no automatic retry.', { n: FIRST_TOKEN_DEFAULT, min: FIRST_TOKEN_MIN, max: FIRST_TOKEN_MAX })}
      >
        <div className="fs-set__inline">
          <Text
            id="sparks-first-token"
            value={firstTokenDraft}
            onChange={setFirstTokenDraft}
            placeholder={String(FIRST_TOKEN_DEFAULT)}
          />
          <Button
            size="sm"
            label={t('Save')}
            disabled={saving || firstTokenDraft === String(shownTimeout)}
            onClick={() => void saveFirstToken()}
            testId="sparks-first-token-save"
          />
          {typeof state.first_token_timeout_s !== 'number' && (
            <span className="fs-prose" data-testid="sparks-first-token-proposed">
              {FIRST_TOKEN_DEFAULT} s ({FIRST_TOKEN_MIN}–{FIRST_TOKEN_MAX})
            </span>
          )}
        </div>
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
