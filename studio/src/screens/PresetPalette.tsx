import { Command } from 'cmdk';
import { Check, Plus, Sparkles, Trash2, UserPen, Wand2, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button } from '../components';
import { deleteTemplate, expandPrompt, getCustomPersona, listPresets, saveCustomPersona, saveTemplate, type CustomPersona, type Preset } from '../adapters/presets';
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
  const [temperature, setTemperature] = useState(1);
  const [maxTokens, setMaxTokens] = useState(0);
  const [busy, setBusy] = useState(false);
  const [expanding, setExpanding] = useState(false);
  const [custom, setCustom] = useState<CustomPersona | null>(null);
  const [customBusy, setCustomBusy] = useState(false);

  const expand = async () => {
    if (!prompt.trim() && !name.trim()) return;
    setExpanding(true);
    try {
      setPrompt(await expandPrompt(name.trim(), prompt.trim()));
    } catch (e) {
      onNotice(`${t('Could not expand the prompt')}: ${(e as Error).message}`, 'danger');
    } finally {
      setExpanding(false);
    }
  };

  const editCustom = () => {
    getCustomPersona()
      .then(setCustom)
      .catch(() => setCustom({ name: '', enabled: true, temperature: 1, maxTokens: 0, systemPrompt: '', injectPrefix: '', injectSuffix: '' }));
  };

  const saveCustom = async () => {
    if (!custom) return;
    setCustomBusy(true);
    try {
      await saveCustomPersona(custom);
      await reload();
      if (custom.enabled) onPick({ id: 'custom', name: custom.name || t('Custom persona'), systemPrompt: custom.systemPrompt, temperature: custom.temperature, maxTokens: custom.maxTokens || undefined, own: false });
      else if (current === 'custom') onPick(null);
      setCustom(null);
      onOpenChange(false);
    } catch (e) {
      onNotice(`${t('Could not save the persona')}: ${(e as Error).message}`, 'danger');
    } finally {
      setCustomBusy(false);
    }
  };

  const reload = () => listPresets().then(setPresets).catch(() => setPresets([]));
  useEffect(() => {
    if (open && presets === null) void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const create = async () => {
    if (!name.trim()) return;
    setBusy(true);
    try {
      const saved = await saveTemplate({ name: name.trim(), systemPrompt: prompt, temperature, maxTokens });
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
          <textarea className="fs-palette__textarea" value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder={t('System prompt: who it is, how it answers, what it avoids… or rough notes, then Expand.')} aria-label={t('System prompt')} rows={6} />
          <div className="fs-palette__row">
            <Button size="sm" variant="ghost" icon={Wand2} label={t('Expand with AI')} loading={expanding} disabled={!prompt.trim() && !name.trim()} onClick={() => void expand()} title={t('Turns your notes into a full system prompt')} />
            <label className="fs-palette__knob">
              <span>{t('Temperature')} {temperature.toFixed(1)}</span>
              <input type="range" min={0} max={2} step={0.1} value={temperature} onChange={(e) => setTemperature(Number(e.target.value))} />
            </label>
            <label className="fs-palette__knob">
              <span>{maxTokens ? t('Max tokens {n}', { n: maxTokens }) : t('Max tokens: no limit')}</span>
              <input type="range" min={0} max={8192} step={256} value={maxTokens} onChange={(e) => setMaxTokens(Number(e.target.value))} />
            </label>
          </div>
          <div className="fs-palette__row">
            <Button size="sm" variant="primary" type="submit" label={t('Save')} loading={busy} disabled={!name.trim()} />
            <Button size="sm" label={t('Cancel')} onClick={() => setCreating(false)} />
          </div>
        </form>
      ) : custom ? (
        <form
          className="fs-palette__form"
          onSubmit={(e) => {
            e.preventDefault();
            void saveCustom();
          }}
        >
          <p className="fs-palette__hint">{t('The custom persona is one ad-hoc character: it is not saved as a template, and it can add text before and after every message you send.')}</p>
          <input className="fs-palette__input" value={custom.name} onChange={(e) => setCustom({ ...custom, name: e.target.value })} placeholder={t('Persona name')} aria-label={t('Name')} autoFocus />
          <textarea className="fs-palette__textarea" value={custom.systemPrompt} onChange={(e) => setCustom({ ...custom, systemPrompt: e.target.value })} placeholder={t('System prompt')} aria-label={t('System prompt')} rows={5} />
          <div className="fs-palette__row">
            <Button size="sm" variant="ghost" icon={Wand2} label={t('Expand with AI')} loading={expanding} disabled={!custom.systemPrompt.trim() && !custom.name.trim()} onClick={() => { setExpanding(true); expandPrompt(custom.name, custom.systemPrompt).then((p) => setCustom((c) => (c ? { ...c, systemPrompt: p } : c))).catch((e: Error) => onNotice(`${t('Could not expand the prompt')}: ${e.message}`, 'danger')).finally(() => setExpanding(false)); }} />
            <label className="fs-palette__knob">
              <span>{t('Temperature')} {custom.temperature.toFixed(1)}</span>
              <input type="range" min={0} max={2} step={0.1} value={custom.temperature} onChange={(e) => setCustom({ ...custom, temperature: Number(e.target.value) })} />
            </label>
            <label className="fs-palette__knob">
              <span>{custom.maxTokens ? t('Max tokens {n}', { n: custom.maxTokens }) : t('Max tokens: no limit')}</span>
              <input type="range" min={0} max={8192} step={256} value={custom.maxTokens} onChange={(e) => setCustom({ ...custom, maxTokens: Number(e.target.value) })} />
            </label>
          </div>
          <input className="fs-palette__input" value={custom.injectPrefix} onChange={(e) => setCustom({ ...custom, injectPrefix: e.target.value })} placeholder={t('Text added before every message (optional)')} aria-label={t('Prefix')} />
          <input className="fs-palette__input" value={custom.injectSuffix} onChange={(e) => setCustom({ ...custom, injectSuffix: e.target.value })} placeholder={t('Text added after every message (optional)')} aria-label={t('Suffix')} />
          <div className="fs-palette__row">
            <label className="fs-switch">
              <input type="checkbox" checked={custom.enabled} onChange={(e) => setCustom({ ...custom, enabled: e.target.checked })} />
              <span>{t('Active')}</span>
            </label>
            <span className="fs-palette__grow" />
            <Button size="sm" variant="primary" type="submit" label={t('Save')} loading={customBusy} />
            <Button size="sm" label={t('Cancel')} onClick={() => setCustom(null)} />
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
              {t('No preset')}
            </Command.Item>
            <Command.Item value="nuevo preset personaje crear new preset persona" onSelect={() => setCreating(true)} className="fs-palette__item" data-testid="preset-new">
              <Plus size={15} aria-hidden="true" />
              {t('New preset or persona…')}
            </Command.Item>
            <Command.Item value="custom persona ad hoc personaje propio prefijo sufijo" onSelect={editCustom} className="fs-palette__item" data-testid="preset-custom">
              {current === 'custom' ? <Check size={15} aria-hidden="true" /> : <UserPen size={15} aria-hidden="true" />}
              {t('Custom persona (prefix, suffix, sampling)…')}
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
