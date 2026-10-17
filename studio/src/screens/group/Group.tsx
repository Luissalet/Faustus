import { useCallback } from 'react';
import { useCoordinationParams } from '../../components/coordinationNavigation';
import { CoordinationSpaces } from '../../components/CoordinationSpaces';
import { AgoraRooms } from './AgoraRooms';
import '../group.css';

/**
 * Group chat (the Agora's Conversation mode): several models talk with you and
 * with each other. The rooms of the selected agora on the left, the open room
 * as a full group chat on the right. A project shows the same view for its
 * own agora in its Agora tab.
 */
export function GroupScreen({ embedded = false }: { embedded?: boolean } = {}) {
  const [params, setParams] = useCoordinationParams('groups');
  const spaceId = params.get('space_id') || 'ecosystem';
  const selected = params.get('s') || '';
  const select = useCallback((id: string) => setParams({ space_id: spaceId, ...(id ? { s: id } : {}) }, { replace: true }), [setParams, spaceId]);
  return (
    <div className="fs-grp-screen" data-testid="group">
      {!embedded && <CoordinationSpaces value={spaceId} onChange={(id) => setParams({ space_id: id }, { replace: true })} />}
      <AgoraRooms key={spaceId} spaceId={spaceId} selected={selected} onSelect={select} />
    </div>
  );
}
