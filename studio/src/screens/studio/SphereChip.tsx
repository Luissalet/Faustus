import * as RadixMenu from '@radix-ui/react-dropdown-menu';
import { ExternalLink } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { sphereName, useSpheres } from '../../adapters/spheres';
import { t, useLang } from '../../i18n';
import './sphere-chip.css';

/**
 * The family hub's active sphere, in the Studio header.
 *
 * The person keeps two lives apart in the Hoard Hub (personal / work); this
 * chip shows which one is active, switches it, and opens the hub. It owns no
 * state: the hub does, and a hub that is not there removes the chip.
 */
export function SphereChip() {
  const lang = useLang();
  const { state, select } = useSpheres();
  const [notice, setNotice] = useState('');
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  if (!state) return null;
  const active = state.spheres.find((s) => s.id === state.active) ?? state.spheres[0];
  const label = sphereName(active, lang);

  const pick = async (id: string) => {
    if (id === state.active) return;
    const error = await select(id);
    window.clearTimeout(timer.current);
    if (error === null) {
      setNotice('');
      return;
    }
    setNotice(error === 'hub refused' ? t('The hub refused the switch.') : t('Could not switch the sphere.'));
    timer.current = window.setTimeout(() => setNotice(''), 6000);
  };

  return (
    <RadixMenu.Root onOpenChange={(open) => open && setNotice('')}>
      <RadixMenu.Trigger asChild>
        <button
          type="button"
          className="fs-sphere"
          title={notice || t('Active sphere (Hoard Hub): {name}', { name: label })}
          aria-label={t('Active sphere (Hoard Hub): {name}', { name: label })}
          data-notice={notice ? 'true' : undefined}
          data-testid="sphere-chip"
        >
          <span className="fs-sphere__dot" style={active.color ? { background: active.color } : undefined} aria-hidden="true" />
          <span className="fs-sphere__name">{label}</span>
        </button>
      </RadixMenu.Trigger>
      <RadixMenu.Portal container={document.getElementById('fs-overlay-root') ?? undefined}>
        <RadixMenu.Content className="fs-menu fs-sphere__menu" align="end" sideOffset={6} data-testid="sphere-menu">
          <RadixMenu.Label className="fs-sphere__heading">{t('Sphere')}</RadixMenu.Label>
          <RadixMenu.RadioGroup value={active.id} onValueChange={(id) => void pick(id)}>
            {state.spheres.map((sphere) => (
              <RadixMenu.RadioItem key={sphere.id} value={sphere.id} className="fs-menu__item fs-sphere__item" data-testid={`sphere-item-${sphere.id}`}>
                <span className="fs-sphere__dot" style={sphere.color ? { background: sphere.color } : undefined} aria-hidden="true" />
                <span className="fs-sphere__item-name">{sphereName(sphere, lang)}</span>
                <RadixMenu.ItemIndicator className="fs-sphere__check" aria-hidden="true">✓</RadixMenu.ItemIndicator>
              </RadixMenu.RadioItem>
            ))}
          </RadixMenu.RadioGroup>
          {state.hubUrl && (
            <>
              <RadixMenu.Separator className="fs-menu__sep" />
              <RadixMenu.Item
                className="fs-menu__item"
                data-testid="sphere-open-hub"
                onSelect={() => {
                  window.open(state.hubUrl, '_blank', 'noopener,noreferrer');
                }}
              >
                <ExternalLink size={15} aria-hidden="true" />
                {t('Open in the Hub')}
              </RadixMenu.Item>
            </>
          )}
          {notice && <p className="fs-sphere__notice" role="status">{notice}</p>}
        </RadixMenu.Content>
      </RadixMenu.Portal>
    </RadixMenu.Root>
  );
}
