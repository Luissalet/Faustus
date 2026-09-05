import { syncLangFromServer, t, useLang } from '../i18n';
import { clearThemeAttribute, syncThemeFromServer } from './theme';
import { applyTheme, getTheme, syncAppearanceFromServer, useAppearance } from './appearance';
import { useNavSize } from './navSize';
import { LogOut, Settings2 } from 'lucide-react';
import { lazy, Suspense, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { BrowserRouter, NavLink, Route, Routes, useLocation } from 'react-router';
import { Button, Skeleton } from '../components';
import { HomeScreen } from '../screens/Home';
import { NotMigrated } from '../screens/NotMigrated';

import { BrandMark } from './BrandMark';
import { DESTINATIONS, TOOLS, toolHref } from './routes';
import { setStudioEnabled } from './flag';
import { ensureOverlayRoot, removeOverlayRoot } from './overlayRoot';
import { useShell } from './store';

/* Inicio is the eager bundle; every other screen arrives as a route chunk
   the first time it is opened, so the eager bundle stays inside the 350 KB
   budget (DECISIONES_UI.md) as screens keep landing. Studio, the heaviest,
   is fetched right after the first paint (`warmStudio`) so opening it from
   Inicio costs nothing perceptible. */
const loadStudio = () => import('../screens/Studio');
const StudioScreen = lazy(() => loadStudio().then((m) => ({ default: m.StudioScreen })));
const ActivityScreen = lazy(() => import('../screens/Activity').then((m) => ({ default: m.ActivityScreen })));
const AutomationsScreen = lazy(() => import('../screens/Automations').then((m) => ({ default: m.AutomationsScreen })));
const LibraryScreen = lazy(() => import('../screens/Library').then((m) => ({ default: m.LibraryScreen })));
const EditorScreen = lazy(() => import('../screens/library/editor/Editor').then((m) => ({ default: m.EditorScreen })));
const DocumentScreen = lazy(() => import('../screens/documents/Editor').then((m) => ({ default: m.DocumentScreen })));
const ProjectScreen = lazy(() => import('../screens/Project').then((m) => ({ default: m.ProjectScreen })));
const ProjectsScreen = lazy(() => import('../screens/Projects').then((m) => ({ default: m.ProjectsScreen })));
const NotesScreen = lazy(() => import('../screens/Notes').then((m) => ({ default: m.NotesScreen })));
const MemoryScreen = lazy(() => import('../screens/Memory').then((m) => ({ default: m.MemoryScreen })));
const CalendarScreen = lazy(() => import('../screens/Calendar').then((m) => ({ default: m.CalendarScreen })));
const EmailScreen = lazy(() => import('../screens/email/Email').then((m) => ({ default: m.EmailScreen })));
const ResearchScreen = lazy(() => import('../screens/research/Research').then((m) => ({ default: m.ResearchScreen })));
const CompareScreen = lazy(() => import('../screens/compare/Compare').then((m) => ({ default: m.CompareScreen })));
const GroupScreen = lazy(() => import('../screens/group/Group').then((m) => ({ default: m.GroupScreen })));
const SettingsScreen = lazy(() => import('../screens/Settings').then((m) => ({ default: m.SettingsScreen })));
const AgentsScreen = lazy(() => import('../screens/Agents').then((m) => ({ default: m.AgentsScreen })));
const SkillsScreen = lazy(() => import('../screens/Skills').then((m) => ({ default: m.SkillsScreen })));
/* cmdk rides in with the first Ctrl+K, not with the page. */
const CommandPalette = lazy(() => import('./CommandPalette').then((m) => ({ default: m.CommandPalette })));

/**
 * The rail.
 *
 * The signature motif — a line with nodes on it — carries the navigation.
 * The coral indicator is one element that slides to whichever node is
 * active, measured from the DOM rather than guessed, so it lands exactly on
 * the node in every layout: the full sidebar, the collapsed rail, and the
 * horizontal bottom bar on a phone.
 */
function Rail() {
  const { pathname } = useLocation();
  const navRef = useRef<HTMLElement>(null);
  const [indicator, setIndicator] = useState<{ x: number; y: number } | null>(null);
  const [rail, setRail] = useState<{ x: number; y: number; w: number; h: number } | null>(null);

  useLayoutEffect(() => {
    const nav = navRef.current;
    if (!nav) return;

    function centre(el: Element, navRect: DOMRect) {
      const rect = el.getBoundingClientRect();
      return {
        x: rect.left - navRect.left + rect.width / 2 + (nav?.scrollLeft ?? 0),
        y: rect.top - navRect.top + rect.height / 2 + (nav?.scrollTop ?? 0),
      };
    }

    function place() {
      if (!nav) return;
      const navRect = nav.getBoundingClientRect();
      const nodes = nav.querySelectorAll<HTMLElement>('.fs-nav__node');

      // The line runs from the first node to the last, in whichever direction
      // the layout laid them out — a column on desktop, a row on a phone.
      if (nodes.length >= 2) {
        const first = centre(nodes[0], navRect);
        const last = centre(nodes[nodes.length - 1], navRect);
        const horizontal = Math.abs(last.x - first.x) > Math.abs(last.y - first.y);
        setRail(
          horizontal
            ? { x: first.x, y: first.y - 1, w: last.x - first.x, h: 2 }
            : { x: first.x - 1, y: first.y, w: 2, h: last.y - first.y },
        );
      }

      const active = nav.querySelector<HTMLElement>('.fs-nav__item[aria-current="page"] .fs-nav__node');
      if (!active) {
        setIndicator(null);
        return;
      }
      const c = centre(active, navRect);
      setIndicator({ x: c.x - 6, y: c.y - 6 });
    }

    place();
    const observer = new ResizeObserver(place);
    observer.observe(nav);
    window.addEventListener('resize', place);
    return () => {
      observer.disconnect();
      window.removeEventListener('resize', place);
    };
  }, [pathname]);

  return (
    <nav className="fs-nav" aria-label={t('Main navigation')} ref={navRef}>
      <div className="fs-nav__brand">
        <BrandMark />
        <span>Faustus</span>
      </div>

      {rail && (
        <span
          className="fs-nav__rail"
          aria-hidden="true"
          style={{ translate: `${rail.x}px ${rail.y}px`, inlineSize: rail.w, blockSize: rail.h }}
        />
      )}
      {indicator && (
        <span
          className="fs-nav__indicator"
          aria-hidden="true"
          style={{ translate: `${indicator.x}px ${indicator.y}px` }}
        />
      )}

      {DESTINATIONS.map((destination) => (
        <NavLink
          key={destination.path}
          to={destination.path}
          end={destination.path === '/'}
          className="fs-nav__item"
          data-testid={`nav-${destination.label.toLowerCase()}`}
        >
          <span className="fs-nav__node">
            <destination.icon size={14} aria-hidden="true" />
          </span>
          <span className="fs-nav__label">{t(destination.label)}</span>
        </NavLink>
      ))}

      <div className="fs-nav__tools" aria-label={t('Tools')}>
        <p className="fs-nav__tools-head">{t('Tools')}</p>
        {TOOLS.map((tool) =>
          tool.ready ? (
            <NavLink key={tool.path} to={tool.path} className="fs-nav__tool" data-testid={`tool-${tool.label.toLowerCase()}`}>
              <tool.icon size={13} aria-hidden="true" />
              <span>{t(tool.label)}</span>
            </NavLink>
          ) : (
            <a key={tool.path} href={toolHref(tool)} className="fs-nav__tool" data-legacy title={t('Opens in the previous interface')} data-testid={`tool-${tool.label.toLowerCase()}`}>
              <tool.icon size={13} aria-hidden="true" />
              <span>{t(tool.label)}</span>
            </a>
          ),
        )}
      </div>

      <div className="fs-nav__spacer" />

      <div className="fs-nav__foot">
        <NavLink to="/settings" className="fs-nav__tool fs-nav__settings" data-testid="nav-settings">
          <Settings2 size={13} aria-hidden="true" />
          <span>{t('Settings')}</span>
        </NavLink>
        <p className="fs-nav__hint">{t('Ctrl+K to search and navigate')}</p>
        <Button
          variant="ghost"
          size="sm"
          icon={LogOut}
          label={t('Previous interface')}
          onClick={() => {
            setStudioEnabled(false);
            window.location.href = '/?shell=legacy';
          }}
        />
      </div>
    </nav>
  );
}

/**
 * Re-mounts its child on every path change so the entrance animation runs.
 * Studio owns its own scrolling (transcript up, composer pinned), so main
 * is told which screen it holds and hands over the height.
 */
function RouteStage() {
  const { pathname, search } = useLocation();
  const editing = (pathname.startsWith('/library/edit') && /[?&](img|draft|new)=/.test(search)) || /^\/documents\/[^/]+/.test(pathname);
  const screen = pathname.startsWith('/studio') ? 'studio' : editing ? 'editor' : pathname.startsWith('/email') || pathname.startsWith('/compare') || (pathname.startsWith('/memory') && /[?&]t=provenance/.test(search)) || (pathname.startsWith('/agents') && /[?&]t=tournament/.test(search)) ? 'wide' : undefined;
  return (
    <main className="fs-main" id="fs-main" tabIndex={-1} data-screen={screen}>
      <div className="fs-main__inner">
        <RouteBody key={pathname} />
      </div>
    </main>
  );
}

function RouteBody() {
  return (
    <div className="fs-route">
      <Suspense fallback={<Skeleton label={t('Loading the screen')} count={4} height="56px" />}>
      <Routes>
        <Route path="/" element={<HomeScreen />} />
        {DESTINATIONS.filter((destination) => !destination.ready).map((destination) => (
          <Route
            key={destination.path}
            path={destination.path}
            element={<NotMigrated destination={destination} />}
          />
        ))}
        <Route path="/studio" element={<StudioScreen />} />
        <Route path="/projects" element={<ProjectsScreen />} />
        <Route path="/projects/:projectId" element={<ProjectScreen />} />
        <Route path="/library" element={<LibraryScreen />} />
        <Route path="/library/edit" element={<EditorScreen />} />
        <Route path="/documents/:id" element={<DocumentScreen />} />
        <Route path="/activity" element={<ActivityScreen />} />
        <Route path="/automations" element={<AutomationsScreen />} />
        <Route path="/notes" element={<NotesScreen />} />
        <Route path="/memory" element={<MemoryScreen />} />
        <Route path="/calendar" element={<CalendarScreen />} />
        <Route path="/email" element={<EmailScreen />} />
        <Route path="/research" element={<ResearchScreen />} />
        <Route path="/compare" element={<CompareScreen />} />
        <Route path="/group" element={<GroupScreen />} />
        <Route path="/settings" element={<SettingsScreen />} />
        <Route path="/agents" element={<AgentsScreen />} />
        <Route path="/skills" element={<SkillsScreen />} />
        <Route path="*" element={<NotMigrated />} />
      </Routes>
      </Suspense>
    </div>
  );
}

export function AppShell() {
  const setPaletteOpen = useShell((state) => state.setPaletteOpen);
  const paletteOpen = useShell((state) => state.paletteOpen);
  const [paletteLoaded, setPaletteLoaded] = useState(false);
  useEffect(() => {
    if (paletteOpen) setPaletteLoaded(true);
  }, [paletteOpen]);

  useEffect(() => {
    // Warm the Studio chunk once the shell has painted (it is the screen
    // almost every visit ends up in); a route hit before that just awaits
    // the same import.
    const idle = (window as Window & { requestIdleCallback?: (cb: () => void) => number }).requestIdleCallback;
    if (idle) idle(() => void loadStudio());
    else window.setTimeout(() => void loadStudio(), 300);
  }, []);

  // Re-key the whole tree when the language changes: static labels are read
  // at render, so a fresh mount is the honest way to refresh every one.
  const lang = useLang();
  const nav = useNavSize();
  const { theme } = useAppearance();
  useEffect(() => {
    void syncLangFromServer();
    void syncThemeFromServer();
    void syncAppearanceFromServer();
    return () => clearThemeAttribute();
  }, []);
  // The root and the overlay root carry the palette source; the effect
  // canvas lives inside the root. Both are recreated when the tree re-keys.
  useEffect(() => {
    applyTheme(getTheme());
  }, [lang, theme]);

  useEffect(() => {
    document.documentElement.setAttribute('data-studio-shell', 'on');
    ensureOverlayRoot();
    return () => {
      document.documentElement.removeAttribute('data-studio-shell');
      removeOverlayRoot();
    };
  }, []);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setPaletteOpen(true);
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [setPaletteOpen]);

  return (
    <BrowserRouter>
      <div className="fs-app fs-shell" data-testid="studio-shell" key={lang} data-nav={nav.mode} data-theme-source={theme.name !== 'studio' && theme.colors ? 'faustus' : undefined} data-resizing={nav.resizing || undefined} style={nav.style}>
        <a className="fs-skip-link" href="#fs-main">
          {t('Skip to content')}
        </a>
        <div className="fs-aurora" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
        <Rail />
        {/* Outside the nav on purpose: the nav clips its overflow, and the
            handle straddles its edge. */}
        <div
          className="fs-nav__resize"
          role="separator"
          aria-orientation="vertical"
          aria-label={t('Sidebar width')}
          aria-valuenow={nav.mode === 'rail' ? 0 : 1}
          title={t('Drag to resize; squeeze to collapse; double-click to reset')}
          tabIndex={0}
          {...nav.handle}
        />
        <RouteStage />
        {paletteLoaded && (
          <Suspense fallback={null}>
            <CommandPalette />
          </Suspense>
        )}
      </div>
    </BrowserRouter>
  );
}
