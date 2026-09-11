import { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { Button, EmptyState, Skeleton } from '../../components';
import {
  addMaterial,
  addReference,
  formatAnchor,
  formatTokenCount,
  getContextPreview,
  getThoughtMap,
  getWires,
  layerSummaryLine,
  removeMaterial,
  removeReference,
  updateMaterial,
  updateReference,
  type ChildInfo,
  type ContextLayer,
  type ContextPreview,
  type MaterialDepth,
  type MaterialInfo,
  type ReferenceDepth,
  type StaleTurns,
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
  /** F1 (CONTRATO_CABLES2): "Regenerar la última (turno M)" on a wire whose
   *  `stale_turns.count > 0` — the panel never regenerates anything itself
   *  (only Studio.tsx has the turn list `regenerateFrom` needs), so this is
   *  handed the 0-based history index of the reply to redo. */
  onRegenerateTurn?: (historyIndex: number) => void;
}

/** F1: "N respuestas escritas con la versión anterior" + "Regenerar la
 *  última (turno M)" — shown on any wire (a child's reference, or a
 *  material) whose `stale_turns.count > 0`. Deliberately separate from the
 *  plain "Update"/"Actualizar referencia" button next to it: that one
 *  clears `stale` going forward (`refresh=True`), this one is about
 *  replies ALREADY saved against the earlier version. */
function StaleTurnsNotice({ staleTurns, onRegenerateTurn }: { staleTurns: StaleTurns; onRegenerateTurn?: (historyIndex: number) => void }) {
  if (!staleTurns.count) return null;
  return (
    <p className="fs-st__stale-turns" data-testid="wire-stale-turns">
      <span>
        {tn(
          staleTurns.count,
          '{n} reply was written with the earlier version',
          '{n} replies were written with the earlier version',
          { n: staleTurns.count },
        )}
      </span>
      {onRegenerateTurn && staleTurns.last_index !== null && (
        <Button
          size="sm"
          label={t('Regenerate the last one (turn {n})', { n: staleTurns.last_index + 1 })}
          onClick={() => onRegenerateTurn(staleTurns.last_index as number)}
          testId="wire-replay"
        />
      )}
    </p>
  );
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
  onRegenerateTurn,
}: {
  child: ChildInfo;
  sessionId: string;
  onChanged: () => void;
  onNotice: SideThreadsPanelProps['onNotice'];
  onRegenerateTurn?: SideThreadsPanelProps['onRegenerateTurn'];
}) {
  const [depth, setDepth] = useState<ReferenceDepth>((child.reference?.depth as ReferenceDepth) ?? 'quote');
  const [busy, setBusy] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const status = statusOf(child);

  useEffect(() => {
    setDepth((child.reference?.depth as ReferenceDepth) ?? 'quote');
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
      {child.reference && <StaleTurnsNotice staleTurns={child.reference.stale_turns} onRegenerateTurn={onRegenerateTurn} />}
    </li>
  );
}

function statusOfMaterial(material: MaterialInfo): { label: string; tone: 'neutral' | 'ok' | 'warn' } {
  if (material.wire.archived) return { label: t('withdrawn'), tone: 'neutral' };
  if (material.stale) return { label: t('outdated'), tone: 'warn' };
  if (material.wire.kind === 'note') return { label: t('Note'), tone: 'ok' };
  return { label: material.wire.depth === 'full' ? t('Full') : t('Selection'), tone: 'ok' };
}

/** F2: one `document` material wired into this session. */
function DocumentMaterialRow({
  material,
  sessionId,
  onChanged,
  onNotice,
  onRegenerateTurn,
}: {
  material: MaterialInfo;
  sessionId: string;
  onChanged: () => void;
  onNotice: SideThreadsPanelProps['onNotice'];
  onRegenerateTurn?: SideThreadsPanelProps['onRegenerateTurn'];
}) {
  const [depth, setDepth] = useState<MaterialDepth>((material.wire.depth as MaterialDepth) || 'selection');
  const [busy, setBusy] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const status = statusOfMaterial(material);

  useEffect(() => {
    setDepth((material.wire.depth as MaterialDepth) || 'selection');
  }, [material.wire.depth, material.wire.id]);

  const fail = (prefix: string) => (error: unknown) => onNotice(`${prefix} ${(error as Error).message}`, 'danger');

  const changeDepth = (next: MaterialDepth) => {
    setDepth(next);
    setBusy(true);
    updateMaterial(sessionId, material.wire.id, { depth: next })
      .then(onChanged)
      .catch(fail(t('Could not update the material.')))
      .finally(() => setBusy(false));
  };

  const refreshIt = () => {
    setBusy(true);
    updateMaterial(sessionId, material.wire.id, { refresh: true })
      .then(onChanged)
      .catch(fail(t('Could not update the material.')))
      .finally(() => setBusy(false));
  };

  const toggleArchived = () => {
    setBusy(true);
    updateMaterial(sessionId, material.wire.id, { archived: !material.wire.archived })
      .then(onChanged)
      .catch(fail(t('Could not update the material.')))
      .finally(() => setBusy(false));
  };

  const remove = () => {
    setBusy(true);
    removeMaterial(sessionId, material.wire.id)
      .then(() => {
        setConfirmRemove(false);
        onChanged();
      })
      .catch(fail(t('Could not remove the material.')))
      .finally(() => setBusy(false));
  };

  return (
    <li className="fs-st__material" data-testid="material-document">
      {/* CONTRATO_CABLES2 F2 deviation: the contract asks for "enlace al
          documento si hay ruta" — this panel has neither a document route
          nor `Studio.tsx`'s `onOpenDoc` (a fichero ajeno's prop), so the
          title renders as plain text rather than a broken/faked link. */}
      <span className="fs-st__child-name" title={material.document?.title ?? material.wire.document_id ?? ''}>
        {material.document?.title ?? t('(removed)')}
      </span>
      <span className="fs-st__badge" data-tone={status.tone}>
        {status.label}
      </span>
      <select
        className="fs-st__depth"
        aria-label={t('Material depth')}
        value={depth}
        disabled={busy}
        onChange={(e) => changeDepth(e.target.value as MaterialDepth)}
        data-testid="material-depth"
      >
        <option value="selection">{t('Selection')}</option>
        <option value="full">{t('Full')}</option>
      </select>
      {material.stale && <Button size="sm" label={t('Update')} disabled={busy} onClick={refreshIt} />}
      <Button size="sm" label={material.wire.archived ? t('Wire') : t('Withdraw')} disabled={busy} onClick={toggleArchived} />
      {confirmRemove ? (
        <>
          <Button size="sm" variant="danger-solid" label={t('Confirm removal')} disabled={busy} onClick={remove} testId="material-remove-confirm" />
          <Button size="sm" label={t('Cancel')} disabled={busy} onClick={() => setConfirmRemove(false)} />
        </>
      ) : (
        <Button size="sm" variant="danger" label={t('Remove')} disabled={busy} onClick={() => setConfirmRemove(true)} testId="material-remove" />
      )}
      <StaleTurnsNotice staleTurns={material.stale_turns} onRegenerateTurn={onRegenerateTurn} />
    </li>
  );
}

/** F2: one free-form `note` material wired into this session. */
function NoteMaterialRow({
  material,
  sessionId,
  onChanged,
  onNotice,
  onRegenerateTurn,
}: {
  material: MaterialInfo;
  sessionId: string;
  onChanged: () => void;
  onNotice: SideThreadsPanelProps['onNotice'];
  onRegenerateTurn?: SideThreadsPanelProps['onRegenerateTurn'];
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(material.wire.note_text ?? '');
  const [busy, setBusy] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);

  useEffect(() => {
    setText(material.wire.note_text ?? '');
  }, [material.wire.note_text, material.wire.id]);

  const fail = (prefix: string) => (error: unknown) => onNotice(`${prefix} ${(error as Error).message}`, 'danger');

  const save = () => {
    if (!text.trim() || busy) return;
    setBusy(true);
    updateMaterial(sessionId, material.wire.id, { noteText: text.trim() })
      .then(() => {
        setEditing(false);
        onChanged();
      })
      .catch(fail(t('Could not update the note.')))
      .finally(() => setBusy(false));
  };

  const toggleArchived = () => {
    setBusy(true);
    updateMaterial(sessionId, material.wire.id, { archived: !material.wire.archived })
      .then(onChanged)
      .catch(fail(t('Could not update the note.')))
      .finally(() => setBusy(false));
  };

  const remove = () => {
    setBusy(true);
    removeMaterial(sessionId, material.wire.id)
      .then(() => {
        setConfirmRemove(false);
        onChanged();
      })
      .catch(fail(t('Could not remove the note.')))
      .finally(() => setBusy(false));
  };

  const noteText = material.wire.note_text ?? '';
  const truncated = noteText.length > 140 ? `${noteText.slice(0, 140)}…` : noteText;

  return (
    <li className="fs-st__material" data-testid="material-note">
      <span className="fs-st__badge" data-tone={material.wire.archived ? 'neutral' : 'ok'}>
        {material.wire.archived ? t('withdrawn') : t('Note')}
      </span>
      {editing ? (
        <>
          <textarea
            className="fs-field fs-st__note-edit"
            value={text}
            disabled={busy}
            onChange={(e) => setText(e.target.value)}
            data-testid="material-note-edit"
          />
          <Button size="sm" variant="primary" label={t('Save')} disabled={busy || !text.trim()} onClick={save} testId="material-note-save" />
          <Button
            size="sm"
            label={t('Cancel')}
            disabled={busy}
            onClick={() => {
              setEditing(false);
              setText(noteText);
            }}
          />
        </>
      ) : (
        <>
          <span className="fs-st__child-name" data-testid="material-note-text">
            {truncated || t('(empty)')}
          </span>
          <Button size="sm" label={t('Edit')} disabled={busy} onClick={() => setEditing(true)} testId="material-note-edit-toggle" />
        </>
      )}
      <Button size="sm" label={material.wire.archived ? t('Wire') : t('Withdraw')} disabled={busy} onClick={toggleArchived} />
      {confirmRemove ? (
        <>
          <Button size="sm" variant="danger-solid" label={t('Confirm removal')} disabled={busy} onClick={remove} testId="material-remove-confirm" />
          <Button size="sm" label={t('Cancel')} disabled={busy} onClick={() => setConfirmRemove(false)} />
        </>
      ) : (
        <Button size="sm" variant="danger" label={t('Remove')} disabled={busy} onClick={() => setConfirmRemove(true)} testId="material-remove" />
      )}
      <StaleTurnsNotice staleTurns={material.stale_turns} onRegenerateTurn={onRegenerateTurn} />
    </li>
  );
}

/** F2 block: "Add a note" — the only way a `note` material is ever created
 *  from inside the panel (a `document` material is created from the
 *  Composer's "Fijar" pin — see `Composer.tsx`). */
function AddNoteForm({ sessionId, onChanged, onNotice }: { sessionId: string; onChanged: () => void; onNotice: SideThreadsPanelProps['onNotice'] }) {
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = () => {
    if (!text.trim() || busy) return;
    setBusy(true);
    addMaterial(sessionId, { kind: 'note', noteText: text.trim() })
      .then(() => {
        setText('');
        onChanged();
      })
      .catch((error: Error) => onNotice(`${t('Could not add the note.')} ${error.message}`, 'danger'))
      .finally(() => setBusy(false));
  };

  return (
    <div className="fs-st__add-note">
      <textarea
        className="fs-field fs-st__note-edit"
        placeholder={t('Add a note')}
        value={text}
        disabled={busy}
        onChange={(e) => setText(e.target.value)}
        data-testid="material-add-note-text"
      />
      <Button size="sm" variant="primary" label={t('Add a note')} disabled={busy || !text.trim()} onClick={submit} testId="material-add-note" />
    </div>
  );
}

function layerLabel(layer: ContextLayer['layer']): string {
  switch (layer) {
    case 'materials':
      return t('Materials');
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

export default function SideThreadsPanel({ sessionId, onNotice, onRegenerateTurn }: SideThreadsPanelProps) {
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
        <EmptyState title={t('Context wires')} body={t('Send the first message before branching a side thread from it.')} />
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

          {/* F2 (CONTRATO_CABLES2): "encima de los excursos" — Materials
              comes before the children/excursos section below. */}
          <section className="fs-st__materials" aria-label={t('Materials')}>
            <h3 className="fs-st__heading">{t('Materials')}</h3>
            {wires.materials.length === 0 ? (
              <p className="fs-studio__hint">{t('No materials pinned yet.')}</p>
            ) : (
              <ul className="fs-st__material-list">
                {wires.materials.map((material) =>
                  material.wire.kind === 'note' ? (
                    <NoteMaterialRow
                      key={material.wire.id}
                      material={material}
                      sessionId={sessionId}
                      onChanged={() => void load()}
                      onNotice={onNotice}
                      onRegenerateTurn={onRegenerateTurn}
                    />
                  ) : (
                    <DocumentMaterialRow
                      key={material.wire.id}
                      material={material}
                      sessionId={sessionId}
                      onChanged={() => void load()}
                      onNotice={onNotice}
                      onRegenerateTurn={onRegenerateTurn}
                    />
                  ),
                )}
              </ul>
            )}
            <AddNoteForm sessionId={sessionId} onChanged={() => void load()} onNotice={onNotice} />
          </section>

          <section className="fs-st__children" aria-label={t('Side threads of this conversation')}>
            <h3 className="fs-st__heading">{t('Side threads of this conversation')}</h3>
            {wires.children.length === 0 ? (
              <p className="fs-studio__hint">{t('No side threads yet. Select text in a reply to start one.')}</p>
            ) : (
              <ul className="fs-st__child-list">
                {wires.children.map((child) => (
                  <ChildRow key={child.wire.id} child={child} sessionId={sessionId} onChanged={() => void load()} onNotice={onNotice} onRegenerateTurn={onRegenerateTurn} />
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
