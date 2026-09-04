import {
  Activity,
  FolderKanban,
  Home,
  Library,
  Sparkles,
  Workflow,
  type LucideIcon,
} from 'lucide-react';

/**
 * The six destinations (UI-020).
 *
 * The sidebar is navigation of intentions, not a directory of subsystems.
 * The old one had eighteen entries and a new user could not build a mental
 * model from it; everything technical now lives inside a destination or in
 * settings.
 *
 * These are real routes with real URLs: they can be bookmarked, opened in a
 * new tab and reached with the browser's back button, which no modal ever
 * could.
 */
export interface Destination {
  path: string;
  label: string;
  icon: LucideIcon;
  /** Migrated screens render themselves; the rest say so honestly. */
  ready: boolean;
}

export const DESTINATIONS: Destination[] = [
  { path: '/', label: 'Inicio', icon: Home, ready: true },
  { path: '/studio', label: 'Studio', icon: Sparkles, ready: false },
  { path: '/projects', label: 'Proyectos', icon: FolderKanban, ready: false },
  { path: '/library', label: 'Biblioteca', icon: Library, ready: false },
  { path: '/automations', label: 'Automatizaciones', icon: Workflow, ready: false },
  { path: '/activity', label: 'Actividad', icon: Activity, ready: false },
];

/**
 * Kept in step with the whitelist in app.py. A route that the server does
 * not serve is a 404 on reload, which is exactly the bug deep links exist
 * to avoid.
 */
export const SERVER_ROUTES = [
  '/',
  '/studio',
  '/projects',
  '/projects/{project_id}',
  '/library',
  '/automations',
  '/activity',
];
