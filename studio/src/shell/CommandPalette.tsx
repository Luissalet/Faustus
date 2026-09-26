import { Command } from 'cmdk';
import { Brain, FileText, Image, KanbanSquare, MessageSquare, NotebookPen, Sparkles } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { SEARCH_SOURCES, searchAll, type SearchHit, type SearchSource } from '../adapters/unifiedSearch';
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
const SOURCE_LABEL: Record<SearchSource, string> = {
  chats: 'Chats',
  brain: 'Brain',
  notes: 'Notes',
  documents: 'Documents',
  gallery: 'Gallery',
  skills: 'Skills',
  board: 'Board',
};

const SOURCE_ICON: Record<SearchSource, typeof Brain> = {
  chats: MessageSquare,
  brain: Brain,
  notes: NotebookPen,
  documents: FileText,
  gallery: Image,
  skills: Sparkles,
  board: KanbanSquare,
};

/** Everything the owner has that matches, mixed (`GET /api/search/all`),
 *  once the query is two characters or more; 250 ms after the last key. */
function useEverywhere(open: boolean, query: string, types: SearchSource[]) {
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [loading, setLoading] = useState(false);
  const q = query.trim();
  const key = types.join(',');
  useEffect(() => {
    if (!open || q.length < 2) {
      setHits([]);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    const timer = window.setTimeout(() => {
      searchAll(q, key ? (key.split(',') as SearchSource[]) : [], 6, controller.signal)
        .then((r) => setHits(r.results ?? []))
        .catch(() => {
          /* navigation still works without it */
        })
        .finally(() => setLoading(false));
    }, 250);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [open, q, key]);
  return { hits, loading };
}

export function CommandPalette() {
  const open = useShell((state) => state.paletteOpen);
  const setOpen = useShell((state) => state.setPaletteOpen);
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const [types, setTypes] = useState<SearchSource[]>([]);
  const { hits, loading } = useEverywhere(open, query, types);
  useEffect(() => {
    if (!open) setQuery('');
  }, [open]);
  const toggleType = (source: SearchSource) =>
    setTypes((current) => (current.includes(source) ? current.filter((s) => s !== source) : [...current, source]));

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
      <Command.Input placeholder={t('Go to, search or run…')} className="fs-palette__input" value={query} onValueChange={setQuery} />
      {query.trim().length >= 2 && (
        <div className="fs-palette__filters" role="group" aria-label={t('Search in')} data-testid="palette-filters">
          <button type="button" className="fs-palette__chip" aria-pressed={types.length === 0} onClick={() => setTypes([])}>
            {t('Everything')}
          </button>
          {SEARCH_SOURCES.map((source) => (
            <button
              key={source}
              type="button"
              className="fs-palette__chip"
              aria-pressed={types.includes(source)}
              onClick={() => toggleType(source)}
              data-testid={`palette-filter-${source}`}
            >
              {t(SOURCE_LABEL[source])}
            </button>
          ))}
        </div>
      )}
      <Command.List className="fs-palette__list">
        {/* cmdk does not count force-mounted items: with server hits on
            screen, "Nothing matches." would sit on top of them. */}
        {hits.length === 0 && (
          <Command.Empty className="fs-palette__empty">{loading ? t('Searching…') : t('Nothing matches.')}</Command.Empty>
        )}

        {hits.length > 0 && (
          <Command.Group heading={t('Everywhere')} className="fs-palette__group" forceMount data-testid="palette-everywhere">
            {hits.map((hit) => {
              const Icon = SOURCE_ICON[hit.type] ?? FileText;
              return (
                <Command.Item
                  key={`${hit.type}:${hit.id}`}
                  value={`${hit.type}:${hit.id}`}
                  forceMount
                  onSelect={() => go(hit.url)}
                  className="fs-palette__item fs-palette__hit"
                  data-testid="palette-hit"
                >
                  <Icon size={15} aria-hidden="true" />
                  <span className="fs-palette__hit-text">
                    <span className="fs-palette__hit-title">{hit.title}</span>
                    {hit.snippet && hit.snippet !== hit.title && <span className="fs-palette__hit-snippet">{hit.snippet}</span>}
                  </span>
                  <span className="fs-palette__hit-type">{t(SOURCE_LABEL[hit.type] ?? hit.type)}</span>
                </Command.Item>
              );
            })}
          </Command.Group>
        )}

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
          {/* Lote 86 (CONTRATO_GIT_4.md): opens the live git panel inside
              the current chat — distinct from the "Source control" entry
              under Tools above, which goes to the standalone full screen. */}
          <Command.Item
            value={`${t('Source control')} ${t('panel')}`}
            onSelect={() => go('/studio?panel=git')}
            className="fs-palette__item"
          >
            {t('Source control')}
          </Command.Item>
          <Command.Item value={`${t('Settings')} ${t('configuration')}`} onSelect={() => go('/settings')} className="fs-palette__item">
            {t('Settings')}
          </Command.Item>
        </Command.Group>
      </Command.List>
    </Command.Dialog>
  );
}
