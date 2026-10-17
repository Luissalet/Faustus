import { TOOLS, type Tool } from './routes';

export type ToolContext = 'chat' | 'project' | 'library' | 'automations' | 'activity' | 'connections' | 'personal';

/** A capability's working context, also used by search and contextual menus. */
export const TOOL_PLACEMENT: Record<string, { context: ToolContext; hint: string }> = {
  '/connectors': { context: 'connections', hint: 'Hoards and connected tools' },
  '/whatsapp': { context: 'personal', hint: 'Messages' },
  '/processes': { context: 'activity', hint: 'Running apps and background work' },
  '/creator': { context: 'library', hint: 'Create an artifact' },
  '/notes': { context: 'personal', hint: 'Notes and tasks' },
  '/brain': { context: 'library', hint: 'Notes, links and knowledge' },
  '/source-control': { context: 'project', hint: 'Repositories in a project' },
  '/calendar': { context: 'personal', hint: 'Events and reminders' },
  '/email': { context: 'personal', hint: 'Messages' },
  '/memory': { context: 'chat', hint: 'What Faustus remembers' },
  '/agents': { context: 'automations', hint: 'Workers and specialists' },
  '/skills': { context: 'chat', hint: 'Reusable instructions' },
  '/extraction': { context: 'library', hint: 'Extract structured data from a document' },
  '/visual-index': { context: 'library', hint: 'Search images and document pages' },
  '/research': { context: 'library', hint: 'Saved research reports' },
  '/compare': { context: 'chat', hint: 'Compare model answers' },
  '/agora': { context: 'project', hint: 'Conversations and coordination' },
  '/state': { context: 'activity', hint: 'Observed state and evidence' },
  '/deltas': { context: 'activity', hint: 'Changes made by a task' },
  '/alternatives': { context: 'project', hint: 'Compare approaches to project work' },
  '/completion': { context: 'activity', hint: 'What finished and what remains' },
  '/cookbook': { context: 'connections', hint: 'Install and serve local models' },
  '/context': { context: 'chat', hint: 'Conversation context and token use' },
  '/workflows': { context: 'automations', hint: 'Reusable workflows' },
};

export const CONTEXT_LABELS: Record<ToolContext, string> = {
  chat: 'Chat actions', project: 'Project tools', library: 'Library tools',
  automations: 'Automation tools', activity: 'Run details', connections: 'Connections and models', personal: 'Personal tools',
};

export function toolContext(pathname: string): ToolContext {
  if (pathname === '/studio') return 'chat';
  if (pathname.startsWith('/projects')) return 'project';
  if (pathname.startsWith('/library') || pathname.startsWith('/documents')) return 'library';
  if (pathname === '/automations') return 'automations';
  if (pathname === '/activity') return 'activity';
  if (pathname === '/settings') return 'connections';
  return TOOL_PLACEMENT[pathname]?.context ?? 'personal';
}

export function toolsIn(context: ToolContext, creatorAvailable = false): Tool[] {
  return TOOLS.filter(tool => TOOL_PLACEMENT[tool.path]?.context === context && (tool.path !== '/creator' || creatorAvailable));
}

/** Never turn a project action into an unscoped global view. */
export function toolHref(path: string, projectId?: string | null, sessionId?: string | null): string {
  if (path === '/source-control') return projectId ? `/projects/${encodeURIComponent(projectId)}?tab=repos` : '/source-control';
  const params = new URLSearchParams();
  if (projectId && ['/alternatives', '/agora', '/context', '/completion', '/deltas'].includes(path)) params.set('project', projectId);
  return params.size ? `${path}?${params}` : path;
}
