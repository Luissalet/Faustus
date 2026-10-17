import { ArrowLeft, ChevronDown } from 'lucide-react';
import { Link, useLocation, useNavigate, useParams } from 'react-router';
import { Menu } from '../components';
import { useCreatorAvailable } from '../adapters/creator';
import { t } from '../i18n';
import { CONTEXT_LABELS, TOOL_PLACEMENT, toolContext, toolHref, toolsIn, type ToolContext } from './toolPlacement';

const PARENTS: Record<ToolContext, { path: string; label: string }> = {
  chat: { path: '/studio', label: 'Studio' }, project: { path: '/projects', label: 'Projects' },
  library: { path: '/library', label: 'Library' }, automations: { path: '/automations', label: 'Automations' },
  activity: { path: '/activity', label: 'Activity' }, connections: { path: '/connectors', label: 'Hoards' },
  personal: { path: '/studio', label: 'Studio' },
};

/** Secondary work stays beside its parent instead of occupying the rail. */
export function ContextualTools({ context: explicitContext, projectId: explicitProject, sessionId }: {
  context?: ToolContext; projectId?: string | null; sessionId?: string | null;
}) {
  const { pathname, search } = useLocation();
  const params = useParams();
  const navigate = useNavigate();
  const creatorAvailable = useCreatorAvailable();
  const context = explicitContext ?? toolContext(pathname);
  const projectId = explicitProject ?? params.projectId ?? new URLSearchParams(search).get('project');
  const tools = toolsIn(context, creatorAvailable === true);
  const parent = context === 'project' && projectId ? { path: `/projects/${encodeURIComponent(projectId)}`, label: 'Project' }
    : pathname === '/brain' || pathname === '/memory' ? { path: '/library?type=knowledge', label: 'Knowledge' } : PARENTS[context];
  const isChild = Boolean(TOOL_PLACEMENT[pathname]) && pathname !== parent.path;
  return (
    <div className="fs-context-tools" data-testid={`context-tools-${context}`}>
      {isChild && <Link to={parent.path} className="fs-link"><ArrowLeft size={13} aria-hidden="true" /> {t(parent.label)}</Link>}
      <Menu testId={`context-menu-${context}`} align="end"
        trigger={<button type="button" className="fs-context-tools__trigger">{t(CONTEXT_LABELS[context])}<ChevronDown size={13} aria-hidden="true" /></button>}
        items={tools.map(tool => ({ label: t(tool.path === '/research' ? 'Research reports' : tool.path === '/brain' ? 'Knowledge' : tool.label), icon: tool.icon,
          onSelect: () => navigate(toolHref(tool.path, projectId, sessionId)) }))} />
    </div>
  );
}
