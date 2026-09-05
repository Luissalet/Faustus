import { Construction } from 'lucide-react';
import { EmptyState } from '../components';
import type { Destination } from '../shell/routes';
import { t } from '../i18n';

/**
 * The honest placeholder.
 *
 * A destination that exists in the navigation but has not been migrated yet
 * says so and offers the way back, instead of pretending to be a screen.
 * Every one of these disappears as its ticket lands — they are a countdown,
 * not furniture.
 */
export function NotMigrated({ destination }: { destination?: Destination }) {
  const name = destination ? t(destination.label) : t('This screen');

  return (
    <EmptyState
      icon={Construction}
      title={t('{name} still lives in the previous interface', { name })}
      body={t('The new shell already has navigation, routes and keyboard. This screen is migrated in its own ticket, and until then it keeps working exactly as always in the previous interface.')}
      primaryAction={{
        label: t('Open the previous interface'),
        onClick: () => {
          window.location.href = '/?shell=legacy';
        },
      }}
    />
  );
}
