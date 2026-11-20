import { ExternalLink } from 'lucide-react';
import { useContext, useEffect, useState } from 'react';
import { AgoraPersonContext } from './util';
import { agoraGet, type Decision } from '../../adapters/agora';
import { locale, t } from '../../i18n';
import './production-decisions.css';

type Piece = { id: number; title: string; view: string; anchor?: string; previews?: unknown[] };
type Gate = { id: number; title: string; order: { title: string }; pieces: Piece[]; decisions: { piece_id: number }[] };
type Variant = { id: number; title: string; status: string; pieces: Piece[] };
type Draft = { id: number; title: string; order: { title: string }; variants: Variant[] };
type Summary = { id: number; title: string };
type RecordRow = {
  id: number; created: number; gate_id: number; note: string; decision: 'keep' | 'cancel'; piece_title: string;
  decided_by?: { id: string; name?: string };
  situation: { decided_by?: string; order?: Summary; gate?: Summary; draft_pass?: Summary; variant?: Summary };
};
type PendingRow = { key: string; title: string; order: string; variants: { id: number; title: string; piece?: Piece }[] };
type State = { space: string; pending: PendingRow[]; pendingError: boolean };

const firstImage = (pieces: Piece[]) => pieces.find(p => p.previews?.length || p.view === 'image');
const anchors = ['keyframe_start', 'keyframe_end', 'keyframe', 'animatic', 'composition', 'blocking_color'];
const decisionImage = (pieces: Piece[]) =>
  firstImage(anchors.flatMap(anchor => pieces.filter(p => p.anchor === anchor))) ?? firstImage(pieces);

export function useProductionDecisions(space: string, version?: string) {
  const [state, setState] = useState<State>();
  useEffect(() => {
    if (!version) return;
    const controller = new AbortController();
    let generation = 0;
    const query = { space_id: space };
    const load = async () => {
      const latest = ++generation;
      const [gates, drafts] = await Promise.allSettled([
        agoraGet<{ waiting: Gate[] }>('orders', { ...query, limit: 1 }, controller.signal),
        agoraGet<{ draft_passes: Draft[] }>('draft_passes', query, controller.signal),
      ]);
      if (controller.signal.aborted || latest !== generation) return;
      const pending: PendingRow[] = [
        ...(drafts.status === 'fulfilled' ? drafts.value.draft_passes : []).map(draft => ({
          key: 'draft-' + draft.id, title: draft.title, order: draft.order.title,
          variants: draft.variants.filter(variant => !['kept', 'discarded'].includes(variant.status)).map(variant => ({
            id: variant.id, title: variant.title, piece: decisionImage(variant.pieces),
          })),
        })),
        ...(gates.status === 'fulfilled' ? gates.value.waiting : []).map(gate => {
          const decided = new Set(gate.decisions.map(row => row.piece_id));
          const piece = firstImage(gate.pieces.filter(candidate => !decided.has(candidate.id)));
          return { key: 'gate-' + gate.id, title: gate.title, order: gate.order.title, variants: piece ? [{ id: piece.id, title: piece.title, piece }] : [] };
        }),
      ];
      setState({ space, pending, pendingError: gates.status === 'rejected' || drafts.status === 'rejected' });
    };
    const refresh = () => { if (!document.hidden) void load(); };
    void load();
    document.addEventListener('visibilitychange', refresh);
    return () => { controller.abort(); document.removeEventListener('visibilitychange', refresh); };
  }, [space, version]);
  return state?.space === space ? state : undefined;
}

function Thumbnail({ piece }: { piece: Piece }) {
  const [failed, fail] = useState(false);
  return failed ? null : <img src={'/api/agora/piece-preview/' + piece.id + (piece.previews?.length ? '?preview=0' : '')}
    alt={piece.title} loading="lazy" onError={() => fail(true)} />;
}

export function PendingProduction({ state, dedicated = false }: { state: State | undefined; dedicated?: boolean }) {
  if (!state) return dedicated ? <p role="status">{t('Loading decisions')}</p> : null;
  if (!state.pending.length && !state.pendingError) return dedicated ? <p>{t('You have no pending decisions.')}</p> : null;
  return <section className="fs-agx-production" data-testid="agora-pending-decisions" aria-label={t('Pending decisions')}>
    {!dedicated && <h2>{t('Pending decisions')}</h2>}
    {state.pending.map(row => (
      <article className="fs-agx-production__gate" key={row.key} data-decision={row.key}>
        <div className="fs-agx-production__heading">
          <div><h3>{row.title}</h3><p>{row.order}</p></div>
          <a href={'/api/hoard/agora/open?' + new URLSearchParams({ space_id: state.space, decision: row.key })}
            target="_blank" rel="noopener">
            {t('Open decision')} <ExternalLink size={14} aria-hidden="true" />
          </a>
        </div>
        {row.variants.length > 0 && <ul className="fs-agx-production__previews">
          {row.variants.map(variant => <li key={variant.id}>
            {variant.piece && <Thumbnail key={variant.piece.id} piece={variant.piece} />}
            {row.key.startsWith('draft-') && <span>{variant.title}</span>}
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
