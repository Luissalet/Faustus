import { responseReason } from './api';
import { applyCommand, CreatorApiError, RevisionConflictError, type ApplyCommandResult } from './creator';

/**
 * WP20 — Studio adapter over the canvas-specific typed ops added to
 * `src/creator/ops/canvas_ops.py` (WP12's `canvas.add_layer`/`move_layer`/
 * `set_mask`/`crop`/`rotate`/`reorder`) plus WP20's additive sibling
 * `src/creator/ops/canvas_layer_style_ops.py` (`set_layer_transform`/
 * `set_blend_mode`/`set_polygon_mask`). Every mutating call goes through
 * `applyCommand` (`adapters/creator.ts`) — one `POST
 * /api/creator/documents/{id}/commands` route dispatches every op by
 * `op.type`, so a stale `expected_revision` always surfaces as the SAME
 * `RevisionConflictError` regardless of which op sent it.
 *
 * The read/derive routes below (`routes/creator_canvas_routes.py`) are
 * separate: server-side render (composited PNG bytes), export (publish a
 * flattened occurrence), legacy import, and the inpaint-request bridge to
 * WP11.
 */

export type BlendMode = 'normal' | 'multiply' | 'screen';
export type LayerKind = 'raster' | 'mask' | 'text' | 'control';

export interface CanvasOffset { x: number; y: number }

export interface CanvasRegion {
  x: number; y: number; width: number; height: number;
  source_w: number; source_h: number;
  exif_orientation?: number;
  layer_transform?: Record<string, unknown>;
}

export interface CanvasLayer {
  id: string;
  kind: LayerKind;
  visible: boolean;
  opacity: number;
  offset: CanvasOffset;
  asset_ref?: string;
  mask_asset_ref?: string;
  mask_polygon?: { x: number; y: number }[];
  crop_region?: CanvasRegion;
  rotation_degrees?: number;
  scale?: number;
  blend_mode?: BlendMode;
}

export interface CanvasDocContent {
  width: number;
  height: number;
  operation_semantics_version: number;
  base_asset_ref: string;
  layers: CanvasLayer[];
  crop_region?: CanvasRegion;
  rotation_degrees?: number;
}

export function genCommandId(prefix = 'cmd'): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

// ── WP12 ops (carried over, typed wrappers) ─────────────────────────────

export function addLayer(
  docId: string, expectedRevision: number, layer: {
    object_id: string; kind: LayerKind; asset_ref?: string; visible?: boolean;
    opacity?: number; offset?: CanvasOffset; index?: number;
  },
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('add-layer'), expectedRevision, { type: 'canvas.add_layer', ...layer });
}

export function moveLayer(
  docId: string, expectedRevision: number, objectId: string, offset: CanvasOffset,
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('move'), expectedRevision, {
    type: 'canvas.move_layer', object_id: objectId, offset,
  });
}

export function setMask(
  docId: string, expectedRevision: number, objectId: string, maskAssetRef: string | null,
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('mask'), expectedRevision, {
    type: 'canvas.set_mask', object_id: objectId, mask_asset_ref: maskAssetRef,
  });
}

export function cropRegion(
  docId: string, expectedRevision: number, region: CanvasRegion, objectId = 'canvas',
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('crop'), expectedRevision, {
    type: 'canvas.crop', object_id: objectId, region,
  });
}

export function rotate(
  docId: string, expectedRevision: number, degrees: number, objectId = 'canvas',
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('rotate'), expectedRevision, {
    type: 'canvas.rotate', object_id: objectId, degrees,
  });
}

export function reorderLayers(
  docId: string, expectedRevision: number, order: string[],
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('reorder'), expectedRevision, { type: 'canvas.reorder', order });
}

// ── WP20 additive ops ────────────────────────────────────────────────────

export function setLayerTransform(
  docId: string, expectedRevision: number, objectId: string, scale: number, degrees?: number,
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('transform'), expectedRevision, {
    type: 'canvas.set_layer_transform', object_id: objectId, scale, degrees,
  });
}

export function setBlendMode(
  docId: string, expectedRevision: number, objectId: string, blendMode: BlendMode,
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('blend'), expectedRevision, {
    type: 'canvas.set_blend_mode', object_id: objectId, blend_mode: blendMode,
  });
}

export function setPolygonMask(
  docId: string, expectedRevision: number, objectId: string, points: { x: number; y: number }[] | null,
): Promise<ApplyCommandResult> {
  return applyCommand(docId, genCommandId('polygon'), expectedRevision, {
    type: 'canvas.set_polygon_mask', object_id: objectId, points,
  });
}

// ── server-side render/export/import/inpaint (routes/creator_canvas_routes.py) ─

async function payloadOf(response: Response): Promise<Record<string, unknown>> {
  try {
    const body: unknown = await response.clone().json();
    return body && typeof body === 'object' ? (body as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

async function cvRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: { Accept: 'application/json', ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const payload = await payloadOf(response);
    const detail = payload.detail && typeof payload.detail === 'object'
      ? (payload.detail as Record<string, unknown>)
      : payload;
    const message = await responseReason(response, path);
    if (response.status === 409 && detail.reason === 'revision_conflict') {
      throw new RevisionConflictError(message, detail);
    }
    throw new CreatorApiError(message, response.status, detail);
  }
  return (await response.json()) as T;
}

/** The composited PNG's URL for `<img src=...>` — the browser does the
 *  fetch (and caches on the ETag revision), this is not a JSON call. */
export function renderUrl(docId: string, revision?: number): string {
  const qs = revision ? `?revision=${revision}` : '';
  return `/api/creator/canvas/${encodeURIComponent(docId)}/render${qs}`;
}

export interface ExportResult {
  occurrence_id: string;
  created: boolean;
  format: string;
  byte_size: number;
  [key: string]: unknown;
}

export function exportCanvas(docId: string, format: 'png' | 'jpeg' = 'png'): Promise<ExportResult> {
  return cvRequest(`/api/creator/canvas/${encodeURIComponent(docId)}/export`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ format }),
  });
}

export interface CreatorDocumentLike {
  id: string; project_id: string; kind: string; schema_version: number;
  revision: number; state: string; asset_refs: string[]; content: unknown;
  created_at: string; updated_at: string;
}

export function importLegacyCanvas(editProjectId: string, projectId: string): Promise<CreatorDocumentLike> {
  return cvRequest('/api/creator/canvas/import-legacy', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ edit_project_id: editProjectId, project_id: projectId }),
  });
}

export interface InpaintRequestResult {
  recipe_id: string;
  params: { prompt: string; reference_image: string; mask: string };
  region: Record<string, unknown>;
  document_id: string;
  document_revision: number;
}

export function requestInpaint(docId: string, region: CanvasRegion, prompt: string): Promise<InpaintRequestResult> {
  return cvRequest(`/api/creator/canvas/${encodeURIComponent(docId)}/inpaint-request`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ region, prompt }),
  });
}
