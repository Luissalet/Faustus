import {
  Activity,
  Brain,
  CalendarDays,
  ChefHat,
  FolderKanban,
  Home,
  Library,
  Mail,
  Sparkles,
  StickyNote,
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
  { path: '/studio', label: 'Studio', icon: Sparkles, ready: true },
  { path: '/projects', label: 'Proyectos', icon: FolderKanban, ready: true },
  { path: '/library', label: 'Biblioteca', icon: Library, ready: true },
  { path: '/automations', label: 'Automatizaciones', icon: Workflow, ready: true },
  { path: '/activity', label: 'Actividad', icon: Activity, ready: true },
];

/**
 * The tools (UI-020 §"everything technical lives inside a destination"):
 * not on the rail's line, but one click away in the sidebar's second group
 * and in the palette. A migrated tool is a Studio route; the rest open the
 * previous interface at their deep link (`/notes?shell=legacy` opens that
 * window) until PARIDAD_FUNCIONAL marks them Migrado.
 */
export interface Tool {
  path: string;
  label: string;
  icon: LucideIcon;
  ready: boolean;
}

export const TOOLS: Tool[] = [
  { path: '/notes', label: 'Notas', icon: StickyNote, ready: true },
  { path: '/calendar', label: 'Calendario', icon: CalendarDays, ready: false },
  { path: '/email', label: 'Correo', icon: Mail, ready: false },
  { path: '/memory', label: 'Memoria', icon: Brain, ready: true },
  { path: '/cookbook', label: 'Cookbook', icon: ChefHat, ready: false },
];

/** Where a tool opens today: its Studio route, or the previous interface. */
export function toolHref(tool: Tool): string {
  return tool.ready ? tool.path : `${tool.path}?shell=legacy`;
}

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
  '/notes',
  '/memory',
];
