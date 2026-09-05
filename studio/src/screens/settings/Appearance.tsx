import { Check, Copy, Download, Trash2, Upload } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Button, IconButton } from '../../components';
import { EFFECTS, type EffectName } from '../../shell/effects';
import {
  deleteCustomTheme,
  exportThemeJson,
  FONT_MAP,
  harmonyColors,
  importThemeJson,
  loadCustomFonts,
  MAX_CUSTOM,
  PRESETS,
  saveCustomTheme,
  setTheme,
  themeFromPreset,
  useAppearance,
  type Colors,
  type FontVariant,
  type Harmony,
  type Theme,
} from '../../shell/appearance';
import { setDisplay, useDisplay } from '../../shell/display';
import { t } from '../../i18n';
import { LANGS, setLang, useLang } from '../../i18n';
import { setTheme as setMode, useTheme as useMode, type ThemeChoice } from '../../shell/theme';
import { Field, Select, Toggle } from './fields';

/**
 * Appearance: the previous interface's theme editor, kept compatible
 * (same storage, same presets, same effects), plus Studio's own dials —
 * language and light/dark — and what the transcript shows.
 */
export function AppearanceSection({ say }: { say: (t: string) => void }) {
  const { theme, custom } = useAppearance();
  const lang = useLang();
  const mode = useMode();
  const display = useDisplay();
  const [fonts, setFonts] = useState<Record<string, FontVariant[]>>({});
  useEffect(() => {
    loadCustomFonts().then(setFonts).catch(() => {});
  }, []);

  const colors: Colors = theme.colors ?? PRESETS.dark;
  const own = theme.name === 'studio' || !theme.colors;
  const patch = (p: Partial<Theme>) => setTheme({ ...theme, ...p });
  const setColor = (k: keyof Colors, v: string) => setTheme({ ...theme, name: PRESETS[theme.name] ? 'custom' : theme.name === 'studio' ? 'custom' : theme.name, colors: { ...colors, [k]: v } });

  const MODES: { value: ThemeChoice; label: string }[] = [
    { value: 'system', label: t('Follow the system') },
    { value: 'light', label: t('Light') },
    { value: 'dark', label: t('Dark') },
  ];

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-look">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-look" className="fs-set__title">{t('Appearance')}</h2>
          <p className="fs-prose">{t('Language, light or dark, the palette, type, the background and what the transcript shows. A theme saved in the previous interface is the same theme here.')}</p>
        </div>
      </header>

      <div className="fs-set__card">
        <div className="fs-set__grid2">
          <Field label={t('Interface language')} htmlFor="ui-lang">
            <Select id="ui-lang" value={lang} onChange={(v) => setLang(v as typeof lang)} options={LANGS} />
          </Field>
          <Field label={t('Light or dark')} htmlFor="ui-theme" help={own ? t('Applies to Studio\'s own palette.') : t('A palette below has its own light or dark; this applies when you go back to Studio\'s palette.')}>
            <Select id="ui-theme" value={mode} onChange={(v) => setMode(v as ThemeChoice)} options={MODES} />
          </Field>
        </div>
      </div>

      <div className="fs-set__card">
        <h3 className="fs-set__card-title">{t('Palette')}</h3>
        <div className="fs-themes">
          <button type="button" className="fs-theme" data-on={own || undefined} onClick={() => setTheme({ ...theme, name: 'studio', colors: undefined, bgPattern: undefined, bgEffectColor: undefined })}>
            <span className="fs-theme__swatch fs-theme__swatch--studio" aria-hidden="true" />
            <span className="fs-theme__name">Studio</span>
            {own && <Check size={12} aria-hidden="true" />}
          </button>
          {Object.entries(PRESETS).map(([name, c]) => (
            <button key={name} type="button" className="fs-theme" data-on={(theme.name === name && !own) || undefined} onClick={() => setTheme(themeFromPreset(name))} title={name}>
              <Swatch c={c} />
              <span className="fs-theme__name">{name}</span>
              {theme.name === name && !own && <Check size={12} aria-hidden="true" />}
            </button>
          ))}
        </div>
        {Object.keys(custom).length > 0 && (
          <>
            <h4 className="fs-users__h">{t('Your themes')}</h4>
            <div className="fs-themes">
              {Object.entries(custom).map(([name, ct]) => (
                <span key={name} className="fs-theme fs-theme--custom" data-on={theme.name === name || undefined}>
                  <button type="button" className="fs-theme__pick" onClick={() => setTheme({ ...ct, name })}>
                    <Swatch c={ct.colors ?? PRESETS.dark} />
                    <span className="fs-theme__name">{name}</span>
                  </button>
                  <IconButton icon={Trash2} label={t('Delete {name}', { name })} size="sm" onClick={() => { if (window.confirm(t('Delete the theme "{name}"?', { name }))) deleteCustomTheme(name); }} />
                </span>
              ))}
            </div>
          </>
        )}
      </div>

      <div className="fs-set__card">
        <h3 className="fs-set__card-title">{t('Colours')}</h3>
        <p className="fs-set__help">{own ? t('Pick a preset or touch a colour to start from Studio\'s palette.') : t('Every change applies at once and turns the palette into "custom" until you save it.')}</p>
        <div className="fs-colors">
          {(
            [
              ['bg', 'Background'],
              ['fg', 'Text'],
              ['panel', 'Panel'],
              ['border', 'Border'],
              ['red', 'Accent'],
            ] as [keyof Colors, string][]
          ).map(([k, label]) => (
            <label key={k} className="fs-color">
              <span>{t(label)}</span>
              <input type="color" value={colors[k]} onChange={(e) => setColor(k, e.target.value)} aria-label={t(label)} />
              <code className="fs-tools__id">{colors[k]}</code>
            </label>
          ))}
        </div>
        <HarmonyRow accent={colors.red} onGenerate={(c) => setTheme({ ...theme, name: 'custom', colors: c })} />
      </div>

      <div className="fs-set__card">
        <h3 className="fs-set__card-title">{t('Type and layout')}</h3>
        <div className="fs-set__grid2">
          <Field label={t('Font')} htmlFor="ap-font">
            <Select
              id="ap-font"
              value={theme.font ?? 'studio'}
              onChange={(v) => patch({ font: v })}
              options={[
                { value: 'studio', label: t('Studio (Inter)') },
                { value: 'mono', label: t('Monospace') },
                { value: 'sans', label: t('System sans-serif') },
                { value: 'serif', label: t('Serif') },
                { value: 'opendyslexic', label: 'OpenDyslexic' },
                ...Object.keys(fonts).filter((f) => !(f in FONT_MAP)).map((f) => ({ value: f, label: `${f} (static/fonts/custom)` })),
              ]}
            />
          </Field>
          <Field label={t('Density')} htmlFor="ap-density">
            <Select id="ap-density" value={theme.density ?? 'comfortable'} onChange={(v) => patch({ density: v as Theme['density'] })} options={[{ value: 'compact', label: t('Compact') }, { value: 'comfortable', label: t('Comfortable') }, { value: 'spacious', label: t('Spacious') }]} />
          </Field>
          <Field label={t('Text size')} htmlFor="ap-size">
            <Select id="ap-size" value={theme.textSize ?? '100'} onChange={(v) => patch({ textSize: v === '125' ? '125' : undefined })} options={[{ value: '100', label: t('Default') }, { value: '125', label: t('Larger') }]} />
          </Field>
          <Field label={t('Frosted glass')} htmlFor="ap-frost" help={t('Translucent surfaces with the paper showing through.')}>
            <Toggle id="ap-frost" checked={!!theme.frosted} onChange={(v) => patch({ frosted: v || undefined })} />
          </Field>
        </div>
        <p className="fs-set__help">{t('Drop .woff2, .ttf or .otf files into static/fonts/custom/ and reload: they appear in the font list.')}</p>
      </div>

      <div className="fs-set__card">
        <h3 className="fs-set__card-title">{t('Background')}</h3>
        <div className="fs-set__grid2">
          <Field label={t('Effect')} htmlFor="ap-effect">
            <Select id="ap-effect" value={theme.bgPattern ?? 'none'} onChange={(v) => patch({ bgPattern: v === 'none' ? undefined : (v as EffectName) })} options={EFFECTS.map((e) => ({ value: e, label: t(EFFECT_LABEL[e]) }))} />
          </Field>
          <Field label={t('Effect colour')} htmlFor="ap-effect-color" help={t('Blank follows the text colour.')}>
            <span className="fs-set__inline">
              <input id="ap-effect-color" type="color" value={theme.bgEffectColor ?? colors.fg} onChange={(e) => patch({ bgEffectColor: e.target.value })} />
              <Button size="sm" variant="ghost" label={t('Reset')} onClick={() => patch({ bgEffectColor: undefined })} />
            </span>
          </Field>
          {theme.bgPattern && theme.bgPattern !== 'dots' && (
            <>
              <Field label={t('Intensity')} htmlFor="ap-int">
                <input id="ap-int" type="range" min={0} max={100} step={5} value={Math.round((theme.bgEffectIntensity ?? 1) * 100)} onChange={(e) => patch({ bgEffectIntensity: Number(e.target.value) / 100 })} />
              </Field>
              <Field label={t('Size')} htmlFor="ap-sz">
                <input id="ap-sz" type="range" min={30} max={250} step={10} value={Math.round((theme.bgEffectSize ?? 1) * 100)} onChange={(e) => patch({ bgEffectSize: Number(e.target.value) / 100 })} />
              </Field>
            </>
          )}
        </div>
      </div>

      <SaveShare theme={theme} say={say} />

      <div className="fs-set__card">
        <h3 className="fs-set__card-title">{t('The transcript')}</h3>
        <Field label={t('Thinking blocks')} htmlFor="dp-think" help={t('The model\'s reasoning, folded under each reply.')}>
          <Toggle id="dp-think" checked={display.thinking} onChange={(v) => setDisplay({ thinking: v })} />
        </Field>
        <Field label={t('Emojis in replies')} htmlFor="dp-emoji" help={t('Off strips them from what the model writes.')}>
          <Toggle id="dp-emoji" checked={display.emojis} onChange={(v) => setDisplay({ emojis: v })} />
        </Field>
        <Field label={t('Blur secrets')} htmlFor="dp-blur" help={t('Mails, keys, tokens and private addresses in replies are blurred until you click them.')}>
          <Toggle id="dp-blur" checked={display.blur} onChange={(v) => setDisplay({ blur: v })} />
        </Field>
        <Field label={t('Welcome screen')} htmlFor="dp-welcome" help={t('The greeting and the suggestions on an empty conversation.')}>
          <Toggle id="dp-welcome" checked={display.welcome} onChange={(v) => setDisplay({ welcome: v })} />
        </Field>
        <Field label={t('Full-width transcript')} htmlFor="dp-wide" help={t('Use the whole window instead of a reading column.')}>
          <Toggle id="dp-wide" checked={display.fullWidth} onChange={(v) => setDisplay({ fullWidth: v })} />
        </Field>
      </div>
    </section>
  );
}

const EFFECT_LABEL: Record<EffectName, string> = {
  none: 'Solid',
  dots: 'Dots',
  synapse: 'Synapse',
  rain: 'Rain',
  constellations: 'Constellations',
  'perlin-flow': 'Perlin flow',
  petals: 'Petals',
  sparkles: 'Sparkles',
  embers: 'Embers',
};

function Swatch({ c }: { c: Colors }) {
  return (
    <span className="fs-theme__swatch" aria-hidden="true" style={{ background: c.bg, borderColor: c.border }}>
      <i style={{ background: c.panel }} />
      <i style={{ background: c.fg }} />
      <i style={{ background: c.red }} />
    </span>
  );
}

function HarmonyRow({ accent, onGenerate }: { accent: string; onGenerate: (c: Colors) => void }) {
  const [a, setA] = useState(accent);
  const [h, setH] = useState<Harmony>('complementary');
  const [m, setM] = useState<'dark' | 'light'>('dark');
  return (
    <div className="fs-harmony">
      <span className="fs-set__help">{t('Harmony: a whole palette from one accent.')}</span>
      <input type="color" value={a} onChange={(e) => setA(e.target.value)} aria-label={t('Accent colour')} />
      <select className="fs-field" value={h} onChange={(e) => setH(e.target.value as Harmony)} aria-label={t('Harmony')}>
        <option value="complementary">{t('Complementary')}</option>
        <option value="analogous">{t('Analogous')}</option>
        <option value="triadic">{t('Triadic')}</option>
        <option value="monochromatic">{t('Monochromatic')}</option>
      </select>
      <select className="fs-field" value={m} onChange={(e) => setM(e.target.value as 'dark' | 'light')} aria-label={t('Mode')}>
        <option value="dark">{t('Dark')}</option>
        <option value="light">{t('Light')}</option>
      </select>
      <Button size="sm" variant="secondary" label={t('Generate')} onClick={() => onGenerate(harmonyColors(a, h, m))} />
    </div>
  );
}

function SaveShare({ theme, say }: { theme: Theme; say: (t: string) => void }) {
  const [name, setName] = useState('');
  const [importing, setImporting] = useState(false);
  const [text, setText] = useState('');
  const file = useRef<HTMLInputElement>(null);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(exportThemeJson());
      say(t('Theme JSON copied.'));
    } catch {
      say(t('Could not copy.'));
    }
  };
  const download = () => {
    const blob = new Blob([exportThemeJson()], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `faustus-theme-${theme.name}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  };
  const apply = (raw: string) => {
    try {
      setTheme(importThemeJson(raw));
      setImporting(false);
      setText('');
      say(t('Theme applied.'));
    } catch {
      say(t('That is not a theme JSON.'));
    }
  };
  return (
    <div className="fs-set__card">
      <h3 className="fs-set__card-title">{t('Save and share')}</h3>
      <div className="fs-set__inline">
        <input className="fs-field" value={name} maxLength={32} placeholder={t('Theme name…')} onChange={(e) => setName(e.target.value)} aria-label={t('Theme name')} />
        <Button
          size="sm"
          variant="primary"
          label={t('Save')}
          disabled={!name.trim()}
          onClick={() => {
            if (saveCustomTheme(name)) {
              say(t('Saved as "{name}".', { name: name.trim() }));
              setName('');
            } else say(t('Up to {n} themes; delete one first.', { n: MAX_CUSTOM }));
          }}
        />
      </div>
      <div className="fs-set__row-end" style={{ justifyContent: 'flex-start' }}>
        <Button size="sm" variant="ghost" icon={Copy} label={t('Copy JSON')} onClick={() => void copy()} />
        <Button size="sm" variant="ghost" icon={Download} label={t('Download JSON')} onClick={download} />
        <Button size="sm" variant="ghost" icon={Upload} label={t('Import')} onClick={() => setImporting((v) => !v)} />
        <Button size="sm" variant="ghost" label={t('Import a file')} onClick={() => file.current?.click()} />
        <input ref={file} type="file" accept="application/json,.json" hidden onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; if (f) void f.text().then(apply); }} />
      </div>
      {importing && (
        <>
          <textarea className="fs-field" rows={4} value={text} onChange={(e) => setText(e.target.value)} placeholder={t('Paste the theme JSON here…')} aria-label={t('Theme JSON')} />
          <div className="fs-set__row-end">
            <Button size="sm" variant="ghost" label={t('Cancel')} onClick={() => setImporting(false)} />
            <Button size="sm" variant="primary" label={t('Apply')} disabled={!text.trim()} onClick={() => apply(text)} />
          </div>
        </>
      )}
    </div>
  );
}
