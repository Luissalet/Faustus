import { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { Button, EmptyState, Skeleton } from '../../components';
import {
  addReference,
  formatAnchor,
  formatTokenCount,
  getContextPreview,
  getThoughtMap,
  getWires,
  layerSummaryLine,
  removeReference,
  updateReference,
  type ChildInfo,
  type ContextLayer,
  type ContextPreview,
  type ReferenceDepth,
  type ThoughtMap,
  type ThoughtMapNode,
  type Wire,
  type WiresFor,
} from '../../adapters/sideThreads';
import { t, tn } from '../../i18n';

/**
 * B4 (CONTRATO_EXCURSOS.md) — the excursos panel: a session's parent (if
 * it is itself a side thread), its own side threads and their wiring, what
 * the model will actually see next turn, and the thought map.
 *
 * PLACEMENT: the contract asks to put this "donde menos toque Studio.tsx —
 * a tab on SidePanel.tsx if it has tabs, else a header button opening a
 * Popover/Dialog". `SidePanel.tsx` DOES have tabs (`TABS` in that file),
 * but it is not one of this lote's own files (CONTRATO_EXCURSOS.md's
 * Lote B file list omits it, and Lote A/B both forbid touching a fichero
 * ajeno) — so the tab route is closed, and this panel is mounted from a
 * new `Waypoints` button in Studio.tsx's header, inside a `Popover`
 * (`components/Popover.tsx`), which is the "menos toque Studio.tsx" branch
 * left. Radix's `Popover.Content` unmounts when closed, so this component
 * simply fetches on mount — that mount happens exactly on open, which is
 * also what gives block 3's "se refresca al abrir el panel" for free.
 */

export interface SideThreadsPanelProps {
  sessionId: string | null;
  onNotice: (text: string, tone?: 'info' | 'warning' | 'danger', action?: { label: string; onClick: () => void }) => void;
}

function statusOf(child: ChildInfo): { label: string; tone: 'neutral' | 'ok' | 'warn' } {
  if (!child.reference) return { label: t('not wired'), tone: 'neutral' };
  if (child.stale) return { label: t('outdated'), tone: 'warn' };
  return { label: child.reference.depth === 'full' ? t('Full') : t('Quote'), tone: 'ok' };
}

function ChildRow({
  child,
  sessionId,
  onChanged,
  onNotice,
}: {
  child: ChildInfo;
  sessionId: string;
  onChanged: () => void;
  onNotice: SideThreadsPanelProps['onNotice'];
}) {
  const [depth, setDepth] = useState<ReferenceDepth>(child.reference?.depth ?? 'quote');
  const [busy, setBusy] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const status = statusOf(child);

  useEffect(() => {
    setDepth(child.reference?.depth ?? 'quote');
  }, [child.reference?.depth, child.wire.id]);

  const fail = (prefix: string) => (error: unknown) => onNotice(`${prefix} ${(error as Error).message}`, 'danger');

  const changeDepth = (next: ReferenceDepth) => {
    setDepth(next);
    if (!child.reference) return; // Not wired yet: the pick only takes effect on "Cablear".
    setBusy(true);
    updateReference(sessionId, child.reference.id, { depth: next })
      .then(onChanged)
      .catch(fail(t('Could not update the reference.')))
      .finally(() => setBusy(false));
  };

  const wireIt = () => {
    setBusy(true);
    addReference(sessionId, { sourceSessionId: child.session.id, depth })
      .then(onChanged)
      .catch(fail(t('Could not wire the reference.')))
      .finally(() => setBusy(false));
  };

  const refreshIt = () => {
    if (!child.reference) return;
    setBusy(true);
    updateReference(sessionId, child.reference.id, { refresh: true })
      .then(onChanged)
      .catch(fail(t('Could not update the reference.')))
      .finally(() => setBusy(false));
  };

  const withdraw = () => {
    if (!child.reference) return;
    setBusy(true);
    updateReference(sessionId, child.reference.id, { archived: true })
      .then(onChanged)
      .catch(fail(t('Could not update the reference.')))
      .finally(() => setBusy(false));
  };

  const remove = () => {
    if (!child.reference) return;
    setBusy(true);
    removeReference(sessionId, child.reference.id)
      .then(() => {
        setConfirmRemove(false);
        onChanged();
      })
      .catch(fail(t('Could not remove the reference.')))
      .finally(() => setBusy(false));
  };

  return (
    <li className="fs-st__child" data-testid="side-threads-child">
      <Link to={`/studio?s=${encodeURIComponent(child.session.id)}`} className="fs-st__child-name">
        {child.session.name}
      </Link>
      <span className="fs-st__child-meta">
        {tn(child.session.message_count, '{n} message', '{n} messages', { n: child.session.message_count })}
      </span>
      <span className="fs-st__badge" data-tone={status.tone}>
        {status.label}
      </span>
      <select
        className="fs-st__depth"
        aria-label={t('Reference depth')}
        value={depth}
        disabled={busy}
        onChange={(e) => changeDepth(e.target.value as ReferenceDepth)}
        data-testid="side-threads-depth"
      >
        <option value="quote">{t('Quote')}</option>
        <option value="full">{t('Full')}</option>
      </select>
      {child.reference ? (
        <>
          {child.stale && <Button size="sm" label={t('Update')} disabled={busy} onClick={refreshIt} />}
          <Button size="sm" label={t('Withdraw')} disabled={busy} onClick={withdraw} />
          {confirmRemove ? (
            <>
              <Button size="sm" variant="danger-solid" label={t('Confirm removal')} disabled={busy} onClick={remove} testId="wire-remove-confirm" />
              <Button size="sm" label={t('Cancel')} disabled={busy} onClick={() => setConfirmRemove(false)} />
            </>
          ) : (
            <Button size="sm" variant="danger" label={t('Remove wire')} disabled={busy} onClick={() => setConfirmRemove(true)} testId="wire-remove" />
          )}
        </>
      ) : (
        <Button size="sm" variant="primary" label={t('Wire')} disabled={busy} onClick={wireIt} />
      )}
    </li>
  );
}

function layerLabel(layer: ContextLayer['layer']): string {
  switch (layer) {
    case 'references':
      return t('References');
    case 'inherited':
      return t('Inherited');
    default:
      return t('Own');
  }
}

function ThoughtMapTree({ node, depth = 0 }: { node: ThoughtMapNode; depth?: number }) {
  return (
    <li className="fs-st__map-node" style={{ ['--depth' as string]: depth }} data-current={node.current || undefined}>
      <Link to={`/studio?s=${encodeURIComponent(node.id)}`} className="fs-st__map-link">
        {node.name}
      </Link>
      <span className="fs-st__map-meta">{tn(node.message_count, '{n} message', '{n} messages', { n: node.message_count })}</span>
      {node.children.length > 0 && (
        <ul className="fs-st__map-list">
          {node.children.map((child) => (
            <ThoughtMapTree key={child.id} node={child} depth={depth + 1} />
          ))}
        </ul>
      )}
    </li>
  );
}

export default function SideThreadsPanel({ sessionId, onNotice }: SideThreadsPanelProps) {
  const navigate = useNavigate();
  const [wires, setWires] = useState<WiresFor | null>(null);
  // The reference (if any) FROM this side thread TO its parent — computed
  // from the PARENT's own `wires_for` (its `references_in`, which alone
  // carries `stale`; this session's own `references_out` does not, see
  // `src/side_threads.py::wires_for`) rather than a new endpoint, so
  // "Traer de vuelta" can tell "not wired" from "wired but stale" from
  // "wired and current".
  const [parentIncoming, setParentIncoming] = useState<{ wire: Wire; stale: boolean } | null>(null);
  const [preview, setPreview] = useState<ContextPreview | null>(null);
  const [map, setMap] = useState<ThoughtMap | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [bringBackBusy, setBringBackBusy] = useState(false);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      if (!sessionId) {
        setLoading(false);
        return;
      }
      setLoading(true);
      setError(null);
      try {
        const [w, p, m] = await Promise.all([getWires(sessionId, signal), getContextPreview(sessionId, signal), getThoughtMap(sessionId, signal)]);
        if (signal?.aborted) return;
        setWires(w);
        setPreview(p);
        setMap(m);
        if (w.parent) {
          try {
            const parentWires = await getWires(w.parent.session.id, signal);
            if (signal?.aborted) return;
            const mine = parentWires.references_in.find((r) => r.wire.source_session_id === sessionId);
            setParentIncoming(mine ? { wire: mine.wire, stale: mine.stale } : null);
          } catch {
            if (!signal?.aborted) setParentIncoming(null);
          }
        } else {
          setParentIncoming(null);
        }
      } catch (e) {
        if (!signal?.aborted) setError((e as Error).message);
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [sessionId],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const bringBack = () => {
    if (!wires?.parent || !sessionId || bringBackBusy) return;
    const parent = wires.parent.session;
    setBringBackBusy(true);
    addReference(parent.id, { sourceSessionId: sessionId, depth: 'quote' })
      .then(() => {
        onNotice(t('Reference added to "{name}"', { name: parent.name }), 'info', {
          label: t('Go'),
          onClick: () => navigate(`/studio?s=${encodeURIComponent(parent.id)}`),
        });
        void load();
      })
      .catch((e: Error) => onNotice(`${t('Could not wire the reference.')} ${e.message}`, 'danger'))
      .finally(() => setBringBackBusy(false));
  };

  const refreshBringBack = () => {
    if (!wires?.parent || !parentIncoming || bringBackBusy) return;
    setBringBackBusy(true);
    updateReference(wires.parent.session.id, parentIncoming.wire.id, { refresh: true })
      .then(() => void load())
      .catch((e: Error) => onNotice(`${t('Could not update the reference.')} ${e.message}`, 'danger'))
      .finally(() => setBringBackBusy(false));
  };

  if (!sessionId) {
    return (
      <div className="fs-st" data-testid="side-threads-panel">
        <EmptyState title={t('Side threads')} body={t('Send the first message before branching a side thread from it.')} />
      </div>
    );
  }

  return (
    <div className="fs-st" data-testid="side-threads-panel">
      {loading && <Skeleton label={t('Loading the side threads')} count={4} height="20px" />}
      {!loading && error && (
        <EmptyState tone="error" title={t('Could not load the side threads.')} body={error} primaryAction={{ label: t('Retry'), onClick: () => void load() }} />
      )}
      {!loading && !error && wires && (
        <>
          {wires.parent && (
            <section className="fs-st__parent" data-testid="side-threads-parent">
              <p className="fs-st__parent-line">
                {t('Side thread of')} «
                <Link to={`/studio?s=${encodeURIComponent(wires.parent.session.id)}`}>{wires.parent.session.name}</Link>» ·{' '}
                {formatAnchor(wires.parent.wire.anchor_index, wires.parent.anchor_state)}
              </p>
              {wires.parent.anchor_state === 'missing' && (
                <p className="fs-notice" data-tone="warning">
                  {t('The whole parent transcript is inherited instead of stopping at that turn.')}
                </p>
              )}
              {parentIncoming ? (
                parentIncoming.stale ? (
                  <Button size="sm" label={t('Update reference')} loading={bringBackBusy} onClick={refreshBringBack} testId="bring-back" />
                ) : (
                  <Button size="sm" label={t('Already referenced')} disabled testId="bring-back" />
                )
              ) : (
                <Button size="sm" variant="primary" label={t('Bring back')} loading={bringBackBusy} onClick={bringBack} testId="bring-back" />
              )}
            </section>
          )}

          <section className="fs-st__children" aria-label={t('Side threads of this conversation')}>
            <h3 className="fs-st__heading">{t('Side threads of this conversation')}</h3>
            {wires.children.length === 0 ? (
              <p className="fs-studio__hint">{t('No side threads yet. Select text in a reply to start one.')}</p>
            ) : (
              <ul className="fs-st__child-list">
                {wires.children.map((child) => (
                  <ChildRow key={child.wire.id} child={child} sessionId={sessionId} onChanged={() => void load()} onNotice={onNotice} />
                ))}
              </ul>
            )}
          </section>

          {preview && (
            <section className="fs-st__preview">
              <p className="fs-st__summary" data-testid="side-threads-summary">
                {layerSummaryLine(preview)}
              </p>
              <details>
                <summary>{t('Layer detail')}</summary>
                <ul className="fs-st__layers">
                  {preview.layers.map((layer) => (
                    <li key={layer.layer}>
                      <span>{layerLabel(layer.layer)}</span>
                      <span>
                        {tn(layer.messages, '{n} message', '{n} messages', { n: layer.messages })} · ~{formatTokenCount(layer.tokens)} tok
                      </span>
                    </li>
                  ))}
                </ul>
              </details>
            </section>
          )}

          {map?.id && (
            <section className="fs-st__map" data-testid="side-threads-map">
              <h3 className="fs-st__heading">{t('Thought map')}</h3>
              <ul className="fs-st__map-list fs-st__map-root">
                <ThoughtMapTree node={map as ThoughtMapNode} />
              </ul>
              {map.truncated && <p className="fs-studio__hint">{t('The map was cut off; it goes deeper than this.')}</p>}
            </section>
          )}
        </>
      )}
    </div>
  );
}
