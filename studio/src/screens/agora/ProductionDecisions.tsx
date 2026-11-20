import { useCallback, useContext, useEffect, useRef, useState } from 'react';
import { AgoraPersonContext } from './util';
import { agoraGet, agoraPost, type Decision } from '../../adapters/agora';
import { locale, t } from '../../i18n';
import './production-decisions.css';

type Summary = { id: number; title: string };
type RecordRow = {
  id: number; created: number; gate_id: number; note: string; decision: 'keep' | 'cancel'; piece_title: string;
  decided_by?: { id: string; name?: string };
  situation: { decided_by?: string; order?: Summary; gate?: Summary; draft_pass?: Summary; variant?: Summary };
};
type PendingRow = { key: string; space: string };
type Queue = { rows: PendingRow[]; counts: Record<string, number>; order_base?: string | null };
export type PendingProductionState = {
  pending: PendingRow[]; pendingError: boolean; orderBase?: string | null; refresh: () => void;
};

type Cards = {
  renderDecisionCards(container: HTMLElement, rows: PendingRow[], options: {
    lang: string; source: (path: string) => string | null; orderHref: (row: PendingRow) => string | null;
    post: (operation: string, body: Record<string, unknown>) => Promise<unknown>; refresh: () => void;
  }): void;
};
const cardModule = '/api/agora/decision-assets/production_decisions_card.js';

function pieceSource(path: string) {
  const match = /^\/api\/agora\/piece_file\/(\d+)(\?preview=\d+)?$/.exec(path);
  return match ? '/api/agora/piece-content/' + match[1] + (match[2] ?? '') : null;
}

export function useProductionDecisions(version?: string) {
  const [queue, setQueue] = useState<Queue>();
  const [error, setError] = useState(false);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision(value => value + 1), []);
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
  }, [version, revision]);
  return { queue, error, refresh };
}

export function PendingProduction({ state, dedicated = false }: { state: PendingProductionState | undefined; dedicated?: boolean }) {
  const cards = useRef<HTMLDivElement>(null);
  const [cardError, setCardError] = useState(false);
  const lang = locale().startsWith('es') ? 'es' : 'en';
  useEffect(() => {
    const container = cards.current;
    if (!state || !container || !state.pending.length && !container.hasChildNodes()) return;
    let current = true;
    void (import(/* @vite-ignore */ cardModule) as Promise<Cards>).then(({ renderDecisionCards }) => {
      if (!current) return;
      renderDecisionCards(container, state.pending, {
        lang, source: pieceSource,
        orderHref: row => state.orderBase
          ? state.orderBase + '/?' + new URLSearchParams({ space_id: row.space, decision: row.key }) + '#hub/orders'
          : null,
        post: agoraPost,
        refresh: state.refresh,
      });
      setCardError(false);
    }).catch(() => { if (current) setCardError(true); });
    return () => { current = false; };
  }, [state, lang]);
  if (!state) return dedicated ? <p role="status">{t('Loading decisions')}</p> : null;
  if (!dedicated && !state.pending.length && !state.pendingError) return null;
  return <section className="fs-agx-production" data-testid="agora-pending-decisions" aria-label={t('Your pending decisions')}>
    {!dedicated && <h2>{t('Your pending decisions')}</h2>}
    <div ref={cards} />
    {state.pendingError || cardError
      ? <p className="fs-notice" data-tone="warning" role="status">{t('Some pending decisions could not be loaded.')}</p>
      : !state.pending.length && <p role="status">{t('You have no pending decisions.')}</p>}
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
