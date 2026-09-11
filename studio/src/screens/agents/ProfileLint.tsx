import { AlertTriangle, Info, OctagonAlert, RefreshCw, Search } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, Dialog, EmptyState, Skeleton } from '../../components';
import { lintCatalog, type LintFinding, type LintSeverity } from '../../adapters/topology';
import { t, tn } from '../../i18n';

/**
 * B2 (OBJ-8): the Studio half of `GET /api/agent-profiles/lint`
 * (`docs/api/topology.md`) — every legal-but-suspicious finding across the
 * whole agent-profile catalogue, opened from Defs.tsx's "Lint" button.
 *
 * Deliberately catalogue-wide rather than one profile at a time: `Defs`
 * already shows every definition on one screen, and a finding's `subject`
 * (e.g. `budget:default`) already says which profile it is about — a
 * second, per-profile lookup (`lintProfile`, also in adapters/topology.ts)
 * would only fragment the same list the catalogue endpoint already returns
 * in one call.
 */

const SEVERITY_ORDER: LintSeverity[] = ['error', 'warn', 'info'];

const SEVERITY_ICON: Record<LintSeverity, typeof OctagonAlert> = {
  error: OctagonAlert,
  warn: AlertTriangle,
  info: Info,
};

const SEVERITY_LABEL: Record<LintSeverity, string> = {
  error: 'Error',
  warn: 'Warning',
  info: 'Info',
};

function sortFindings(findings: LintFinding[]): LintFinding[] {
  const rank = Object.fromEntries(SEVERITY_ORDER.map((s, i) => [s, i]));
  return findings.slice().sort((a, b) => rank[a.severity] - rank[b.severity] || a.subject.localeCompare(b.subject) || a.code.localeCompare(b.code));
}

export function ProfileLint({ onClose }: { onClose: () => void }) {
  const [findings, setFindings] = useState<LintFinding[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [severity, setSeverity] = useState<LintSeverity | 'all'>('all');
  const [query, setQuery] = useState('');

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      setFindings(sortFindings(await lintCatalog()));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const counts = useMemo(() => {
    const c: Record<LintSeverity, number> = { error: 0, warn: 0, info: 0 };
    for (const f of findings ?? []) c[f.severity]++;
    return c;
  }, [findings]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (findings ?? []).filter((f) => {
      if (severity !== 'all' && f.severity !== severity) return false;
      if (!needle) return true;
      return f.code.toLowerCase().includes(needle) || f.subject.toLowerCase().includes(needle) || f.message.toLowerCase().includes(needle);
    });
  }, [findings, severity, query]);

  return (
    <Dialog open onOpenChange={(next) => { if (!next) onClose(); }} title={t('Profile and workflow lint')} testId="profile-lint">
      <div className="fs-lint">
        <p className="fs-prose">
          {t('Legal-but-suspicious configurations — distinct from the schema errors the catalogue already refuses at registration. Nothing here blocks a run by itself.')}
        </p>
        <div className="fs-lint__toolbar">
          <label className="fs-agents__search">
            <Search size={13} aria-hidden="true" />
            <input type="search" placeholder={t('Search findings…')} value={query} onChange={(e) => setQuery(e.target.value)} aria-label={t('Search lint findings')} />
          </label>
          <div className="fs-lint__chips" role="group" aria-label={t('Severity')}>
            {(['all', ...SEVERITY_ORDER] as const).map((s) => (
              <button
                key={s}
                type="button"
                className="fs-chip"
                aria-pressed={severity === s}
                data-on={severity === s || undefined}
                data-severity={s === 'all' ? undefined : s}
                onClick={() => setSeverity(s)}
                data-testid={`lint-filter-${s}`}
              >
                {s === 'all' ? t('All') : t(SEVERITY_LABEL[s])} <span className="fs-lint__chip-n">{s === 'all' ? (findings?.length ?? 0) : counts[s]}</span>
              </button>
            ))}
          </div>
          <span className="fs-lint__spacer" />
          <Button variant="ghost" size="sm" icon={RefreshCw} label={t('Refresh')} loading={busy} onClick={() => void load()} testId="lint-refresh" />
        </div>

        {findings === null && !error && <Skeleton label={t('Loading lint findings')} height="52px" count={4} />}

        {error && (
          <EmptyState
            tone="error"
            icon={OctagonAlert}
            headingLevel={3}
            title={t('Could not read the lint findings')}
            body={error}
            primaryAction={{ label: t('Retry'), onClick: () => void load() }}
          />
        )}

        {findings !== null && !error && findings.length === 0 && (
          <EmptyState headingLevel={3} title={t('No findings')} body={t('Nothing legal-but-suspicious in the current catalogue.')} />
        )}

        {findings !== null && !error && findings.length > 0 && visible.length === 0 && (
          <p className="fs-agents__empty">{t('No finding matches that search.')}</p>
        )}

        {visible.length > 0 && (
          <ul className="fs-lint__list" data-testid="lint-findings">
            {visible.map((f, i) => {
              const Icon = SEVERITY_ICON[f.severity];
              return (
                <li key={`${f.code}-${f.subject}-${i}`} className="fs-lint__row" data-severity={f.severity} data-testid="lint-finding">
                  <span className="fs-lint__sev" title={t(SEVERITY_LABEL[f.severity])}>
                    <Icon size={14} aria-hidden="true" />
                  </span>
                  <div className="fs-lint__body">
                    <div className="fs-lint__head">
                      <code className="fs-lint__code">{f.code}</code>
                      <span className="fs-lint__subject">{f.subject}</span>
                    </div>
                    <p className="fs-lint__message">{f.message}</p>
                    {f.hint && <p className="fs-lint__hint">{f.hint}</p>}
                  </div>
                </li>
              );
            })}
          </ul>
        )}

        {findings !== null && !error && findings.length > 0 && (
          <p className="fs-lint__summary">
            {tn(counts.error, '{n} error', '{n} errors')} · {tn(counts.warn, '{n} warning', '{n} warnings')} · {tn(counts.info, '{n} note', '{n} notes')}
          </p>
        )}
      </div>
    </Dialog>
  );
}
