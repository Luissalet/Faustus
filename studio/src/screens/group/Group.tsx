import { useCallback } from 'react';
import { Link } from 'react-router';
import { useCoordinationParams } from '../../components/coordinationNavigation';
import { CoordinationSpaces } from '../../components/CoordinationSpaces';
import { StudioScreen } from '../Studio';
import { t } from '../../i18n';
import '../group.css';

/**
 * Group chat (the Agora's Conversation mode): several models talk with you and
 * with each other. The rooms of the selected agora on the left, the open room
 * as a full group chat on the right. A project shows the same view for its
 * own agora in its Agora tab.
 */
export function GroupScreen() {
  const [params, setParams] = useCoordinationParams('groups');
  const spaceId = params.get('space_id') || 'ecosystem';
  const selected = params.get('s') || '';
  const select = useCallback((id: string) => setParams({ space_id: spaceId, ...(id ? { s: id } : {}) }, { replace: true }), [setParams, spaceId]);
  return (
    <div className="fs-project-agora-shell" data-testid="group">
      <StudioScreen groupContext={{selected, onSelect: select, spaceId, navigation: <>
        <CoordinationSpaces value={spaceId} onChange={(id) => setParams({space_id: id}, {replace: true})} />
        <Link className="fs-chip" to={`/agora?space_id=${encodeURIComponent(spaceId)}&mode=council`}>{t('Deliberation')}</Link>
      </>}} />
    </div>
  );
}
