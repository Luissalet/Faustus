import { Command } from 'cmdk';
import { Check, Cpu } from 'lucide-react';
import { overlayRoot } from '../shell/overlayRoot';
import type { ModelRoute } from '../adapters/chat';
import '../shell/palette.css';

/** The searchable list behind the model chip. Lazy: cmdk is ~15 KB gzip
 *  and nobody needs it until the chip is clicked or Ctrl+K is pressed. */
export default function ModelPalette({
  open,
  onOpenChange,
  routes,
  current,
  onPick,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  routes: ModelRoute[];
  current: ModelRoute | null;
  onPick: (route: ModelRoute) => void;
}) {
  const byEndpoint = new Map<string, ModelRoute[]>();
  for (const route of routes) {
    const list = byEndpoint.get(route.endpointName) ?? [];
    list.push(route);
    byEndpoint.set(route.endpointName, list);
  }
  return (
    <Command.Dialog open={open} onOpenChange={onOpenChange} label="Elegir modelo" className="fs-palette" container={overlayRoot()} data-testid="studio-models">
      <Command.Input placeholder="Buscar modelo…" className="fs-palette__input" />
      <Command.List className="fs-palette__list">
        <Command.Empty className="fs-palette__empty">{routes.length ? 'Ningún modelo coincide.' : 'Ningún endpoint responde.'}</Command.Empty>
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
                {route.model}
              </Command.Item>
            ))}
          </Command.Group>
        ))}
      </Command.List>
    </Command.Dialog>
  );
}
