import { LogOut } from 'lucide-react';
import { lazy, Suspense, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { BrowserRouter, NavLink, Route, Routes, useLocation } from 'react-router';
import { Button, Skeleton } from '../components';
import { HomeScreen } from '../screens/Home';
import { NotMigrated } from '../screens/NotMigrated';
import { StudioScreen } from '../screens/Studio';

import { BrandMark } from './BrandMark';
import { CommandPalette } from './CommandPalette';
import { DESTINATIONS } from './routes';
import { setStudioEnabled } from './flag';
import { ensureOverlayRoot, removeOverlayRoot } from './overlayRoot';
import { useShell } from './store';

/* Inicio and Studio are the app; the rest arrive as route chunks the
   first time they are opened, so the eager bundle stays inside the
   350 KB budget (DECISIONES_UI.md) as screens keep landing. */
const ActivityScreen = lazy(() => import('../screens/Activity').then((m) => ({ default: m.ActivityScreen })));
const AutomationsScreen = lazy(() => import('../screens/Automations').then((m) => ({ default: m.AutomationsScreen })));
const LibraryScreen = lazy(() => import('../screens/Library').then((m) => ({ default: m.LibraryScreen })));
const ProjectScreen = lazy(() => import('../screens/Project').then((m) => ({ default: m.ProjectScreen })));
const ProjectsScreen = lazy(() => import('../screens/Projects').then((m) => ({ default: m.ProjectsScreen })));

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
    <nav className="fs-nav" aria-label="Navegación principal" ref={navRef}>
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
          <span className="fs-nav__label">{destination.label}</span>
        </NavLink>
      ))}

      <div className="fs-nav__spacer" />

      <div className="fs-nav__foot">
        <p className="fs-nav__hint">Ctrl+K para buscar y navegar</p>
        <Button
          variant="ghost"
          size="sm"
          icon={LogOut}
          label="Interfaz anterior"
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
  const { pathname } = useLocation();
  const screen = pathname.startsWith('/studio') ? 'studio' : undefined;
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
      <Suspense fallback={<Skeleton label="Cargando la pantalla" count={4} height="56px" />}>
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
        <Route path="/activity" element={<ActivityScreen />} />
        <Route path="/automations" element={<AutomationsScreen />} />
        <Route path="*" element={<NotMigrated />} />
      </Routes>
      </Suspense>
    </div>
  );
}

export function AppShell() {
  const setPaletteOpen = useShell((state) => state.setPaletteOpen);

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
      <div className="fs-app fs-shell" data-testid="studio-shell">
        <a className="fs-skip-link" href="#fs-main">
          Saltar al contenido
        </a>
        <div className="fs-aurora" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
        <Rail />
        <RouteStage />
        <CommandPalette />
      </div>
    </BrowserRouter>
  );
}
