import { useCallback, useEffect, useMemo, useState } from 'react';
import { Redo2, Undo2, ZoomIn, ZoomOut, Shapes, Download, Wand2 } from 'lucide-react';
import { Button, EmptyState, Toast } from '../../components';
import { t } from '../../i18n';
import * as api from '../../adapters/creator';
import * as cvApi from '../../adapters/creator_canvas';
// `undoTo` is a GENERIC document-revision endpoint (`routes/creator_timeline_routes.py`
// wires it, but `undo_document` never checks `doc.kind` — see that route's
// source) — reused here rather than duplicated onto a canvas-specific path.
import { undoTo } from '../../adapters/creator_timeline';
import { CanvasLayers } from './CanvasLayers';
import './canvas.css';

/**
 * WP20 — Canvas screen: a layered image editor over a `canvas`
 * CreatorDocument. Every mutating gesture is a typed `canvas.*` op through
 * `adapters/creator_canvas.ts`, applied with the document's OWN
 * `expected_revision` — a 409 (`RevisionConflictError`) reloads rather than
 * silently overwriting, exactly like `Timeline.tsx`. The underlying picture
 * is always the SERVER's deterministic composite
 * (`GET /api/creator/canvas/{id}/render`); `CanvasLayers` only adds an
 * interactive preview on top for the gesture in progress.
 */

export interface CanvasProps {
  doc: api.CreatorDocument;
  onDocUpdated: (doc: api.CreatorDocument) => void;
  onRevisionConflict: () => void;
}

export function Canvas({ doc, onDocUpdated, onRevisionConflict }: CanvasProps) {
  const content = doc.content as cvApi.CanvasDocContent;
  const [zoom, setZoom] = useState(1);
  const [selectedLayerId, setSelectedLayerId] = useState<string | null>(null);
  const [maskMode, setMaskMode] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [revisionHistory, setRevisionHistory] = useState<number[]>([doc.revision]);
  const [undoIndex, setUndoIndex] = useState(0);
  const [inpaintPrompt, setInpaintPrompt] = useState('');
  const [inpaintBusy, setInpaintBusy] = useState(false);

  useEffect(() => {
    setRevisionHistory((prev) => (prev[prev.length - 1] === doc.revision ? prev : [...prev, doc.revision]));
    setUndoIndex(0);
  }, [doc.revision]);

  const runOp = useCallback(async (fn: () => Promise<api.ApplyCommandResult>) => {
    setError(null);
    try {
      const result = await fn();
      onDocUpdated(result.document);
    } catch (err) {
      if (err instanceof api.RevisionConflictError) onRevisionConflict();
      else setError(err instanceof Error ? err.message : String(err));
    }
  }, [onDocUpdated, onRevisionConflict]);

  const onCommitMove = useCallback((layerId: string, offset: { x: number; y: number }) => {
    void runOp(() => cvApi.moveLayer(doc.id, doc.revision, layerId, offset));
  }, [doc.id, doc.revision, runOp]);

  const onCommitTransform = useCallback((layerId: string, scale: number, degrees?: number) => {
    void runOp(() => cvApi.setLayerTransform(doc.id, doc.revision, layerId, scale, degrees));
  }, [doc.id, doc.revision, runOp]);

  const onCommitPolygonMask = useCallback((layerId: string, points: { x: number; y: number }[]) => {
    void runOp(() => cvApi.setPolygonMask(doc.id, doc.revision, layerId, points));
    setMaskMode(false);
  }, [doc.id, doc.revision, runOp]);

  const setBlendMode = useCallback((mode: cvApi.BlendMode) => {
    if (!selectedLayerId) return;
    void runOp(() => cvApi.setBlendMode(doc.id, doc.revision, selectedLayerId, mode));
  }, [doc.id, doc.revision, selectedLayerId, runOp]);

  const reorder = useCallback((order: string[]) => {
    void runOp(() => cvApi.reorderLayers(doc.id, doc.revision, order));
  }, [doc.id, doc.revision, runOp]);

  const moveLayerUp = useCallback((layerId: string) => {
    const ids = content.layers.map((l) => l.id);
    const i = ids.indexOf(layerId);
    if (i < 0 || i === ids.length - 1) return;
    [ids[i], ids[i + 1]] = [ids[i + 1], ids[i]];
    reorder(ids);
  }, [content.layers, reorder]);

  const moveLayerDown = useCallback((layerId: string) => {
    const ids = content.layers.map((l) => l.id);
    const i = ids.indexOf(layerId);
    if (i <= 0) return;
    [ids[i], ids[i - 1]] = [ids[i - 1], ids[i]];
    reorder(ids);
  }, [content.layers, reorder]);

  const undo = useCallback(() => {
    const idx = undoIndex + 1;
    if (idx >= revisionHistory.length) return;
    const target = revisionHistory[revisionHistory.length - 1 - idx];
    void undoTo(doc.id, target, doc.revision)
      .then((result) => { setUndoIndex(idx); onDocUpdated(result.document); setToast(t('Undone.')); })
      .catch((err) => {
        if (err instanceof api.RevisionConflictError) onRevisionConflict();
        else setError(err instanceof Error ? err.message : String(err));
      });
  }, [undoIndex, revisionHistory, doc.id, doc.revision, onDocUpdated, onRevisionConflict]);

  const redo = useCallback(() => {
    const idx = undoIndex - 1;
    if (idx < 0) return;
    const target = revisionHistory[revisionHistory.length - 1 - idx];
    void undoTo(doc.id, target, doc.revision)
      .then((result) => { setUndoIndex(idx); onDocUpdated(result.document); setToast(t('Redone.')); })
      .catch((err) => {
        if (err instanceof api.RevisionConflictError) onRevisionConflict();
        else setError(err instanceof Error ? err.message : String(err));
      });
  }, [undoIndex, revisionHistory, doc.id, doc.revision, onDocUpdated, onRevisionConflict]);

  const onExport = useCallback(async () => {
    try {
      const result = await cvApi.exportCanvas(doc.id, 'png');
      setToast(t('Exported as {id}', { id: result.occurrence_id }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [doc.id]);

  const onInpaintRequest = useCallback(async () => {
    if (!selectedLayerId || !inpaintPrompt.trim()) return;
    const layer = content.layers.find((l) => l.id === selectedLayerId);
    if (!layer) return;
    setInpaintBusy(true);
    setError(null);
    try {
      const region: cvApi.CanvasRegion = {
        x: Math.max(0, layer.offset.x), y: Math.max(0, layer.offset.y),
        width: Math.min(content.width - Math.max(0, layer.offset.x), 64),
        height: Math.min(content.height - Math.max(0, layer.offset.y), 64),
        source_w: content.width, source_h: content.height,
      };
      const result = await cvApi.requestInpaint(doc.id, region, inpaintPrompt.trim());
      setToast(t('Inpaint proposal ready ({recipe})', { recipe: result.recipe_id }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setInpaintBusy(false);
    }
  }, [doc.id, selectedLayerId, inpaintPrompt, content]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 2500);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const renderUrl = useMemo(() => cvApi.renderUrl(doc.id, doc.revision), [doc.id, doc.revision]);

  if (!content || typeof content.width !== 'number' || !Array.isArray(content.layers)) {
    return <EmptyState title={t('No canvas content')} body={t('This document has no layers yet.')} />;
  }

  return (
    <div className="fs-canvas" data-testid="canvas-editor" role="application" aria-label={t('Canvas editor')}>
      <div className="fs-canvas__toolbar" role="toolbar" aria-label={t('Canvas controls')}>
        <span className="fs-canvas__muted">{t('revision {n}', { n: doc.revision })}</span>
        <Button size="sm" variant="ghost" icon={Undo2} label={t('Undo')}
          onClick={undo} disabled={undoIndex + 1 >= revisionHistory.length} testId="canvas-undo" />
        <Button size="sm" variant="ghost" icon={Redo2} label={t('Redo')}
          onClick={redo} disabled={undoIndex <= 0} testId="canvas-redo" />
        <Button size="sm" variant="ghost" icon={ZoomOut} label={t('Zoom out')}
          onClick={() => setZoom((z) => Math.max(0.1, z / 1.25))} testId="canvas-zoom-out" />
        <Button size="sm" variant="ghost" icon={ZoomIn} label={t('Zoom in')}
          onClick={() => setZoom((z) => Math.min(4, z * 1.25))} testId="canvas-zoom-in" />
        <Button size="sm" variant={maskMode ? 'primary' : 'ghost'} icon={Shapes} label={t('Polygon mask')}
          onClick={() => setMaskMode((m) => !m)} disabled={!selectedLayerId} testId="canvas-mask-mode" />
        <Button size="sm" variant="ghost" icon={Download} label={t('Export')} onClick={() => void onExport()} testId="canvas-export" />
      </div>

      {error && <p className="fs-canvas__error" role="alert">{error}</p>}

      <div className="fs-canvas__body">
        <div className="fs-canvas__stage" style={{ width: content.width * zoom, height: content.height * zoom }}>
          <CanvasLayers
            content={content}
            baseImageUrl={renderUrl}
            selectedLayerId={selectedLayerId}
            onSelectLayer={setSelectedLayerId}
            onCommitMove={onCommitMove}
            onCommitTransform={onCommitTransform}
            maskMode={maskMode}
            onCommitPolygonMask={onCommitPolygonMask}
            zoom={zoom}
          />
        </div>

        <aside className="fs-canvas__layers" aria-label={t('Layers')}>
          <h3>{t('Layers')}</h3>
          <ul>
            {[...content.layers].reverse().map((layer) => (
              <li
                key={layer.id}
                data-testid={`canvas-layer-${layer.id}`}
                className={layer.id === selectedLayerId ? 'fs-canvas__layer fs-canvas__layer--selected' : 'fs-canvas__layer'}
                onClick={() => setSelectedLayerId(layer.id)}
              >
                <span>{layer.id}</span>
                <span className="fs-canvas__muted">{layer.kind}</span>
                <button type="button" data-testid={`canvas-layer-up-${layer.id}`}
                  onClick={(e) => { e.stopPropagation(); moveLayerUp(layer.id); }}>↑</button>
                <button type="button" data-testid={`canvas-layer-down-${layer.id}`}
                  onClick={(e) => { e.stopPropagation(); moveLayerDown(layer.id); }}>↓</button>
              </li>
            ))}
          </ul>

          {selectedLayerId && (
            <div className="fs-canvas__inspector">
              <label>
                {t('Blend mode')}
                <select
                  data-testid="canvas-blend-select"
                  value={content.layers.find((l) => l.id === selectedLayerId)?.blend_mode ?? 'normal'}
                  onChange={(e) => setBlendMode(e.target.value as cvApi.BlendMode)}
                >
                  <option value="normal">{t('Normal')}</option>
                  <option value="multiply">{t('Multiply')}</option>
                  <option value="screen">{t('Screen')}</option>
                </select>
              </label>

              <label>
                {t('Inpaint prompt')}
                <input
                  data-testid="canvas-inpaint-prompt"
                  value={inpaintPrompt}
                  onChange={(e) => setInpaintPrompt(e.target.value)}
                  placeholder={t('Describe the change')}
                />
              </label>
              <Button size="sm" variant="primary" icon={Wand2} label={t('Request generation')}
                onClick={() => void onInpaintRequest()} loading={inpaintBusy}
                disabled={!inpaintPrompt.trim()} testId="canvas-inpaint-request" />
            </div>
          )}

          {maskMode && (
            <p className="fs-canvas__hint">{t('Click to add points, double-click to close the mask.')}</p>
          )}
        </aside>
      </div>

      {toast && <Toast>{toast}</Toast>}
    </div>
  );
}
