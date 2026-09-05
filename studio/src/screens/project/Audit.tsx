import { Trash2 } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router';
import { Button, Skeleton } from '../../components';
import { clearProjectAudit, projectAudit, type AuditEntry } from '../../adapters/projects';
import { locale, t } from '../../i18n';

/** Every turn that changed files in this project, with the chat it came from. */
export function ProjectAudit({ projectId, say }: { projectId: string; say: (m: string) => void }) {
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [busy, setBusy] = useState(false);
  const reload = useCallback(() => {
    projectAudit(projectId)
      .then(setEntries)
      .catch((e: Error) => {
        say(e.message);
        setEntries([]);
      });
  }, [projectId, say]);
  useEffect(reload, [reload]);

  if (entries === null) return <Skeleton label={t('Loading the activity')} count={3} height="52px" />;

  return (
    <div className="fs-pj__audit">
      <div className="fs-pj__obj-head">
        <p className="fs-prose">{t('Every turn that changed files in this project, with the chat and the exact answer it came from.')}</p>
        {entries.length > 0 && (
          <Button
            variant="ghost"
            size="sm"
            icon={Trash2}
            label={t('Clear')}
            loading={busy}
            onClick={() => {
              setBusy(true);
              void clearProjectAudit(projectId)
                .then(reload, (e: Error) => say(e.message))
                .finally(() => setBusy(false));
            }}
          />
        )}
      </div>
      {entries.length === 0 ? (
        <p className="fs-pj__muted">{t('No agent turn has changed files in this project yet.')}</p>
      ) : (
        <ul className="fs-pj__audit-list">
          {entries.slice(0, 200).map((e, i) => (
            <li key={`${e.ts}-${i}`} className="fs-pj__audit-row">
              <div className="fs-pj__audit-head">
                {e.sessionId ? (
                  <Link to={`/studio?s=${encodeURIComponent(e.sessionId)}${e.messageId ? `&m=${encodeURIComponent(e.messageId)}` : ''}`} className="fs-pj__audit-link">
                    {t('Open the chat')}
                  </Link>
                ) : (
                  <span />
                )}
                <time>{e.ts ? new Date(e.ts * 1000).toLocaleString(locale(), { dateStyle: 'medium', timeStyle: 'short' }) : ''}</time>
              </div>
              {e.request && <p className="fs-pj__audit-request">{e.request}</p>}
              {e.files.length > 0 && (
                <div className="fs-pj__audit-files">
                  {e.files.slice(0, 6).map((f) => (
                    <code key={f}>{f}</code>
                  ))}
                  {e.files.length > 6 && <small>+{e.files.length - 6}</small>}
                </div>
              )}
              <div className="fs-pj__audit-badges">
                {e.tests === 'pass' && <span data-tone="ok">{t('tests ✓')}</span>}
                {e.tests === 'fail' && <span data-tone="bad">{t('tests ✗')}</span>}
                {e.tests === 'inconclusive' && <span>{t('tests ?')}</span>}
                {e.review === 'ok' && <span data-tone="ok">{t('review ✓')}</span>}
                {e.review === 'issues' && <span data-tone="warn">{t('review ⚠')}</span>}
                {e.stopReason === 'complete_unverified' && <span data-tone="bad">{t('unverified')}</span>}
                {e.checkpoint && <span title={t('Restorable: checkpoint {sha}', { sha: e.checkpoint })}>⟲ {e.checkpoint.slice(0, 7)}</span>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
