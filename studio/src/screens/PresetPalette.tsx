import { Command } from 'cmdk';
import { Check, Plus, Sparkles, Trash2, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button } from '../components';
import { deleteTemplate, listPresets, saveTemplate, type Preset } from '../adapters/presets';
import { overlayRoot } from '../shell/overlayRoot';
import '../shell/palette.css';
import { t } from '../i18n';

/**
 * Presets and personas behind the composer chip: the built-in ones
 * (/api/presets) and the person's own templates, which can be written and
 * deleted right here. Lazy, like the model palette.
 */
export default function PresetPalette({
  open,
  onOpenChange,
  current,
  onPick,
  onNotice,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  current: string | null;
  onPick: (preset: Preset | null) => void;
  onNotice: (text: string, tone?: 'info' | 'warning' | 'danger') => void;
}) {
  const [presets, setPresets] = useState<Preset[] | null>(null);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [prompt, setPrompt] = useState('');
  const [busy, setBusy] = useState(false);

  const reload = () => listPresets().then(setPresets).catch(() => setPresets([]));
  useEffect(() => {
    if (open && presets === null) void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const create = async () => {
    if (!name.trim()) return;
    setBusy(true);
    try {
      const saved = await saveTemplate({ name: name.trim(), systemPrompt: prompt });
      setName('');
      setPrompt('');
      setCreating(false);
      await reload();
      onPick(saved);
      onOpenChange(false);
    } catch (e) {
      onNotice(`${t('Could not save the preset')}: ${(e as Error).message}`, 'danger');
    } finally {
      setBusy(false);
    }
  };

  const builtin = (presets ?? []).filter((p) => !p.own);
  const own = (presets ?? []).filter((p) => p.own);

  return (
    <Command.Dialog open={open} onOpenChange={onOpenChange} label={t('Choose preset')} className="fs-palette" container={overlayRoot()} data-testid="studio-presets">
      {creating ? (
        <form
          className="fs-palette__form"
          onSubmit={(e) => {
            e.preventDefault();
            void create();
          }}
        >
          <input className="fs-palette__input" value={name} onChange={(e) => setName(e.target.value)} placeholder={t('Preset name')} aria-label={t('Name')} autoFocus />
          <textarea className="fs-palette__textarea" value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder={t('System prompt: who it is, how it answers, what it avoids…')} aria-label={t('System prompt')} rows={6} />
          <div className="fs-palette__row">
            <Button size="sm" variant="primary" type="submit" label={t('Save')} loading={busy} disabled={!name.trim()} />
            <Button size="sm" label={t('Cancel')} onClick={() => setCreating(false)} />
          </div>
        </form>
      ) : (
        <>
          <Command.Input placeholder={t('Search preset or persona…')} className="fs-palette__input" />
          <Command.List className="fs-palette__list">
            <Command.Empty className="fs-palette__empty">{presets === null ? t('Loading…') : t('No preset matches.')}</Command.Empty>
            <Command.Item
              value="sin preset ninguno quitar"
              onSelect={() => {
                onPick(null);
                onOpenChange(false);
              }}
              className="fs-palette__item"
            >
              {current ? <X size={15} aria-hidden="true" /> : <Check size={15} aria-hidden="true" />}
              Sin preset
            </Command.Item>
            <Command.Item value="nuevo preset personaje crear" onSelect={() => setCreating(true)} className="fs-palette__item" data-testid="preset-new">
              <Plus size={15} aria-hidden="true" />
              Nuevo preset o personaje…
            </Command.Item>
            {own.length > 0 && (
              <Command.Group heading={t('Yours')} className="fs-palette__group">
                {own.map((p) => (
                  <Command.Item
                    key={p.id}
                    value={`${p.name} ${p.systemPrompt.slice(0, 80)}`}
                    onSelect={() => {
                      onPick(p);
                      onOpenChange(false);
                    }}
                    className="fs-palette__item"
                    data-testid={`preset-${p.id}`}
                  >
                    {p.id === current ? <Check size={15} aria-hidden="true" /> : <Sparkles size={15} aria-hidden="true" />}
                    <span className="fs-palette__grow">{p.name}</span>
                    <button
                      type="button"
                      className="fs-palette__mini"
                      aria-label={t('Delete {name}', { name: p.name })}
                      onClick={(e) => {
                        e.stopPropagation();
                        deleteTemplate(p.id)
                          .then(() => {
                            if (p.id === current) onPick(null);
                            void reload();
                          })
                          .catch(() => onNotice(t('Could not delete the preset.'), 'danger'));
                      }}
                    >
                      <Trash2 size={13} aria-hidden="true" />
                    </button>
                  </Command.Item>
                ))}
              </Command.Group>
            )}
            {builtin.length > 0 && (
              <Command.Group heading={t('Included')} className="fs-palette__group">
                {builtin.map((p) => (
                  <Command.Item
                    key={p.id}
                    value={`${p.name} ${p.systemPrompt.slice(0, 80)}`}
                    onSelect={() => {
                      onPick(p);
                      onOpenChange(false);
                    }}
                    className="fs-palette__item"
                    data-testid={`preset-${p.id}`}
                    title={p.systemPrompt.slice(0, 300)}
                  >
                    {p.id === current ? <Check size={15} aria-hidden="true" /> : <Sparkles size={15} aria-hidden="true" />}
                    <span className="fs-palette__grow">{p.name}</span>
                    {p.temperature !== undefined && <span className="fs-palette__hint">T {p.temperature}</span>}
                  </Command.Item>
                ))}
              </Command.Group>
            )}
          </Command.List>
        </>
      )}
    </Command.Dialog>
  );
}
