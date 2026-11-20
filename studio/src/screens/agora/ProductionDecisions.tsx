import { ExternalLink } from 'lucide-react';
import { useContext, useEffect, useState } from 'react';
import { AgoraPersonContext } from './util';
import { agoraGet, type Decision } from '../../adapters/agora';
import { locale, t } from '../../i18n';
import './production-decisions.css';

type Summary = { id: number; title: string };
type RecordRow = {
  id: number; created: number; gate_id: number; note: string; decision: 'keep' | 'cancel'; piece_title: string;
  decided_by?: { id: string; name?: string };
  situation: { decided_by?: string; order?: Summary; gate?: Summary; draft_pass?: Summary; variant?: Summary };
};
type Preview = { piece_id: number; title: string; preview: boolean };
type PendingRow = {
  key: string; title: string; order: string; space: string; image: Preview | null;
  variants?: { id: number; title: string; image: Preview | null }[];
  pieces?: string[];
};
type Queue = { rows: PendingRow[]; counts: Record<string, number> };
export type PendingProductionState = { pending: PendingRow[]; pendingError: boolean };

export function useProductionDecisions(version?: string) {
  const [queue, setQueue] = useState<Queue>();
  const [error, setError] = useState(false);
  useEffect(() => {
    if (!version) return;
    const controller = new AbortController();
    let generation = 0;
    async function refresh() {
      if (document.hidden) return;
      const latest = ++generation;
      const current = () => !controller.signal.aborted && latest === generation;
      try {
        const result = await agoraGet<Queue>('pending_decisions', { all_spaces: 1 }, controller.signal);
        if (current()) { setQueue(result); setError(false); }
      } catch {
        if (current()) setError(true);
      }
    }
    void refresh();
    const interval = window.setInterval(refresh, 8000);
    document.addEventListener('visibilitychange', refresh);
    return () => {
      controller.abort();
      window.clearInterval(interval);
      document.removeEventListener('visibilitychange', refresh);
    };
  }, [version]);
  return { queue, error };
}

function Thumbnail({ image }: { image: Preview }) {
  const [failed, fail] = useState(false);
  return failed ? null : <img src={'/api/agora/piece-preview/' + image.piece_id + (image.preview ? '?preview=0' : '')}
    alt={image.title} loading="lazy" onError={() => fail(true)} />;
}

export function PendingProduction({ state, dedicated = false }: { state: PendingProductionState | undefined; dedicated?: boolean }) {
  if (!state) return dedicated ? <p role="status">{t('Loading decisions')}</p> : null;
  if (!state.pending.length && !state.pendingError) return dedicated ? <p>{t('You have no pending decisions.')}</p> : null;
  return <section className="fs-agx-production" data-testid="agora-pending-decisions" aria-label={t('Your pending decisions')}>
    {!dedicated && <h2>{t('Your pending decisions')}</h2>}
    {state.pending.map(row => (
      <article className="fs-agx-production__gate" key={row.key} data-decision={row.key}>
        <div className="fs-agx-production__heading">
          <div><h3>{row.title}</h3><p>{row.order}</p></div>
          <a href={'/api/hoard/agora/open?' + new URLSearchParams({ space_id: row.space, decision: row.key })}
            target="_blank" rel="noopener">
            {t('Open decision')} <ExternalLink size={14} aria-hidden="true" />
          </a>
        </div>
        {row.image && <div className="fs-agx-production__previews"><Thumbnail key={row.image.piece_id} image={row.image} /></div>}
        {!!row.pieces?.length && <p className="fs-agx-production__pieces">{row.pieces.join(' · ')}</p>}
        {!!row.variants?.length && <ul className="fs-agx-production__previews">
          {row.variants.map(variant => <li key={variant.id}>
            {variant.image && <Thumbnail key={variant.image.piece_id} image={variant.image} />}
            <span>{variant.title}</span>
          </li>)}
        </ul>}
      </article>
    ))}
    {state.pendingError && <p className="fs-notice" data-tone="warning" role="status">{t('Some pending decisions could not be loaded.')}</p>}
  </section>;
}

export async function readDecisionLog(space: string, signal: AbortSignal) {
  const query = { space_id: space, limit: 100 };
  const log = await agoraGet<{ decisions: Decision[]; production_decisions?: RecordRow[] }>('decisions', query, signal);
  let records = log.production_decisions;
  let productionError = false;
  if (!records) {
    try {
      records = (await agoraGet<{ decisions: RecordRow[] }>('order_decisions', query, signal)).decisions;
    } catch {
      productionError = true;
    }
  }
  const unique = new Map<string, RecordRow>();
  for (const row of records ?? []) {
    const key = JSON.stringify([row.situation.draft_pass?.id, row.situation.variant?.id, row.gate_id, Math.floor(row.created), row.decision, row.note]);
    if (!unique.has(key)) unique.set(key, row);
  }
  return { threads: log.decisions, production: [...unique.values()], productionError };
}

export function ProductionHistory({ rows, error }: { rows: RecordRow[]; error: boolean }) {
  const person = useContext(AgoraPersonContext);
  if (!rows.length && !error) return null;
  return <section className="fs-agx-production" data-testid="agora-production-history" aria-labelledby="agora-production-history-heading">
    <h3 id="agora-production-history-heading">{t('Production decision history')}</h3>
    {error && <p role="status">{t('Production decision history could not be loaded.')}</p>}
    {rows.map(row => {
      const { order, gate, draft_pass: pass, variant } = row.situation;
      const actorId = row.decided_by?.id || row.situation.decided_by;
      const actorName = row.decided_by?.name || (actorId && actorId === person?.id ? person?.name || actorId : actorId);
      const date = new Date(row.created * 1000).toLocaleDateString(locale());
      return <article className="fs-agx-production__record" key={row.id}>
        <h3>{actorName ? t('{name} · Decision ({date})', { name: actorName, date }) : t('Decision ({date})', { date })}</h3>
        <p>{pass?.title || gate?.title || row.piece_title}</p>
        <p>{[order?.title, row.decision === 'keep' && variant?.title,
          row.decision === 'keep' ? t('Kept') : pass ? t('Discarded') : t('Cancelled')].filter(Boolean).join(' · ')}</p>
        {row.note && <p className="fs-agx-production__note">{row.note}</p>}
      </article>;
    })}
  </section>;
}
