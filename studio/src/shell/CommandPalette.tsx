import { Command } from 'cmdk';
import { useNavigate } from 'react-router';
import { overlayRoot } from './overlayRoot';
import { DESTINATIONS, TOOLS } from './routes';
import { useShell } from './store';
import './palette.css';
import { t } from '../i18n';

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
      label={t('Search and navigate')}
      className="fs-palette"
      container={overlayRoot()}
      data-testid="command-palette"
    >
      <Command.Input placeholder={t('Go to, search or run…')} className="fs-palette__input" />
      <Command.List className="fs-palette__list">
        <Command.Empty className="fs-palette__empty">{t('Nothing matches.')}</Command.Empty>

        <Command.Group heading={t('Go to')} className="fs-palette__group">
          {DESTINATIONS.map((destination) => (
            <Command.Item
              key={destination.path}
              value={t(destination.label)}
              onSelect={() => go(destination.path)}
              className="fs-palette__item"
            >
              <destination.icon size={15} aria-hidden="true" />
              {t(destination.label)}
            </Command.Item>
          ))}
        </Command.Group>

        <Command.Group heading={t('Tools')} className="fs-palette__group">
          {TOOLS.map((tool) => (
            <Command.Item
              key={tool.path}
              value={`${t(tool.label)} ${t('tool')}`}
              onSelect={() => go(tool.path)}
              className="fs-palette__item"
            >
              <tool.icon size={15} aria-hidden="true" />
              {t(tool.label)}
            </Command.Item>
          ))}
        </Command.Group>

        <Command.Group heading={t('Actions')} className="fs-palette__group">
          <Command.Item
            value={t('New conversation')}
            onSelect={() => go('/studio')}
            className="fs-palette__item"
          >
            {t('New conversation')}
          </Command.Item>
          <Command.Item
            value={t('Search conversations')}
            onSelect={() => go('/studio?buscar=1')}
            className="fs-palette__item"
          >
            {t('Search conversations')}
          </Command.Item>
          <Command.Item value={`${t('Settings')} ${t('configuration')}`} onSelect={() => go('/settings')} className="fs-palette__item">
            {t('Settings')}
          </Command.Item>
        </Command.Group>
      </Command.List>
    </Command.Dialog>
  );
}
