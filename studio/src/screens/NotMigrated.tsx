import { Construction } from 'lucide-react';
import { EmptyState } from '../components';
import type { Destination } from '../shell/routes';

/**
 * The honest placeholder.
 *
 * A destination that exists in the navigation but has not been migrated yet
 * says so and offers the way back, instead of pretending to be a screen.
 * Every one of these disappears as its ticket lands — they are a countdown,
 * not furniture.
 */
export function NotMigrated({ destination }: { destination?: Destination }) {
  const name = destination?.label ?? 'Esta pantalla';

  return (
    <EmptyState
      icon={Construction}
      title={`${name} todavía vive en la interfaz anterior`}
      body="El shell nuevo ya tiene navegación, rutas y teclado. Esta pantalla se migra en su propio ticket, y hasta entonces sigue funcionando exactamente igual que siempre en la interfaz de siempre."
      primaryAction={{
        label: 'Abrir la interfaz anterior',
        onClick: () => {
          window.location.href = '/?shell=legacy';
        },
      }}
    />
  );
}
