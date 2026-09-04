import { LogOut } from 'lucide-react';
import { useEffect } from 'react';
import { BrowserRouter, NavLink, Route, Routes } from 'react-router';
import { Button } from '../components';
import { HomeScreen } from '../screens/Home';
import { NotMigrated } from '../screens/NotMigrated';
import { ProjectScreen } from '../screens/Project';
import { ProjectsScreen } from '../screens/Projects';
import { CommandPalette } from './CommandPalette';
import { DESTINATIONS } from './routes';
import { setStudioEnabled } from './flag';
import { ensureOverlayRoot, removeOverlayRoot } from './overlayRoot';
import { useShell } from './store';

function Nav() {
  return (
    <nav className="fs-nav" aria-label="Navegación principal">
      <div className="fs-nav__brand">
        <span aria-hidden="true">◆</span>
        <span>Faustus</span>
      </div>

      {DESTINATIONS.map((destination) => (
        <NavLink
          key={destination.path}
          to={destination.path}
          end={destination.path === '/'}
          className="fs-nav__item"
          data-testid={`nav-${destination.label.toLowerCase()}`}
        >
          <destination.icon size={17} aria-hidden="true" />
          <span>{destination.label}</span>
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
            // The pilot must always have a way back that costs one click.
            setStudioEnabled(false);
            window.location.href = '/?shell=legacy';
          }}
        />
      </div>
    </nav>
  );
}

export function AppShell() {
  const setPaletteOpen = useShell((state) => state.setPaletteOpen);

  useEffect(() => {
    // Marks the document so the legacy tree stops painting. Removed on
    // unmount, so turning the pilot off restores the old interface without
    // a reload having to do it.
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
        <Nav />
        <main className="fs-main" id="fs-main" tabIndex={-1}>
          <div className="fs-main__inner">
            <Routes>
              <Route path="/" element={<HomeScreen />} />
              {DESTINATIONS.filter((destination) => !destination.ready).map((destination) => (
                <Route
                  key={destination.path}
                  path={destination.path}
                  element={<NotMigrated destination={destination} />}
                />
              ))}
              <Route path="/projects" element={<ProjectsScreen />} />
              <Route path="/projects/:projectId" element={<ProjectScreen />} />
              <Route path="*" element={<NotMigrated />} />
            </Routes>
          </div>
        </main>
        <CommandPalette />
      </div>
    </BrowserRouter>
  );
}
