import { t } from '../i18n';
import { Command } from 'cmdk';
import { Check, Cloud, Coins, Cpu, Lock, Plus, RefreshCw } from 'lucide-react';
import { useRef, useState } from 'react';
import { overlayRoot } from '../shell/overlayRoot';
import type { ModelRoute } from '../adapters/chat';
import {
  aliasesOf, costLabel, FIT_EXPLAIN_CAP_LABEL, FIT_WORD, fetchFitExplain, fitOf, fitSize,
  useEndpointProfiles, useFitHints,
  type FitExplain,
} from '../adapters/fit';
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
  onConnect,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  routes: ModelRoute[];
  current: ModelRoute | null;
  onPick: (route: ModelRoute) => void;
  /** Ask every endpoint again (the old picker's ↻). */
  onRefresh?: () => void;
  refreshing?: boolean;
  onConnect?: () => void;
}) {
  // Will it fit on this card? Read once each time the picker opens; the
  // server answers nothing at all when it cannot tell, and nothing is drawn.
  const fit = useFitHints(open);
  // SET-02: privacy + cost, one cheap DB-only read for every endpoint —
  // same "once per open" shape as fit above.
  const profiles = useEndpointProfiles(open);
  // CMP-11: capability fit is NOT prefetched — one round trip per row would
  // hammer every endpoint the moment the palette opens — fetched on
  // hover/focus, once per row, kept here for the life of the open palette.
  // Local AND remote rows both ask: a remote endpoint at least carries
  // `supports_tools`, and every requirement it has no evidence for comes
  // back `unknown` rather than being skipped.
  const [caps, setCaps] = useState<Record<string, FitExplain>>({});
  const capsRequested = useRef(new Set<string>());
  const requestCaps = (route: ModelRoute) => {
    if (capsRequested.current.has(route.id)) return;
    capsRequested.current.add(route.id);
    void fetchFitExplain(route.model, route.endpointId)
      .then((explain) => setCaps((prev) => ({ ...prev, [route.id]: explain })))
      .catch(() => undefined); // best-effort: a failed read leaves the row with no badge, not an error
  };
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
        {onConnect && <Command.Item value="connect conectar provider proveedor claude API" onSelect={onConnect} className="fs-palette__item">
          <Plus size={15} aria-hidden="true" />{t('Connect an AI provider')}
        </Command.Item>}
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
              const profile = profiles[route.endpointId];
              const explain = caps[route.id];
              const reasons = explain?.reasons ?? [];
              return (
                <Command.Item
                  key={route.id}
                  value={`${route.model} ${route.endpointName}`}
                  onSelect={() => {
                    onPick(route);
                    onOpenChange(false);
                  }}
                  onMouseEnter={() => requestCaps(route)}
                  onFocus={() => requestCaps(route)}
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
                  {profile && (
                    <span className="fs-palette__privacy" data-local={profile.isLocal || undefined} title={costLabel(profile.cost)}>
                      {profile.isLocal ? <Lock size={12} aria-hidden="true" /> : <Cloud size={12} aria-hidden="true" />}
                      {profile.cost === 'paid' && <Coins size={12} aria-hidden="true" />}
                    </span>
                  )}
                  {/* CMP-11 (INFORME V2 §3.10): each requested capability gets its
                      own badge, never a merged yes/no — `announced`/`unknown`
                      are shown, not hidden, and the tooltip carries the CAUSE
                      plus, for a `missing` one, which other model does meet it. */}
                  {reasons.length > 0 && (
                    <span className="fs-palette__caps">
                      {reasons.map((reason) => (
                        <span
                          key={reason.capability}
                          className="fs-palette__cap"
                          data-state={reason.state}
                          title={reason.alternatives.length
                            ? `${reason.message} ${t('Alternatives: {names}', { names: reason.alternatives.join(', ') })}`
                            : reason.message}
                        >
                          {FIT_EXPLAIN_CAP_LABEL[reason.capability] ?? reason.capability}
                        </span>
                      ))}
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
