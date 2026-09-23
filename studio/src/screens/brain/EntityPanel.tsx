import { AlertTriangle, Clock, RefreshCw, Sparkles } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button, Skeleton } from '../../components';
import { loadEntityProfile, runWikiRefresh, updateEntity, type EntityProfile } from '../../adapters/brain';
import { locale, t } from '../../i18n';

/**
 * An entity note's right-panel extra: relations valid *now* against
 * relations closed with a date window, a timeline, and the "as of" date
 * picker that reloads the whole profile at that instant — the same
 * `entities.profile(..., as_of=...)` the vault note's generated `##
 * Relations`/`## History` sections are rendered from, so the panel and the
 * note never disagree about what was true when.
 */

function fmt(iso: string | null): string {
  if (!iso) return '—';
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return iso;
  return new Date(at).toLocaleDateString(locale(), { dateStyle: 'medium' });
}

export function EntityPanel({ entityId, onChanged }: { entityId: string; onChanged?: () => void }) {
  const [asOf, setAsOf] = useState('');
  const [profile, setProfile] = useState<EntityProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    loadEntityProfile(entityId, asOf || undefined, controller.signal)
      .then(setProfile)
      .catch(setError)
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [entityId, asOf]);

  async function toggleHidden() {
    if (!profile) return;
    await updateEntity(entityId, { hidden: !profile.entity.hidden });
    setProfile({ ...profile, entity: { ...profile.entity, hidden: !profile.entity.hidden } });
    onChanged?.();
  }

  async function refreshSummary() {
    setRefreshing(true);
    try {
      await runWikiRefresh(entityId);
      setProfile(await loadEntityProfile(entityId, asOf || undefined));
    } catch (e) {
      setError(e);
    } finally {
      setRefreshing(false);
    }
  }

  if (loading && !profile) return <Skeleton label={t('Reading the entity')} count={3} height="40px" />;
  if (!profile) return <p className="fs-notice" data-tone="danger">{String((error as Error)?.message ?? error ?? t('The entity could not be read'))}</p>;

  return (
    <section className="fs-brain__entity" data-testid="brain-entity-panel">
      <header className="fs-brain__entity-head">
        <span className="fs-badge">{profile.entity.type}</span>
        {profile.entity.aliases.length > 0 && <span className="fs-muted">{profile.entity.aliases.join(', ')}</span>}
        <Button variant="ghost" size="sm" label={profile.entity.hidden ? t('Unhide') : t('Hide')} onClick={() => void toggleHidden()} />
      </header>

      <label className="fs-brain__asof">
        <Clock size={13} aria-hidden="true" /> {t('As of')}
        <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} aria-label={t('As of date')} data-testid="brain-entity-asof" />
        {asOf && <Button variant="ghost" size="sm" label={t('Now')} onClick={() => setAsOf('')} />}
      </label>

      <div className="fs-brain__entity-summary">
        <div className="fs-brain__entity-summary-head">
          <h4>{t('Summary')}</h4>
          <Button variant="ghost" size="sm" icon={RefreshCw} label={t('Refresh')} loading={refreshing} onClick={() => void refreshSummary()} testId="brain-entity-refresh" />
        </div>
        <p className="fs-muted">{profile.summary || t('No summary yet — refresh once there are facts recorded.')}</p>
      </div>

      <h4>{t('Facts')}</h4>
      {profile.facts.length === 0 && <p className="fs-muted">{t('No facts recorded yet.')}</p>}
      <ul className="fs-brain__facts">
        {profile.facts.map((fact, i) => (
          <li key={i} data-valid={fact.validNow || undefined}>
            {fact.text}
            {(fact.validFrom || fact.validUntil) && (
              <span className="fs-muted"> · {fmt(fact.validFrom)} — {fact.validUntil ? fmt(fact.validUntil) : t('now')}</span>
            )}
          </li>
        ))}
      </ul>

      <h4>{t('Relations')}</h4>
      {profile.relations.length === 0 && <p className="fs-muted">{t('No relation is valid at this date.')}</p>}
      <ul className="fs-brain__relations">
        {profile.relations.map((rel) => (
          <li key={rel.id}>
            <span className="fs-brain__rel-verb">{rel.rel}</span> {rel.dstName}
          </li>
        ))}
      </ul>

      {profile.history.length > 0 && (
        <details className="fs-brain__history">
          <summary>{t('History ({n} closed)', { n: profile.history.length })}</summary>
          <ul className="fs-brain__relations">
            {profile.history.map((rel) => (
              <li key={rel.id}>
                <span className="fs-brain__rel-verb">{rel.rel}</span> {rel.dstName}
                <span className="fs-muted"> · {fmt(rel.validFrom)} — {fmt(rel.validUntil)}</span>
              </li>
            ))}
          </ul>
        </details>
      )}

      <h4>{t('Timeline')}</h4>
      {profile.timeline.length === 0 && <p className="fs-muted">{t('Nothing recorded yet.')}</p>}
      <ol className="fs-brain__timeline">
        {profile.timeline.map((ev, i) => (
          <li key={i}>
            <span className="fs-muted">{fmt(ev.at)}</span> · {ev.kind} — {ev.text}
          </li>
        ))}
      </ol>

      {error != null && (
        <p className="fs-notice" data-tone="warning">
          <AlertTriangle size={12} aria-hidden="true" /> {String((error as Error)?.message ?? error)}
        </p>
      )}
      <p className="fs-muted fs-brain__entity-note">
        <Sparkles size={11} aria-hidden="true" /> {t('Deterministic first: relations and facts come from stored records, not from a model asserting them.')}
      </p>
    </section>
  );
}
