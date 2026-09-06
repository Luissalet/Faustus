import { t } from '../i18n';
import { Command } from 'cmdk';
import { Check, Cpu, RefreshCw } from 'lucide-react';
import { overlayRoot } from '../shell/overlayRoot';
import type { ModelRoute } from '../adapters/chat';
import { aliasesOf, FIT_WORD, fitOf, fitSize, useFitHints } from '../adapters/fit';
import { fmtGb, shortGpuName } from '../adapters/localModels';
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
  const cards = fit.vram.count ?? 1;
  return (
    <Command.Dialog open={open} onOpenChange={onOpenChange} label={t('Choose model')} className="fs-palette" container={overlayRoot()} data-testid="studio-models">
      <Command.Input placeholder={t('Search model…')} className="fs-palette__input" />
      {/* "No room" is only an answer if you can see what the room is. The
          budget is what the verdicts are measured against, so it is stated
          once, above the list, instead of being hidden in every tooltip. */}
      {fit.vram.supported && fit.vram.budgetBytes ? (
        <p className="fs-palette__vram">
          {cards > 1
            ? t('{free} usable across {n} GPUs of {total}. Each row shows the weights on disk; the context window grows on top of them.', { free: fmtGb(fit.vram.budgetBytes), n: cards, total: fmtGb(fit.vram.totalBytes) })
            : t('{free} usable of {total} on {gpu}. Each row shows the weights on disk; the context window grows on top of them.', { free: fmtGb(fit.vram.budgetBytes), total: fmtGb(fit.vram.totalBytes), gpu: shortGpuName(fit.vram.name) || t('the card') })}
        </p>
      ) : null}
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
            {list.map((route) => {
              // One lookup per row. `fitOf` answers nothing at all for a model
              // served from another machine: the size and the verdict are both
              // about the card in THIS box.
              const hint = fitOf(route, fit);
              const alias = hint ? aliasesOf(route.model, fit) : [];
              const size = fitSize(hint);
              return (
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
                  {alias.length > 0 && (
                    <span className="fs-palette__alias" title={t('The same weights as {names}', { names: alias.join(', ') })}>
                      {t('same as {name}', { name: alias[0] })}
                    </span>
                  )}
                  {size && (
                    <span className="fs-palette__size" title={hint?.note ?? t('{size} on disk. Approximate — the KV cache grows on top of it with the context window.', { size })}>
                      {size}
                    </span>
                  )}
                  {hint?.state && (
                    <span className="fs-palette__fit" data-fit={hint.state} title={hint.note}>
                      {t(FIT_WORD[hint.state])}
                    </span>
                  )}
                </Command.Item>
              );
            })}
          </Command.Group>
        ))}
      </Command.List>
    </Command.Dialog>
  );
}
