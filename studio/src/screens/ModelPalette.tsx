import { t } from '../i18n';
import { Command } from 'cmdk';
import { Check, Cpu, RefreshCw } from 'lucide-react';
import { overlayRoot } from '../shell/overlayRoot';
import type { ModelRoute } from '../adapters/chat';
import { aliasesOf, FIT_WORD, useFitHints } from '../adapters/fit';
import '../shell/palette.css';

/** The searchable list behind the model chip. Lazy: cmdk is ~15 KB gzip
 *  and nobody needs it until the chip is clicked or Ctrl+K is pressed. */
export default function ModelPalette({
  open,
  onOpenChange,
  routes,
  current,
  onPick,
  onRefresh,
  refreshing = false,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  routes: ModelRoute[];
  current: ModelRoute | null;
  onPick: (route: ModelRoute) => void;
  /** Ask every endpoint again (the old picker's ↻). */
  onRefresh?: () => void;
  refreshing?: boolean;
}) {
  // Will it fit on this card? Read once each time the picker opens; the
  // server answers nothing at all when it cannot tell, and nothing is drawn.
  const fit = useFitHints(open);
  const byEndpoint = new Map<string, ModelRoute[]>();
  for (const route of routes) {
    const list = byEndpoint.get(route.endpointName) ?? [];
    list.push(route);
    byEndpoint.set(route.endpointName, list);
  }
  return (
    <Command.Dialog open={open} onOpenChange={onOpenChange} label={t('Choose model')} className="fs-palette" container={overlayRoot()} data-testid="studio-models">
      <Command.Input placeholder={t('Search model…')} className="fs-palette__input" />
      <Command.List className="fs-palette__list">
        <Command.Empty className="fs-palette__empty">{routes.length ? t('No model matches.') : t('No endpoint responds.')}</Command.Empty>
        {onRefresh && (
          <Command.Item value="refrescar modelos endpoints" onSelect={onRefresh} className="fs-palette__item" data-testid="model-refresh" disabled={refreshing}>
            <RefreshCw size={15} aria-hidden="true" />
            {refreshing ? t('Asking the endpoints…') : t('Refresh the model list')}
          </Command.Item>
        )}
        {[...byEndpoint.entries()].map(([endpoint, list]) => (
          <Command.Group key={endpoint} heading={endpoint} className="fs-palette__group">
            {list.map((route) => (
              <Command.Item
                key={route.id}
                value={`${route.model} ${route.endpointName}`}
                onSelect={() => {
                  onPick(route);
                  onOpenChange(false);
                }}
                className="fs-palette__item"
                data-testid={`model-${route.model}`}
              >
                {route.id === current?.id ? <Check size={15} aria-hidden="true" /> : <Cpu size={15} aria-hidden="true" />}
                <span className="fs-palette__name">{route.model}</span>
                {aliasesOf(route.model, fit).length > 0 && (
                  <span className="fs-palette__alias" title={t('The same weights as {names}', { names: aliasesOf(route.model, fit).join(', ') })}>
                    {t('same as {name}', { name: aliasesOf(route.model, fit)[0] })}
                  </span>
                )}
                {fit.models[route.model]?.state && (
                  <span className="fs-palette__fit" data-fit={fit.models[route.model]?.state} title={fit.models[route.model]?.note}>
                    {t(FIT_WORD[fit.models[route.model]!.state!])}
                  </span>
                )}
              </Command.Item>
            ))}
          </Command.Group>
        ))}
      </Command.List>
    </Command.Dialog>
  );
}
