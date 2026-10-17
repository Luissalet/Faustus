import { FileJson2, MoreHorizontal, Telescope, X } from 'lucide-react';
import { Suspense } from 'react';
import { useNavigate } from 'react-router';
import { IconButton, Menu, Skeleton } from '../../components';
import { t } from '../../i18n';
import { lazyChunk, RouteErrorBoundary } from '../../shell/lazyChunk';
import { toolHref, toolsIn } from '../../shell/toolPlacement';

const Extraction = lazyChunk(() => import('../extraction/Extraction').then(module => ({ default: module.ExtractionScreen })));
const Compare = lazyChunk(() => import('../compare/Compare').then(module => ({ default: module.CompareScreen })));

export type ChatWorkspace = 'extract' | 'compare';

export function chatWorkspace(value: string | null): ChatWorkspace | null {
  return value === 'extract' || value === 'compare' ? value : null;
}

export function ChatToolsMenu({ projectId, sessionId, onResearch, onWorkspace }: {
  projectId?: string | null; sessionId?: string | null;
  onResearch: () => void; onWorkspace: (tool: ChatWorkspace) => void;
}) {
  const navigate = useNavigate();
  return <Menu align="end" testId="chat-tools-menu"
    trigger={<IconButton icon={MoreHorizontal} label={t('Chat actions')} size="sm" testId="chat-tools" />}
    items={[
      { label: t('Research'), icon: Telescope, onSelect: onResearch },
      { label: t('Extraction'), icon: FileJson2, onSelect: () => onWorkspace('extract') },
      ...toolsIn('chat').map(tool => ({ label: t(tool.label), icon: tool.icon,
        onSelect: () => tool.path === '/compare' ? onWorkspace('compare') : navigate(toolHref(tool.path, projectId, sessionId)) })),
    ]} />;
}

/** Existing document workspaces beside the same live conversation. */
export function ChatToolWorkspace({ tool, onClose, onUse }: {
  tool: ChatWorkspace; onClose: () => void; onUse: (text: string) => void;
}) {
  return <aside className="fs-panel fs-chat-workspace" aria-label={t(tool === 'extract' ? 'Extraction' : 'Compare')} data-testid={`chat-workspace-${tool}`}>
    <header className="fs-panel__head"><strong>{t(tool === 'extract' ? 'Extraction' : 'Compare')}</strong><IconButton icon={X} label={t('Close')} onClick={onClose} size="sm" /></header>
    <div className="fs-chat-workspace__body"><RouteErrorBoundary><Suspense fallback={<Skeleton count={3} label={t('Loading the screen')} />}>
      {tool === 'extract' ? <Extraction onUse={onUse} /> : <Compare />}
    </Suspense></RouteErrorBoundary></div>
  </aside>;
}
