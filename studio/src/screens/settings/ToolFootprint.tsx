import { Gauge, ShieldAlert } from 'lucide-react';
import { useEffect, useState } from 'react';
import { EmptyState, Skeleton } from '../../components';
import {
  footprintSourceLabel,
  loadToolFootprint,
  type ToolFootprintReport,
} from '../../adapters/toolFootprint';
import { t } from '../../i18n';

/**
 * Settings → Tools: read-only view of GET /api/tools/footprint so the
 * catalogue can be thinned with evidence (OBJ-56 / FAUSTUS §277).
 */

function displaySource(source: string): string {
  const label = footprintSourceLabel(source);
  // Only translate the Built-in label; MCP server ids stay as-is (Claude #300 CHANGES).
  return source === 'builtin' ? t(label) : label;
}

function toolsWord(n: number): string {
  return n === 1 ? t('tool') : t('tools');
}

export function ToolFootprintPanel() {
  const [report, setReport] = useState<ToolFootprintReport | null>(null);
  const [failedStatus, setFailedStatus] = useState<number | 'other' | null>(null);

  const load = () =>
    loadToolFootprint({ top: 15 })
      .then((d) => {
        setReport(d);
        setFailedStatus(null);
      })
      .catch((e: unknown) => {
        setFailedStatus((e as { status?: number })?.status ?? 'other');
        setReport(null);
      });

  useEffect(() => {
    void load();
  }, []);

  if (failedStatus === 401 || failedStatus === 403) {
    return (
      <EmptyState
        tone="denied"
        title={t('Administrators only')}
        body={t('This account cannot see the tool footprint.')}
      />
    );
  }
  if (failedStatus === 426) {
    return (
      <EmptyState
        tone="incompatible"
        title={t('This client is out of date')}
        body={t('Update Faustus before browsing the tool footprint.')}
      />
    );
  }
  if (failedStatus === 'other') {
    return (
      <EmptyState
        icon={ShieldAlert}
        tone="error"
        title={t('Could not read the tool footprint.')}
        body={t('GET /api/tools/footprint failed.')}
        primaryAction={{ label: t('Try again'), onClick: () => void load() }}
      />
    );
  }

  const threshold = report?.thresholds.oversized_description_tokens ?? 250;

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-tool-footprint" data-testid="tool-footprint">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-tool-footprint" className="fs-set__title">{t('Tool footprint')}</h2>
          <p className="fs-prose">
            {t(
              'How much of the prompt the tool catalogue costs, by origin. Name collisions and near-duplicate descriptions are listed so the catalogue can be thinned. Nothing here changes permissions.',
            )}
          </p>
        </div>
      </header>

      {report === null ? (
        <Skeleton label={t('Loading')} count={4} height="44px" />
      ) : report.count === 0 ? (
        <EmptyState icon={Gauge} title={t('No tools in the catalogue.')} />
      ) : (
        <>
          <div className="fs-set__card" data-testid="tool-footprint-summary">
            <p className="fs-set__help">
              <strong>{report.count}</strong> {toolsWord(report.count)} ·{' '}
              <strong>{report.total_tokens.toLocaleString()}</strong> {t('tokens total')} ·{' '}
              <strong>{report.offered_tokens.toLocaleString()}</strong> {t('offered up front')}
              {report.deferred_tokens > 0
                ? ` · ${report.deferred_tokens.toLocaleString()} ${t('deferred')}`
                : ''}
              {report.hidden_tokens > 0
                ? ` · ${report.hidden_tokens.toLocaleString()} ${t('hidden twins')}`
                : ''}
            </p>
          </div>

          <div className="fs-set__card" data-testid="tool-footprint-sources">
            <h3 className="fs-set__card-title">{t('By source')}</h3>
            {report.by_source.length === 0 ? (
              <p className="fs-set__help">{t('No sources.')}</p>
            ) : (
              <ul className="fs-tools">
                {report.by_source.map((bucket) => (
                  <li key={bucket.source} className="fs-tools__row">
                    <span className="fs-tools__text">
                      <strong>{displaySource(bucket.source)}</strong>
                      <span className="fs-set__help">
                        {bucket.tools} {toolsWord(bucket.tools)} · {bucket.tokens.toLocaleString()} {t('tokens')}
                        {bucket.share != null
                          ? ` · ${Math.round(bucket.share * 100)}%`
                          : ''}
                      </span>
                    </span>
                    <code className="fs-tools__id">{bucket.source}</code>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="fs-set__card" data-testid="tool-footprint-collisions">
            <h3 className="fs-set__card-title">{t('Name collisions')}</h3>
            {report.name_collisions.length === 0 ? (
              <p className="fs-set__help">{t('No short names published by more than one source.')}</p>
            ) : (
              <ul className="fs-tools">
                {report.name_collisions.map((row) => (
                  <li key={row.short_name} className="fs-tools__row">
                    <span className="fs-tools__text">
                      <strong>
                        <code>{row.short_name}</code>
                      </strong>
                      <span className="fs-set__help">
                        {row.tools.join(', ')} · {row.sources.join(', ')} ·{' '}
                        {row.tokens.toLocaleString()} {t('tokens')}
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="fs-set__card" data-testid="tool-footprint-near">
            <h3 className="fs-set__card-title">{t('Near-duplicate descriptions')}</h3>
            {report.near_duplicates.length === 0 ? (
              <p className="fs-set__help">{t('No near-duplicate description pairs.')}</p>
            ) : (
              <ul className="fs-tools">
                {report.near_duplicates.map((pair) => (
                  <li key={`${pair.a}|${pair.b}`} className="fs-tools__row">
                    <span className="fs-tools__text">
                      <strong>
                        <code>{pair.a}</code> ↔ <code>{pair.b}</code>
                      </strong>
                      <span className="fs-set__help">
                        {t('similarity')} {pair.similarity}
                        {pair.same_source ? ` · ${t('same source')}` : ''} ·{' '}
                        {pair.tokens.toLocaleString()} {t('tokens')}
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="fs-set__card" data-testid="tool-footprint-oversized">
            <h3 className="fs-set__card-title">
              {t('Long descriptions')} (&gt;{threshold} {t('tokens')})
            </h3>
            {report.oversized_descriptions.length === 0 ? (
              <p className="fs-set__help">
                {t('No descriptions over the trim threshold.')}
              </p>
            ) : (
              <ul className="fs-tools">
                {report.oversized_descriptions.map((row) => (
                  <li key={`${row.source}:${row.name}`} className="fs-tools__row">
                    <span className="fs-tools__text">
                      <strong>
                        <code>{row.name}</code>
                      </strong>
                      <span className="fs-set__help">
                        {displaySource(row.source)} · {row.description_tokens}{' '}
                        {t('description tokens')}
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}
    </section>
  );
}
