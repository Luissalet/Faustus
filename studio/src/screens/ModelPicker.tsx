import { Command } from 'cmdk';
import { Check, ChevronDown, Cpu } from 'lucide-react';
import { useState } from 'react';
import { overlayRoot } from '../shell/overlayRoot';
import type { ModelRoute } from '../adapters/chat';
import '../shell/palette.css';

/**
 * The model picker is a palette, not a dropdown.
 *
 * Two reasons. A box with forty models in it needs a search field, and
 * cmdk is already in the bundle for Ctrl+K; the Radix dropdown would have
 * pulled floating-ui in for the first time — 80 KB, a quarter of the
 * budget — to draw a list the palette draws better.
 */
export function ModelPicker({
  routes,
  current,
  onPick,
}: {
  routes: ModelRoute[];
  current: ModelRoute | null;
  onPick: (route: ModelRoute) => void;
}) {
  const [open, setOpen] = useState(false);
  const byEndpoint = new Map<string, ModelRoute[]>();
  for (const route of routes) {
    const list = byEndpoint.get(route.endpointName) ?? [];
    list.push(route);
    byEndpoint.set(route.endpointName, list);
  }

  return (
    <>
      <button
        type="button"
        className="fs-studio__chip fs-studio__chip--model"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen(true)}
        data-testid="studio-model"
      >
        <Cpu size={13} aria-hidden="true" />
        <span>{current ? current.model : routes.length ? 'Elegir modelo' : 'Sin modelos'}</span>
        <ChevronDown size={12} aria-hidden="true" />
      </button>
      <Command.Dialog
        open={open}
        onOpenChange={setOpen}
        label="Elegir modelo"
        className="fs-palette"
        container={overlayRoot()}
        data-testid="studio-models"
      >
        <Command.Input placeholder="Buscar modelo…" className="fs-palette__input" />
        <Command.List className="fs-palette__list">
          <Command.Empty className="fs-palette__empty">
            {routes.length ? 'Ningún modelo coincide.' : 'Ningún endpoint responde.'}
          </Command.Empty>
          {[...byEndpoint.entries()].map(([endpoint, list]) => (
            <Command.Group key={endpoint} heading={endpoint} className="fs-palette__group">
              {list.map((route) => (
                <Command.Item
                  key={route.id}
                  value={`${route.model} ${route.endpointName}`}
                  onSelect={() => {
                    onPick(route);
                    setOpen(false);
                  }}
                  className="fs-palette__item"
                  data-testid={`model-${route.model}`}
                >
                  {route.id === current?.id ? (
                    <Check size={15} aria-hidden="true" />
                  ) : (
                    <Cpu size={15} aria-hidden="true" />
                  )}
                  {route.model}
                </Command.Item>
              ))}
            </Command.Group>
          ))}
        </Command.List>
      </Command.Dialog>
    </>
  );
}
