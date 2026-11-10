import { Command } from 'cmdk';
import { Brain, FileText, FolderOpen, Image, KanbanSquare, MessageSquare, NotebookPen, SlidersHorizontal, Sparkles } from 'lucide-react';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { SEARCH_SOURCES, matchRanges, parseScopeCommand, searchAll, type SearchHit, type SearchSource, type SearchWhen } from '../adapters/unifiedSearch';
import { overlayRoot } from './overlayRoot';
import { DESTINATIONS, TOOLS } from './routes';
import { CONTEXT_LABELS, TOOL_PLACEMENT, toolContext, toolHref, toolsIn } from './toolPlacement';
import { useCreatorAvailable } from '../adapters/creator';
import { useShell } from './store';
import { useNavHidden } from './navSize';
import { flipNav } from './navToggle';
import { usePlatform } from './platform';
import { SphereChip } from '../screens/studio/SphereChip';
import { tourForPath } from '../lib/tours';
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
  projects: 'Projects',
  settings: 'Settings',
};

const SOURCE_ICON: Record<SearchSource, typeof Brain> = {
  chats: MessageSquare,
  brain: Brain,
  notes: NotebookPen,
  documents: FileText,
  gallery: Image,
  skills: Sparkles,
  board: KanbanSquare,
  projects: FolderOpen,
  settings: SlidersHorizontal,
};

/** The hit's title with the query's words marked. */
function Highlighted({ text, query }: { text: string; query: string }) {
  const ranges = matchRanges(text, query);
  if (!ranges.length) return <>{text}</>;
  const parts: ReactNode[] = [];
  let at = 0;
  ranges.forEach(([from, to], i) => {
    if (from > at) parts.push(text.slice(at, from));
    parts.push(<mark key={i} className="fs-palette__mark">{text.slice(from, to)}</mark>);
    at = to;
  });
  if (at < text.length) parts.push(text.slice(at));
  return <>{parts}</>;
}

/** Everything the owner has that matches, mixed (`GET /api/search/all`),
 *  once the query is two characters or more; 250 ms after the last key. */
function useEverywhere(open: boolean, query: string, types: SearchSource[], when: SearchWhen = {}) {
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [loading, setLoading] = useState(false);
  const q = query.trim();
  const key = types.join(',');
  const after = when.after ?? '';
  const before = when.before ?? '';
  useEffect(() => {
    if (!open || q.length < 2) {
      setHits([]);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    const timer = window.setTimeout(() => {
      searchAll(q, key ? (key.split(',') as SearchSource[]) : [], 6, controller.signal, { after, before })
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
  }, [open, q, key, after, before]);
  return { hits, loading };
}

export function CommandPalette() {
  const platform = usePlatform();
  const open = useShell((state) => state.paletteOpen);
  const setOpen = useShell((state) => state.setPaletteOpen);
  const navIsHidden = useNavHidden();
  const navigate = useNavigate();
  const { pathname, search } = useLocation();
  const creatorAvailable = useCreatorAvailable();
  const context = toolContext(pathname);
  const projectId = /^\/projects\/([^/]+)/.exec(pathname)?.[1] ?? new URLSearchParams(search).get('project');
  const contextual = toolsIn(context, creatorAvailable === true);
  const [query, setQuery] = useState('');
  const visibleTools = query.trim() ? TOOLS.filter(tool => tool.path !== '/creator' || creatorAvailable) : contextual;
  const [types, setTypes] = useState<SearchSource[]>([]);
  const [sinceDays, setSinceDays] = useState(0);
  const [until, setUntil] = useState('');
  const after = sinceDays ? new Date(Date.now() - sinceDays * 86400000).toISOString().slice(0, 10) : '';
  // `/chats contrato` = only chats; `/` or `/pro` = list the scopes to pick one.
  const scopeCmd = parseScopeCommand(query);
  const searchText = scopeCmd?.kind === 'run' ? scopeCmd.rest : scopeCmd?.kind === 'list' ? '' : query;
  const searchTypes = scopeCmd?.kind === 'run' ? [scopeCmd.scope] : types;
  const { hits, loading } = useEverywhere(open, searchText, searchTypes, { after, before: until });
  useEffect(() => {
    if (!open) setQuery('');
  }, [open]);
  const toggleType = (source: SearchSource) =>
    setTypes((current) => (current.includes(source) ? current.filter((s) => s !== source) : [...current, source]));

  function go(path: string) {
    setOpen(false);
    navigate(path);
  }

  /** `/projects ` in the box, the caret after it, ready for the words. */
  const inputRef = useRef<HTMLInputElement>(null);
  function pickScope(scope: SearchSource) {
    const text = `/${scope} `;
    setQuery(text);
    requestAnimationFrame(() => {
      const el = inputRef.current;
      if (el) { el.focus(); el.setSelectionRange(text.length, text.length); }
    });
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
      <Command.Input ref={inputRef} placeholder={t('Go to, search or run…')} className="fs-palette__input" value={query} onValueChange={setQuery} />
      {query.trim().length >= 2 && !scopeCmd && (
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
      {query.trim().length >= 2 && (scopeCmd?.kind === 'run' ? scopeCmd.scope === 'chats' : !scopeCmd && (types.length === 0 || types.includes('chats'))) && (
        <div className="fs-palette__filters" role="group" aria-label={t('Chats from')} data-testid="palette-when">
          {[[0, 'Any time'], [7, 'Past week'], [30, 'Past month'], [365, 'Past year']].map(([days, label]) => (
            <button key={days} type="button" className="fs-palette__chip" aria-pressed={sinceDays === days}
              onClick={() => setSinceDays(Number(days))} data-testid={`palette-since-${days}`}>
              {t(String(label))}
            </button>
          ))}
          <label className="fs-palette__chip fs-palette__until" title={t('Only what had been said by this date')}>
            {t('As of')}
            <input type="date" value={until} onChange={(e) => setUntil(e.target.value)} aria-label={t('As of date')} data-testid="palette-until" />
          </label>
        </div>
      )}
      <Command.List className="fs-palette__list">
        {/* cmdk does not count force-mounted items: with server hits on
            screen, "Nothing matches." would sit on top of them. */}
        {hits.length === 0 && scopeCmd?.kind !== 'list' && (
          <Command.Empty className="fs-palette__empty">{loading ? t('Searching…') : t('Nothing matches.')}</Command.Empty>
        )}

        {scopeCmd?.kind === 'list' && (
          <Command.Group heading={t('Search only in')} className="fs-palette__group" forceMount data-testid="palette-scopes">
            {scopeCmd.scopes.map((scope) => {
              const Icon = SOURCE_ICON[scope] ?? FileText;
              return (
                <Command.Item
                  key={`scope:${scope}`}
                  value={`scope:${scope}`}
                  forceMount
                  onSelect={() => pickScope(scope)}
                  className="fs-palette__item"
                  data-testid={`palette-scope-${scope}`}
                >
                  <Icon size={15} aria-hidden="true" />
                  <span>/{scope}<small className="fs-palette__tool-context">{t(SOURCE_LABEL[scope])}</small></span>
                </Command.Item>
              );
            })}
          </Command.Group>
        )}

        {hits.length > 0 && (
          <Command.Group heading={scopeCmd?.kind === 'run' ? `${t('Only in')} ${t(SOURCE_LABEL[scopeCmd.scope])}` : t('Everywhere')} className="fs-palette__group" forceMount data-testid="palette-everywhere">
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
                    <span className="fs-palette__hit-title"><Highlighted text={hit.title} query={searchText} /></span>
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

        <Command.Group heading={t(CONTEXT_LABELS[context])} className="fs-palette__group">
          {visibleTools.map((tool) => (
            <Command.Item
              key={tool.path}
              value={`${t(tool.label)} ${t(TOOL_PLACEMENT[tool.path]?.hint ?? '')}`}
              onSelect={() => go(toolHref(tool.path, projectId))}
              className="fs-palette__item"
            >
              <tool.icon size={15} aria-hidden="true" />
              <span>{t(tool.label)}<small className="fs-palette__tool-context">{t(TOOL_PLACEMENT[tool.path]?.hint ?? '')}</small></span>
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
          <Command.Item value={t('Help and tours')} onSelect={() => { setOpen(false); useShell.getState().setTour(tourForPath(pathname, search)?.id ?? 'demo'); }} className="fs-palette__item">
            {t('Help and tours')}
          </Command.Item>
          <Command.Item value={`${t('Settings')} ${t('configuration')}`} onSelect={() => go('/settings')} className="fs-palette__item">
            {t('Settings')}
          </Command.Item>
          {platform !== 'mobile' && (
            <Command.Item value={`${t('Hide the sidebar')} ${t('Show the sidebar')}`} onSelect={() => { setOpen(false); flipNav(); }} className="fs-palette__item" data-testid="palette-nav-toggle">
              {navIsHidden ? t('Show the sidebar') : t('Hide the sidebar')}
            </Command.Item>
          )}
        </Command.Group>
      </Command.List>
      {platform === 'mobile' && <div className="fs-palette__account"><span>{t('Sphere')}</span><SphereChip /></div>}
    </Command.Dialog>
  );
}
