import { X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { IconButton, Skeleton } from '../../components';
import { Field, SaveBar, Text, Toggle } from '../settings/fields';
import { loadBrainSettings, saveBrainSettings, type BrainSettings } from '../../adapters/brain';
import { t } from '../../i18n';

/**
 * The vault's own settings drawer (`GET`/`PUT /api/brain/settings`) — a
 * slide-over rather than a route, since it is a handful of switches for one
 * screen, not a destination of its own. Deliberately its own tiny draft
 * (not `settings/fields.tsx`'s `useDraft`, which is wired to the global
 * `/api/auth/settings` store): this endpoint is a different resource
 * entirely.
 */
export function SettingsDrawer({ onClose }: { onClose: () => void }) {
  const [settings, setSettings] = useState<BrainSettings | null>(null);
  const [draft, setDraft] = useState<Partial<BrainSettings>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    const controller = new AbortController();
    loadBrainSettings(controller.signal).then(setSettings).catch(setError);
    return () => controller.abort();
  }, []);

  const value = { ...settings, ...draft } as BrainSettings;
  const set = <K extends keyof BrainSettings>(key: K, v: BrainSettings[K]) => setDraft((d) => ({ ...d, [key]: v }));
  const dirty = Object.keys(draft).length > 0;

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const saved = await saveBrainSettings(draft);
      setSettings(saved);
      setDraft({});
    } catch (e) {
      setError(e);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fs-brain__drawer-backdrop" role="presentation">
      <button type="button" className="fs-brain__scrim" aria-label={t('Close')} tabIndex={-1} onClick={onClose} />
      <aside className="fs-brain__drawer" role="dialog" aria-modal="true" aria-label={t('Vault settings')} onClick={(e) => e.stopPropagation()} data-testid="brain-settings-drawer">
        <header className="fs-brain__drawer-head">
          <h3>{t('Vault settings')}</h3>
          <IconButton icon={X} label={t('Close')} size="sm" onClick={onClose} />
        </header>

        {!settings && <Skeleton label={t('Reading the settings')} count={4} height="40px" />}
        {error != null && <p className="fs-notice" data-tone="danger">{String((error as Error)?.message ?? error)}</p>}

        {settings && (
          <div className="fs-brain__drawer-body">
            <Field label={t('Vault enabled')} help={t('Turns the whole markdown vault off; nothing is synced or read while it is off.')}>
              <Toggle id="brain-enabled" checked={value.brain_enabled} onChange={(v) => set('brain_enabled', v)} />
            </Field>
            <Field label={t('Vault folder')} help={t('Empty uses the default location under the data directory.')}>
              <Text id="brain-vault-dir" value={value.brain_vault_dir} onChange={(v) => set('brain_vault_dir', v)} placeholder={t('default')} />
            </Field>
            <Field label={t('Sync interval (seconds)')}>
              <Text id="brain-sync-seconds" type="number" value={String(value.brain_vault_sync_seconds)} onChange={(v) => set('brain_vault_sync_seconds', Number(v) || 0)} />
            </Field>
            <Field label={t('Entity extraction')} help={t('Finds entities and relations in memories and notes, deterministically.')}>
              <Toggle id="brain-entity-extraction" checked={value.brain_entity_extraction} onChange={(v) => set('brain_entity_extraction', v)} />
            </Field>
            <Field label={t('Model-assisted extraction')} help={t('Adds a background model pass on top of the deterministic one; never blocks a chat turn.')}>
              <Toggle id="brain-llm-extraction" checked={value.brain_llm_extraction} onChange={(v) => set('brain_llm_extraction', v)} />
            </Field>
            <Field label={t('Entity summaries')} help={t('Lets a background model write the 2-5 sentence entity summary, each claim cited.')}>
              <Toggle id="brain-wiki-summaries" checked={value.brain_wiki_summaries} onChange={(v) => set('brain_wiki_summaries', v)} />
            </Field>
            <Field label={t('Temporal parsing of memories')} help={t('Reads dates like "since March" out of a memory when it is saved.')}>
              <Toggle id="memory-temporal-parse" checked={value.memory_temporal_parse} onChange={(v) => set('memory_temporal_parse', v)} />
            </Field>
            <Field label={t('Supersede on conflict')} help={t('An updated fact closes the previous one instead of leaving both open.')}>
              <Toggle id="memory-temporal-supersede" checked={value.memory_temporal_supersede} onChange={(v) => set('memory_temporal_supersede', v)} />
            </Field>
            <Field label={t('Offer to the context engine')} help={t('Entity cards and matching notes can enter a turn\'s context when they are relevant.')}>
              <Toggle id="brain-context-source" checked={value.brain_context_source} onChange={(v) => set('brain_context_source', v)} />
            </Field>
            <Field label={t('Your name')} help={t('How your own entity is called; memories that mention this name are about you.')}>
              <Text id="owner-display-name" value={value.owner_display_name} onChange={(v) => set('owner_display_name', v)} placeholder={t('Me')} />
            </Field>
            <SaveBar dirty={dirty} saving={saving} onSave={() => void save()} />
          </div>
        )}
      </aside>
    </div>
  );
}
