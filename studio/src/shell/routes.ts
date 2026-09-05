import {
  Activity,
  Bot,
  Brain,
  CalendarDays,
  ChefHat,
  FolderKanban,
  Home,
  Library,
  Mail,
  Sparkles,
  StickyNote,
  Zap,
  Workflow,
  type LucideIcon,
  Telescope,
  Columns3,
  Users,
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
}

export const DESTINATIONS: Destination[] = [
  { path: '/', label: 'Home', icon: Home },
  { path: '/studio', label: 'Studio', icon: Sparkles },
  { path: '/projects', label: 'Projects', icon: FolderKanban },
  { path: '/library', label: 'Library', icon: Library },
  { path: '/automations', label: 'Automations', icon: Workflow },
  { path: '/activity', label: 'Activity', icon: Activity },
];

/**
 * The tools (UI-020 §"everything technical lives inside a destination"):
 * not on the rail's line, but one click away in the sidebar's second group
 * and in the palette. Every one of them is a Studio route.
 */
export interface Tool {
  path: string;
  label: string;
  icon: LucideIcon;
}

export const TOOLS: Tool[] = [
  { path: '/notes', label: 'Notes', icon: StickyNote },
  { path: '/calendar', label: 'Calendar', icon: CalendarDays },
  { path: '/email', label: 'Mail', icon: Mail },
  { path: '/memory', label: 'Memory', icon: Brain },
  { path: '/agents', label: 'Agents', icon: Bot },
  { path: '/skills', label: 'Skills', icon: Zap },
  { path: '/research', label: 'Research', icon: Telescope },
  { path: '/compare', label: 'Compare', icon: Columns3 },
  { path: '/group', label: 'Group chat', icon: Users },
  { path: '/cookbook', label: 'Cookbook', icon: ChefHat },
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
  '/library/edit',
  '/documents/{doc_id}',
  '/automations',
  '/activity',
  '/notes',
  '/memory',
  '/calendar',
  '/email',
  '/settings',
  '/agents',
  '/skills',
  '/research',
  '/compare',
  '/group',
  '/cookbook',
  // The paths the interface this one replaced owned. Still served, still in
  // bookmarks; the router redirects each to the screen that took over.
  '/gallery',
  '/tasks',
  '/brain',
];
