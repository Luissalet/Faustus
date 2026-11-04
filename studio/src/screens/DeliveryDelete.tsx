import { Trash2 } from 'lucide-react';
import { useState } from 'react';
import { t } from '../i18n';

/**
 * Delete control for a Delivery record. Always asks first, says what goes and what
 * stays in the confirmation text, and reports the server's refusal if there is one.
 * The server only ever removes Faustus' own records and copies.
 */
export function DeleteItem({ label, confirm, onDelete, testId, disabled }: {
  label: string; confirm: string; onDelete: () => Promise<void>; testId?: string; disabled?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <button type="button" className="fs-dlv__del" aria-label={t('Delete {name}', { name: label })} title={t('Delete')}
      disabled={busy || disabled} data-testid={testId}
      onClick={async () => {
        if (!window.confirm(confirm)) return;
        setBusy(true);
        try {
          await onDelete();
        } catch (e) {
          window.alert((e as Error).message);
        } finally {
          setBusy(false);
        }
      }}>
      <Trash2 size={14} aria-hidden="true" />
    </button>
  );
}
