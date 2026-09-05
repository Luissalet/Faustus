import { Command } from 'cmdk';
import { useNavigate } from 'react-router';
import { overlayRoot } from './overlayRoot';
import { DESTINATIONS, TOOLS, toolHref } from './routes';
import { useShell } from './store';
import './palette.css';

/**
 * Command palette (UI-022).
 *
 * Ctrl/Cmd+K reaches every destination and every action, so the essential
 * navigation of the app can be completed without a mouse.
 *
 * "Buscar conversaciones" used to own this shortcut in the legacy UI. It
 * becomes a command inside the palette rather than a rival binding: two
 * things fighting over one key means the user learns neither.
 */
export function CommandPalette() {
  const open = useShell((state) => state.paletteOpen);
  const setOpen = useShell((state) => state.setPaletteOpen);
  const navigate = useNavigate();

  function go(path: string) {
    setOpen(false);
    navigate(path);
  }

  return (
    <Command.Dialog
      open={open}
      onOpenChange={setOpen}
      label="Buscar y navegar"
      className="fs-palette"
      container={overlayRoot()}
      data-testid="command-palette"
    >
      <Command.Input placeholder="Ir a, buscar o ejecutar…" className="fs-palette__input" />
      <Command.List className="fs-palette__list">
        <Command.Empty className="fs-palette__empty">Nada coincide.</Command.Empty>

        <Command.Group heading="Ir a" className="fs-palette__group">
          {DESTINATIONS.map((destination) => (
            <Command.Item
              key={destination.path}
              value={destination.label}
              onSelect={() => go(destination.path)}
              className="fs-palette__item"
            >
              <destination.icon size={15} aria-hidden="true" />
              {destination.label}
            </Command.Item>
          ))}
        </Command.Group>

        <Command.Group heading="Herramientas" className="fs-palette__group">
          {TOOLS.map((tool) => (
            <Command.Item
              key={tool.path}
              value={`${tool.label} herramienta`}
              onSelect={() => {
                if (tool.ready) go(tool.path);
                else {
                  setOpen(false);
                  window.location.href = toolHref(tool);
                }
              }}
              className="fs-palette__item"
            >
              <tool.icon size={15} aria-hidden="true" />
              {tool.label}
              {!tool.ready && <span className="fs-palette__note">interfaz anterior</span>}
            </Command.Item>
          ))}
        </Command.Group>

        <Command.Group heading="Acciones" className="fs-palette__group">
          <Command.Item
            value="Nueva conversación"
            onSelect={() => go('/studio')}
            className="fs-palette__item"
          >
            Nueva conversación
          </Command.Item>
          <Command.Item
            value="Buscar conversaciones"
            onSelect={() => go('/studio?buscar=1')}
            className="fs-palette__item"
          >
            Buscar conversaciones
          </Command.Item>
        </Command.Group>
      </Command.List>
    </Command.Dialog>
  );
}
