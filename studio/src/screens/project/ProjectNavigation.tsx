import { ChevronDown } from 'lucide-react';
import { useNavigate } from 'react-router';
import { ActivityDot, Menu } from '../../components';
import type { ComponentProps } from 'react';
import { t } from '../../i18n';
import { TOOLS } from '../../shell/routes';
import { toolHref } from '../../shell/toolPlacement';

export const PROJECT_SECTIONS = [
  { id: 'overview', label: 'Overview' }, { id: 'chats', label: 'Chats' },
  { id: 'tasks', label: 'Tasks' }, { id: 'knowledge', label: 'Knowledge' },
  { id: 'skills', label: 'Skills' }, { id: 'settings', label: 'Settings' },
] as const;
export type ProjectSection = typeof PROJECT_SECTIONS[number]['id'];

export function projectSection(raw: string | null): ProjectSection {
  const aliases: Record<string, ProjectSection> = { brief: 'overview', agora: 'overview', board: 'tasks', objetivos: 'tasks', requisitos: 'tasks', memoria: 'knowledge', contexto: 'knowledge', reglas: 'settings', repos: 'settings', ajustes: 'settings', actividad: 'overview' };
  return PROJECT_SECTIONS.some((s) => s.id === raw) ? raw as ProjectSection : aliases[raw || ''] || 'overview';
}

export function ProjectNavigation({ selected, onChange, projectId, liveChats, liveTone }: {
  selected: ProjectSection; onChange: (key: ProjectSection) => void;
  projectId: string; liveChats: number; liveTone: ComponentProps<typeof ActivityDot>['state'];
}) {
  const navigate = useNavigate();
  return <div className="fs-project-navigation"><div className="fs-tabs" role="tablist" aria-label={t('Project sections')}>
    {PROJECT_SECTIONS.map((entry, index) => <button key={entry.id} type="button" role="tab" aria-selected={selected === entry.id}
      tabIndex={selected === entry.id ? 0 : -1} className="fs-tab" data-testid={`project-tab-${entry.id}`} onClick={() => onChange(entry.id)}
      onKeyDown={(e) => {
        const next = e.key === 'ArrowRight' ? (index + 1) % 6 : e.key === 'ArrowLeft' ? (index + 5) % 6 : e.key === 'Home' ? 0 : e.key === 'End' ? 5 : -1;
        if (next < 0) return;
        e.preventDefault(); onChange(PROJECT_SECTIONS[next].id);
        e.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus();
      }}>
      {t(entry.label)}{entry.id === 'chats' && liveChats > 0 && <ActivityDot state={liveTone} />}
    </button>)}
    </div><Menu testId="project-tools-menu" trigger={<button type="button" className="fs-tab" data-testid="project-tools">
      {t('Project tools')}<ChevronDown size={13} aria-hidden="true" />
    </button>} items={[
      ...TOOLS.filter(tool => ['/alternatives', '/deltas', '/completion', '/state'].includes(tool.path)).map(tool => ({ label: t(tool.label), icon: tool.icon,
        onSelect: () => navigate(toolHref(tool.path, projectId)) })),
    ]} />
  </div>;
}
