import { ChevronDown } from 'lucide-react';
import { useNavigate } from 'react-router';
import { ActivityDot, Menu } from '../../components';
import type { ComponentProps } from 'react';
import { t } from '../../i18n';
import { TOOLS } from '../../shell/routes';
import { toolHref } from '../../shell/toolPlacement';

const PRIMARY = new Set(['brief', 'chats', 'agora', 'board', 'repos', 'ajustes']);

export function ProjectNavigation<K extends string>({ entries, selected, onChange, projectId, liveChats, liveTone }: {
  entries: readonly { id: K; label: string }[]; selected: K; onChange: (key: K) => void;
  projectId: string; liveChats: number; liveTone: ComponentProps<typeof ActivityDot>['state'];
}) {
  const navigate = useNavigate();
  const secondary = entries.filter(entry => !PRIMARY.has(entry.id));
  const selectedSecondary = secondary.find(entry => entry.id === selected);
  return <div className="fs-tabs" role="tablist" aria-label={t('Project sections')}>
    {entries.filter(entry => PRIMARY.has(entry.id)).map(entry => <button key={entry.id} type="button" role="tab" aria-selected={selected === entry.id}
      className="fs-tab" data-testid={`project-tab-${entry.id}`} onClick={() => onChange(entry.id)}>
      {t(entry.id === 'repos' ? 'Source control' : entry.label)}{entry.id === 'chats' && liveChats > 0 && <ActivityDot state={liveTone} />}
    </button>)}
    <Menu testId="project-more-menu" trigger={<button type="button" className="fs-tab" data-on={Boolean(selectedSecondary) || undefined} data-testid="project-more">
      {selectedSecondary ? t(selectedSecondary.label) : t('Project details')}<ChevronDown size={13} aria-hidden="true" />
    </button>} items={[
      ...secondary.map(entry => ({ label: t(entry.label), onSelect: () => onChange(entry.id) })), null,
      ...TOOLS.filter(tool => ['/alternatives', '/deltas', '/completion', '/state'].includes(tool.path)).map(tool => ({ label: t(tool.label), icon: tool.icon,
        onSelect: () => navigate(toolHref(tool.path, projectId)) })),
    ]} />
  </div>;
}
