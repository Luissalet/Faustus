import { AlertTriangle, RefreshCw } from 'lucide-react';
import { Button } from '../../components';
import type { BrainStatus } from '../../adapters/brain';
import { relativeTime } from '../../adapters/home';
import { t, tn } from '../../i18n';

/** The vault's own sync status and manual trigger — `vault.sync()` imports
 *  file edits first, then exports the store to files, then reindexes; this
 *  bar shows what the last run did and lets a person ask for another one
 *  without waiting for `brain_vault_sync_seconds` to come around. */
export function SyncBar({ status, syncing, onSync }: { status: BrainStatus | null; syncing: boolean; onSync: () => void }) {
  const report = status?.lastSync ?? null;
  return (
    <div className="fs-brain__syncbar" data-testid="brain-syncbar">
      <span className="fs-brain__syncbar-status">
        {report ? (
          <>
            {t('Last sync')} {relativeTime(report.at) || t('just now')}
            {' · '}
            {tn(report.imported, '{n} note imported', '{n} notes imported')}
            {' · '}
            {tn(report.exported, '{n} note exported', '{n} notes exported')}
            {report.guardTripped && (
              <span className="fs-brain__syncbar-warn">
                <AlertTriangle size={12} aria-hidden="true" /> {t('guard tripped — some deletions were skipped')}
              </span>
            )}
          </>
        ) : (
          t('Never synced yet')
        )}
      </span>
      <Button variant="ghost" size="sm" icon={RefreshCw} label={syncing ? t('Syncing…') : t('Sync now')} loading={syncing} onClick={onSync} testId="brain-sync-now" />
    </div>
  );
}
