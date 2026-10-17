import { Suspense } from 'react';
import { lazyChunk } from '../../shell/lazyChunk';
import { Skeleton } from '../../components';
import type { Project } from '../../adapters/projects';
import { ProjectObjectives } from './Objectives';
import { ProjectRequirements } from './Requirements';
import { ProjectBoard } from './ProjectAgora';
import { t } from '../../i18n';

const BoardPanel = lazyChunk(() => import('../board/BoardPanel').then((m) => ({ default: m.BoardPanel })));

export function ProjectTasks({ project, focus, say }: { project: Project; focus?: string; say: (message: string) => void }) {
  return <div className="fs-project-workspace" data-testid="project-tasks">
    <section className="fs-panel" aria-label={t('Tasks')}>
      <Suspense fallback={<Skeleton label={t('Loading the board')} count={3} height="80px" />}><BoardPanel projectId={project.id} /></Suspense>
    </section>
    <details className="fs-panel fs-project-workspace__section" open={focus === 'objectives' || undefined}>
      <summary>{t('Objectives')}</summary>
      <ProjectObjectives projectId={project.id} say={say} />
    </details>
    <details className="fs-panel fs-project-workspace__section" open={focus === 'requirements' || undefined}>
      <summary>{t('Requirements and acceptance')}</summary>
      <p className="fs-prose">{t('Define what must be delivered and how to verify it. Existing requirement decisions, revisions and evidence stay attached.')}</p>
      <ProjectRequirements projectId={project.id} say={say} />
    </details>
    <details className="fs-panel fs-project-workspace__section">
      <summary>{t('Agent coordination tasks')}</summary>
      <ProjectBoard project={project} />
    </details>
  </div>;
}
