import { Compass } from 'lucide-react';
import { useNavigate } from 'react-router';
import { EmptyState } from '../components';
import { t } from '../i18n';

/**
 * A route that is not one of ours.
 *
 * Usually a stale bookmark or a typed path. It says so plainly and offers
 * the way home rather than an empty frame — and never a raw 404, because
 * the server did answer: the router did not recognise the path.
 */
export function NotFound() {
  const navigate = useNavigate();
  return (
    <EmptyState
      icon={Compass}
      title={t('There is nothing at this address')}
      body={t('The link may be old or mistyped. Ctrl+K searches and navigates everywhere.')}
      primaryAction={{ label: t('Go home'), onClick: () => navigate('/') }}
    />
  );
}
