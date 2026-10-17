import { FolderKanban } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { listProjects, type Project } from '../../adapters/projects';
import { EmptyState, Skeleton } from '../../components';
import { t } from '../../i18n';

/** Older global Git bookmarks now begin by choosing the working project. */
export function ProjectPicker() {
  const navigate = useNavigate();
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let live = true;
    setFailed(false);
    void listProjects().then(rows => { if (live) setProjects(rows); }).catch(() => { if (live) setFailed(true); });
    return () => { live = false; };
  }, [revision]);
  return <div className="fs-screen" data-testid="source-control-project-picker">
    <header className="fs-screen__head"><h1 className="fs-screen__title">{t('Source control')}</h1></header>
    <p className="fs-prose">{t('Choose a project to work with its repositories.')}</p>
    {failed ? <EmptyState title={t('Could not load projects.')} body={t('Try again')} primaryAction={{ label: t('Try again'), onClick: () => setRevision(value => value + 1) }} />
      : projects === null ? <Skeleton count={3} label={t('Loading projects')} />
      : projects.length === 0 ? <EmptyState title={t('No projects yet')} body={t('Choose a project to work with its repositories.')} primaryAction={{ label: t('Projects'), onClick: () => navigate('/projects') }} />
      : <ul className="fs-project-picker">{projects.map(project => <li key={project.id}><Link to={`/projects/${encodeURIComponent(project.id)}?tab=repos`}><FolderKanban size={16} aria-hidden="true" /><span>{project.name}</span></Link></li>)}</ul>}
    <details className="fs-project-picker__overview"><summary>{t('Repository overview')}</summary><p><Link className="fs-link" to="/source-control?scope=all">{t('All repositories')}</Link></p></details>
  </div>;
}
