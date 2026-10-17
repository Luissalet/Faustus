import { useLocation, useNavigate, useSearchParams } from 'react-router';
import { t } from '../../i18n';

const TYPES = [
  { id: 'todo', label: 'All' },
  { id: 'imagen', label: 'Images' },
  { id: 'documento', label: 'Documents' },
  { id: 'chats', label: 'Chats' },
  { id: 'knowledge', label: 'Knowledge' },
  { id: 'research', label: 'Research' },
  { id: 'meetings', label: 'Meetings' },
  { id: 'historial', label: 'Imported' },
  { id: 'archivo', label: 'Archive' },
];

/** Shared navigation keeps the original Memory and Brain URLs inside Library. */
export function LibraryNavigation() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const knowledge = pathname === '/memory' || pathname === '/brain';
  const type = knowledge ? 'knowledge' : params.get('type') ?? 'todo';
  return (
    <div className="fs-tabs" role="tablist" aria-label={t('Filter by type')}>
      {TYPES.map(entry => (
        <button key={entry.id} type="button" role="tab" className="fs-tab"
          aria-selected={entry.id === type} data-testid={`library-type-${entry.id}`}
          onClick={() => {
            if (entry.id === type) return;
            if (entry.id === 'knowledge') { navigate('/memory'); return; }
            const next = knowledge ? new URLSearchParams() : new URLSearchParams(params);
            if (entry.id === 'todo') next.delete('type');
            else next.set('type', entry.id);
            navigate({ pathname: '/library', search: next.size ? `?${next}` : '' });
          }}>
          {t(entry.label)}
        </button>
      ))}
    </div>
  );
}
