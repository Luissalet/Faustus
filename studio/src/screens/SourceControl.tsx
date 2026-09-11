import { GitBranch } from 'lucide-react';
import { useSearchParams } from 'react-router';
import { SourceControlPanel } from './source-control/SourceControlPanel';
import './source-control.css';
import { t } from '../i18n';

/**
 * OBJ-4 — Source control: every git repo under the owner's linked project
 * folders (nested ones included), each with its branch, ahead/behind and
 * pending changes; select one to see CHANGES (stage/unstage + the commit
 * box) and the commit GRAPH, and pick a file or a commit to see its diff.
 * The VS Code Source Control view this mirrors, over `/api/git/*`
 * (`adapters/git.ts`, contract in the OBJ-4 lots' shared brief).
 *
 * Lote 86 (`CONTRATO_GIT_4.md`): the state and every handler moved into
 * `SourceControlPanel` — reused as-is by a project's own "Repositories"
 * section and by the chat's compact side panel — so this screen is now
 * just that panel mounted full width, plus the page chrome (title,
 * `?project=` scope read straight off the URL the way it always has).
 */
export function SourceControlScreen() {
  const [params] = useSearchParams();
  const projectId = params.get('project') ?? undefined;

  return (
    <div className="fs-screen fs-sc" data-testid="source-control">
      <header className="fs-screen__head">
        <div className="fs-sc__title">
          <h1 className="fs-screen__title">
            <GitBranch size={20} aria-hidden="true" /> {t('Source control')}
          </h1>
          <p className="fs-screen__sub">{t('Every git repository under your linked project folders.')}</p>
        </div>
      </header>
      <SourceControlPanel projectId={projectId} />
    </div>
  );
}
