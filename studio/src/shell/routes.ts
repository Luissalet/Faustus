import {
  Activity,
  Bot,
  Brain,
  CalendarDays,
  ChefHat,
  Cpu,
  Database,
  FolderKanban,
  Gauge,
  GitBranch,
  GitCompare,
  GitFork,
  Home,
  Library,
  ListChecks,
  Mail,
  Network,
  Plug,
  Sparkles,
  StickyNote,
  Zap,
  Workflow,
  type LucideIcon,
  Telescope,
  Columns3,
  Users,
  Scale,
  Wand2,
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
  // CONTRATO_CONECTORES Lote F3: the Hoard presets (Jobhunter, Writer…) and
  // every other MCP server, unified — /api/app-connectors, not a second store.
  { path: '/connectors', label: 'Connectors', icon: Plug },
  // The control center (src/process_center.py): what is running because of
  // Faustus — ports, background jobs, launched apps — and a Stop for each.
  { path: '/processes', label: 'Processes', icon: Cpu },
  // WP05: shown only once `useCreatorAvailable()` (adapters/creator.ts)
  // confirms `creator_enabled` is on -- the Rail filters this entry out by
  // path, same list either way so the palette and SERVER_ROUTES agree.
  { path: '/creator', label: 'Creator', icon: Wand2 },
  { path: '/notes', label: 'Notes', icon: StickyNote },
  { path: '/source-control', label: 'Source control', icon: GitBranch },
  { path: '/calendar', label: 'Calendar', icon: CalendarDays },
  { path: '/email', label: 'Mail', icon: Mail },
  { path: '/memory', label: 'Memory', icon: Brain },
  { path: '/agents', label: 'Agents', icon: Bot },
  { path: '/skills', label: 'Skills', icon: Zap },
  { path: '/research', label: 'Research', icon: Telescope },
  { path: '/compare', label: 'Compare', icon: Columns3 },
  { path: '/group', label: 'Group chat', icon: Users },
  { path: '/council', label: 'Council', icon: Scale },
  { path: '/state', label: 'State Mirror', icon: Database },
  { path: '/deltas', label: 'Deltas', icon: GitCompare },
  { path: '/alternatives', label: 'Alternatives', icon: GitFork },
  { path: '/completion', label: 'Completion', icon: ListChecks },
  { path: '/cookbook', label: 'Cookbook', icon: ChefHat },
  { path: '/context', label: 'Context', icon: Gauge },
  { path: '/workflows', label: 'Workflows', icon: Network },
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
  '/source-control',
  '/connectors',
  '/processes',
  '/memory',
  '/calendar',
  '/email',
  '/settings',
  '/agents',
  '/skills',
  '/research',
  '/compare',
  '/group',
  '/council',
  '/state',
  '/deltas',
  '/alternatives',
  '/completion',
  '/cookbook',
  '/context',
  '/workflows',
  '/creator',
  // The paths the interface this one replaced owned. Still served, still in
  // bookmarks; the router redirects each to the screen that took over.
  '/gallery',
  '/tasks',
  '/brain',
];
